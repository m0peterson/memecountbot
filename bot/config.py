"""Configuration loaded from environment variables (or a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

DEFAULT_WARNING_TEXT = (
    "Россияне, которые скидывают мемы в общие чаты, работают хуже всех, "
    "заявили аналитики «Стахановца». Те, кто отправляет более пяти нерабочих "
    "сообщений в день, показывают снижение эффективности на 27% относительно "
    "коллег. Сотрудник начинает отставать по задачам, теряет фокус и "
    "переключается на развлекательный контент как способ избегания сложной работы."
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _id_set(name: str) -> set[int] | None:
    """Parse a comma-separated id list, or ``None`` when the variable is unset.

    A malformed entry is fatal on purpose: for an allowlist, silently dropping
    the id it could not read would leave an empty set, and an empty set means
    "no restriction" — a typo would quietly switch the allowlist off.
    """
    raw = _str(name)
    if not raw:
        return None
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise ConfigError(
                f"{name} contains {part!r}, which is not a numeric Telegram id. "
                f"Use a comma-separated list of ids, for example {name}=-1001234567890"
            ) from None
    if not ids:
        raise ConfigError(f"{name} is set but lists no ids. Leave it empty to disable it.")
    return ids


@dataclass(frozen=True)
class Config:
    # --- required ---
    bot_token: str
    openrouter_api_key: str

    # --- llm ---
    openrouter_model: str = "google/gemini-2.5-flash"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    min_confidence: float = 0.6
    request_timeout: float = 45.0
    llm_retries: int = 2

    # --- counting ---
    daily_limit: int = 5
    timezone: ZoneInfo = ZoneInfo("Europe/Moscow")
    warn_once_per_day: bool = True

    # --- what to look at ---
    check_text: bool = True
    check_video: bool = False
    min_text_len: int = 12
    max_text_len: int = 1500
    count_stickers_as_memes: bool = False

    # --- reply payload ---
    warning_file: str = "assets/warning.jpg"
    warning_type: str = "auto"  # auto|photo|sticker|animation|video|document|text
    warning_text: str = DEFAULT_WARNING_TEXT
    send_warning_caption: bool = False

    # --- misc ---
    db_path: str = "data/memecount.db"
    allowed_chats: set[int] | None = None
    admin_ids: set[int] | None = None
    enable_commands: bool = True
    drop_pending_updates: bool = False
    health_port: int = 0
    log_level: str = "INFO"
    cache_verdicts: bool = True
    retention_days: int = 30

    @property
    def warn_threshold(self) -> int:
        """A user is warned once their count exceeds ``daily_limit``."""
        return self.daily_limit + 1


def load_config() -> Config:
    token = _str("BOT_TOKEN") or _str("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError(
            "BOT_TOKEN is not set. Get one from @BotFather and put it in your .env file."
        )

    api_key = _str("OPENROUTER_API_KEY")
    if not api_key:
        raise ConfigError(
            "OPENROUTER_API_KEY is not set. Create a key at https://openrouter.ai/keys"
        )

    tz_name = _str("TIMEZONE", "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TIMEZONE={tz_name!r} is not a valid IANA timezone") from exc

    warning_type = _str("WARNING_TYPE", "auto").lower()
    valid_types = {"auto", "photo", "sticker", "animation", "video", "document", "text"}
    if warning_type not in valid_types:
        raise ConfigError(
            f"WARNING_TYPE={warning_type!r} must be one of: {', '.join(sorted(valid_types))}"
        )

    return Config(
        bot_token=token,
        openrouter_api_key=api_key,
        openrouter_model=_str("OPENROUTER_MODEL", "google/gemini-2.5-flash"),
        openrouter_base_url=_str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        min_confidence=_float("MIN_CONFIDENCE", 0.6),
        request_timeout=_float("REQUEST_TIMEOUT", 45.0),
        llm_retries=_int("LLM_RETRIES", 2),
        daily_limit=max(0, _int("DAILY_LIMIT", 5)),
        timezone=tz,
        warn_once_per_day=_bool("WARN_ONCE_PER_DAY", True),
        check_text=_bool("CHECK_TEXT", True),
        check_video=_bool("CHECK_VIDEO", False),
        min_text_len=_int("MIN_TEXT_LEN", 12),
        max_text_len=_int("MAX_TEXT_LEN", 1500),
        count_stickers_as_memes=_bool("COUNT_STICKERS_AS_MEMES", False),
        warning_file=_str("WARNING_FILE", "assets/warning.jpg"),
        warning_type=warning_type,
        warning_text=_str("WARNING_TEXT", DEFAULT_WARNING_TEXT),
        send_warning_caption=_bool("SEND_WARNING_CAPTION", False),
        db_path=_str("DB_PATH", "data/memecount.db"),
        allowed_chats=_id_set("ALLOWED_CHATS"),
        admin_ids=_id_set("ADMIN_IDS"),
        enable_commands=_bool("ENABLE_COMMANDS", True),
        drop_pending_updates=_bool("DROP_PENDING_UPDATES", False),
        health_port=_int("PORT", 0),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        cache_verdicts=_bool("CACHE_VERDICTS", True),
        retention_days=_int("RETENTION_DAYS", 30),
    )

"""memecountbot — silently counts memes per user per day and calls them out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command
from aiogram.types import Message

from bot.classifier import MemeClassifier, Verdict
from bot.config import Config, ConfigError, load_config
from bot.media import Candidate, build_candidate
from bot.storage import Storage
from bot.warning import WarningSender

log = logging.getLogger("memecountbot")

router = Router(name="memecount")

GROUP_CHATS = {ChatType.GROUP, ChatType.SUPERGROUP}
PURGE_INTERVAL_SECONDS = 6 * 60 * 60


class Runtime:
    """Everything the handlers need, assembled once at startup."""

    def __init__(self, cfg: Config, storage: Storage, classifier: MemeClassifier,
                 sender: WarningSender) -> None:
        self.cfg = cfg
        self.storage = storage
        self.classifier = classifier
        self.sender = sender


# --------------------------------------------------------------------- helpers

def chat_allowed(cfg: Config, chat_id: int) -> bool:
    return cfg.allowed_chats is None or chat_id in cfg.allowed_chats


async def is_admin(bot: Bot, cfg: Config, chat_id: int, user_id: int) -> bool:
    if cfg.admin_ids and user_id in cfg.admin_ids:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in {"creator", "administrator"}


def display_name(user) -> str:
    name = " ".join(filter(None, [getattr(user, "first_name", None),
                                  getattr(user, "last_name", None)])).strip()
    return name or (getattr(user, "username", None) or f"id{user.id}")


async def _download(bot: Bot, file_id: str) -> bytes | None:
    try:
        buf = await bot.download(file_id)
    except TelegramAPIError as exc:
        log.warning("could not download %s: %s", file_id, exc)
        return None
    if buf is None:
        return None
    try:
        return buf.read()
    finally:
        with contextlib.suppress(Exception):
            buf.close()


async def judge(rt: Runtime, bot: Bot, candidate: Candidate) -> Verdict | None:
    """Classify a candidate, using the persistent cache when we can."""
    cfg = rt.cfg

    if candidate.kind == "sticker" and cfg.count_stickers_as_memes:
        return Verdict(True, 1.0, "stickers always count")

    key = candidate.cache_key if cfg.cache_verdicts else None
    if key:
        cached = await rt.storage.get_verdict(key)
        if cached is not None:
            log.debug("cache hit for %s -> %s", key, cached.is_meme)
            return Verdict(cached.is_meme, cached.confidence, cached.reason)

    image: bytes | None = None
    if candidate.has_image:
        image = await _download(bot, candidate.file_id)
        if image is None and not candidate.text:
            return None

    verdict = await rt.classifier.classify(
        text=candidate.text, hint=candidate.hint, image=image
    )
    if verdict is None:
        return None  # the model was unreachable — never cache an unknown
    if key:
        await rt.storage.put_verdict(key, verdict.is_meme, verdict.confidence, verdict.reason)
    return verdict


# -------------------------------------------------------------------- handlers

@router.message(Command("memestats"), F.chat.type.in_(GROUP_CHATS))
async def cmd_stats(message: Message, rt: Runtime) -> None:
    if not rt.cfg.enable_commands or not chat_allowed(rt.cfg, message.chat.id):
        return
    day = rt.storage.today(message.date)
    rows = await rt.storage.leaderboard(message.chat.id, day)
    if not rows:
        await message.reply(f"Сегодня ({day}) мемов не замечено. Продуктивный день.")
        return

    lines = [f"Мемы за {day} (лимит {rt.cfg.daily_limit}/день):"]
    for place, (user_id, count) in enumerate(rows, start=1):
        try:
            member = await message.bot.get_chat_member(message.chat.id, user_id)
            name = display_name(member.user)
        except TelegramAPIError:
            name = f"id{user_id}"
        flag = " ⚠️" if count > rt.cfg.daily_limit else ""
        lines.append(f"{place}. {name} — {count}{flag}")
    await message.reply("\n".join(lines))


@router.message(Command("memereset"), F.chat.type.in_(GROUP_CHATS))
async def cmd_reset(message: Message, rt: Runtime) -> None:
    if not rt.cfg.enable_commands or not chat_allowed(rt.cfg, message.chat.id):
        return
    if not message.from_user or not await is_admin(
        message.bot, rt.cfg, message.chat.id, message.from_user.id
    ):
        await message.reply("Сбрасывать счётчики могут только админы чата.")
        return
    day = rt.storage.today(message.date)
    removed = await rt.storage.reset_chat_day(message.chat.id, day)
    await message.reply(f"Счётчики за {day} сброшены ({removed} записей).")


@router.message(Command("memehelp"), F.chat.type.in_(GROUP_CHATS))
async def cmd_help(message: Message, rt: Runtime) -> None:
    if not rt.cfg.enable_commands or not chat_allowed(rt.cfg, message.chat.id):
        return
    cfg = rt.cfg
    await message.reply(
        "Я молча читаю чат и считаю мемы по каждому участнику.\n"
        f"Лимит: {cfg.daily_limit} мемов в день (сброс в полночь, {cfg.timezone}).\n"
        f"На {cfg.warn_threshold}-м мемe отвечаю напоминанием о продуктивности.\n\n"
        "/memestats — счёт за сегодня\n"
        "/memereset — сбросить счёт за сегодня (админы)"
    )


@router.message(F.chat.type.in_(GROUP_CHATS))
async def on_group_message(message: Message, rt: Runtime) -> None:
    cfg = rt.cfg
    if not chat_allowed(cfg, message.chat.id):
        return
    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None:
        return  # bots, channel posts and anonymous admins are not tracked

    day = rt.storage.today(message.date)
    state = await rt.storage.get_state(message.chat.id, user.id, day)
    if cfg.warn_once_per_day and state.warned:
        return  # already called out today — no need to spend tokens on them

    candidate = build_candidate(
        message,
        check_text=cfg.check_text,
        check_video=cfg.check_video,
        min_text_len=cfg.min_text_len,
        max_text_len=cfg.max_text_len,
    )
    if candidate is None:
        return

    verdict = await judge(rt, message.bot, candidate)
    if verdict is None or not verdict.is_meme:
        return

    count = await rt.storage.increment(message.chat.id, user.id, day)
    log.info(
        "meme #%s by %s (%s) in %s [%s] — %s",
        count, display_name(user), user.id, message.chat.id, candidate.kind, verdict.reason,
    )

    if count < cfg.warn_threshold:
        return

    first_warning = await rt.storage.mark_warned(message.chat.id, user.id, day)
    if cfg.warn_once_per_day and not first_warning:
        return

    if await rt.sender.send(message):
        log.info("warned %s (%s) in chat %s after %s memes",
                 display_name(user), user.id, message.chat.id, count)


@router.errors()
async def on_error(event) -> bool:
    log.exception("unhandled error while processing update: %s", event.exception)
    return True


# ---------------------------------------------------------------------- wiring

async def _purge_loop(storage: Storage, retention_days: int) -> None:
    while True:
        try:
            removed = await storage.purge_old(retention_days)
            if removed:
                log.info("purged %s counter rows older than %s days", removed, retention_days)
        except Exception:  # noqa: BLE001 - a housekeeping loop must never die
            log.exception("purge failed")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


async def _health_server(port: int) -> None:
    from aiohttp import web

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/health", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health endpoint listening on port %s", port)


async def _shutdown(classifier: MemeClassifier, storage: Storage, bot: Bot) -> None:
    await classifier.aclose()
    await storage.close()
    await bot.session.close()


async def run(cfg: Config) -> None:
    storage = Storage(cfg.db_path, cfg.timezone)
    await storage.connect()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=None))
    classifier = MemeClassifier(
        cfg.openrouter_api_key,
        cfg.openrouter_model,
        base_url=cfg.openrouter_base_url,
        min_confidence=cfg.min_confidence,
        timeout=cfg.request_timeout,
        retries=cfg.llm_retries,
    )
    sender = WarningSender(
        bot,
        storage,
        warning_file=cfg.warning_file,
        warning_type=cfg.warning_type,
        warning_text=cfg.warning_text,
        send_caption=cfg.send_warning_caption,
    )
    rt = Runtime(cfg, storage, classifier, sender)

    dp = Dispatcher()
    dp.include_router(router)
    dp["rt"] = rt

    log.info("model=%s limit=%s/day tz=%s warning=%s",
             cfg.openrouter_model, cfg.daily_limit, cfg.timezone, sender.describes)
    log.info("connecting to Telegram...")
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("Telegram rejected BOT_TOKEN. Check the token you got from @BotFather.")
        await _shutdown(classifier, storage, bot)
        return
    except TelegramNetworkError as exc:
        log.error("Cannot reach api.telegram.org (%s). Check the host's network or proxy.", exc)
        await _shutdown(classifier, storage, bot)
        return

    log.info("connected as @%s (id %s)", me.username, me.id)
    if not me.can_read_all_group_messages:
        log.warning(
            "PRIVACY MODE IS ON — the bot only sees commands and replies to itself, "
            "so it will count nothing. Open @BotFather -> /setprivacy -> pick this bot "
            "-> Disable, then remove and re-add the bot to the group."
        )

    tasks = [asyncio.create_task(_purge_loop(storage, cfg.retention_days))]
    if cfg.health_port:
        await _health_server(cfg.health_port)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await _shutdown(classifier, storage, bot)


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        log.error("%s", exc)
        return 2

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    try:
        asyncio.run(run(cfg))
    except (KeyboardInterrupt, SystemExit):
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())

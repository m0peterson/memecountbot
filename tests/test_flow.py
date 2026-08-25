"""End-to-end-ish test of the counting handler with fake Telegram/LLM parts."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from bot.classifier import Verdict
from bot.config import Config
from bot.main import Runtime, on_group_message
from bot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


class FakeClassifier:
    def __init__(self, verdict=Verdict(True, 0.95, "joke picture")):
        self.verdict = verdict
        self.calls = 0

    async def classify(self, *, text="", hint="", image=None):
        self.calls += 1
        return self.verdict


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return True


class FakeBot:
    async def download(self, file_id):
        raise AssertionError("no download expected for text messages")


def make_message(text="Заходит как-то программист в бар", user_id=42, chat_id=-100):
    return NS(
        text=text, caption=None, photo=None, sticker=None, animation=None, video=None,
        document=None, voice=None, video_note=None, audio=None, contact=None,
        location=None, poll=None, dice=None, invoice=None,
        forward_origin=None, forward_from=None,
        chat=NS(id=chat_id, type="supergroup"),
        from_user=NS(id=user_id, is_bot=False, first_name="Денис", last_name=None, username="den"),
        sender_chat=None, date=NOW, bot=FakeBot(),
    )


def make_runtime(tmp_path, classifier=None, sender=None, **overrides):
    cfg = Config(
        bot_token="t", openrouter_api_key="k", timezone=TZ,
        db_path=str(tmp_path / "flow.db"), warning_file="", **overrides,
    )
    storage = Storage(cfg.db_path, TZ)
    return cfg, storage, Runtime(cfg, storage, classifier or FakeClassifier(),
                                 sender or FakeSender())


def test_warns_on_the_sixth_meme_only(tmp_path):
    classifier, sender = FakeClassifier(), FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, classifier, sender)

    async def scenario():
        await storage.connect()
        for i in range(1, 9):
            await on_group_message(make_message(f"мем номер {i} про работу"), rt)
        # limit is 5, so the warning fires exactly once, on message #6
        assert len(sender.sent) == 1
        assert sender.sent[0].text.startswith("мем номер 6")
        state = await storage.get_state(-100, 42, "2026-08-25")
        assert state.warned is True
        # after the warning the bot stops spending tokens on that user today
        assert classifier.calls == 6
        await storage.close()

    asyncio.run(scenario())


def test_repeat_warnings_when_once_per_day_disabled(tmp_path):
    sender = FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, sender=sender, warn_once_per_day=False)

    async def scenario():
        await storage.connect()
        for i in range(1, 9):
            await on_group_message(make_message(f"мем номер {i} про работу"), rt)
        assert len(sender.sent) == 3  # messages 6, 7, 8
        await storage.close()

    asyncio.run(scenario())


def test_non_memes_are_never_counted(tmp_path):
    sender = FakeSender()
    cfg, storage, rt = make_runtime(
        tmp_path, classifier=FakeClassifier(Verdict(False, 0.9, "work screenshot")),
        sender=sender,
    )

    async def scenario():
        await storage.connect()
        for i in range(10):
            await on_group_message(make_message(f"деплой прошёл, версия {i}"), rt)
        assert sender.sent == []
        assert (await storage.get_state(-100, 42, "2026-08-25")).count == 0
        await storage.close()

    asyncio.run(scenario())


def test_counters_are_per_user(tmp_path):
    sender = FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, sender=sender)

    async def scenario():
        await storage.connect()
        for uid in (1, 2, 3):
            for i in range(5):
                await on_group_message(make_message(f"мем {i} для всех", user_id=uid), rt)
        assert sender.sent == []  # nobody crossed the limit
        for i in range(1):
            await on_group_message(make_message("шестой мем подряд", user_id=2), rt)
        assert len(sender.sent) == 1
        assert (await storage.get_state(-100, 1, "2026-08-25")).count == 5
        assert (await storage.get_state(-100, 2, "2026-08-25")).count == 6
        await storage.close()

    asyncio.run(scenario())


def test_bots_and_anonymous_admins_are_skipped(tmp_path):
    classifier, sender = FakeClassifier(), FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, classifier, sender)

    async def scenario():
        await storage.connect()
        bot_msg = make_message("мем от другого бота")
        bot_msg.from_user.is_bot = True
        await on_group_message(bot_msg, rt)

        anon = make_message("мем от анонимного админа")
        anon.sender_chat = NS(id=-100)
        await on_group_message(anon, rt)

        assert classifier.calls == 0
        await storage.close()

    asyncio.run(scenario())


def test_allowed_chats_filter(tmp_path):
    classifier, sender = FakeClassifier(), FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, classifier, sender, allowed_chats={-999})

    async def scenario():
        await storage.connect()
        await on_group_message(make_message("мем в чужом чате"), rt)
        assert classifier.calls == 0
        await on_group_message(make_message("мем в разрешённом чате", chat_id=-999), rt)
        assert classifier.calls == 1
        await storage.close()

    asyncio.run(scenario())


def test_day_rollover_resets_the_counter(tmp_path):
    sender = FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, sender=sender)

    async def scenario():
        await storage.connect()
        for i in range(6):
            await on_group_message(make_message(f"мем {i} сегодня"), rt)
        assert len(sender.sent) == 1

        tomorrow = make_message("первый мем нового дня")
        tomorrow.date = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)
        await on_group_message(tomorrow, rt)
        assert (await storage.get_state(-100, 42, "2026-08-26")).count == 1
        assert len(sender.sent) == 1  # fresh day, fresh start
        await storage.close()

    asyncio.run(scenario())


def test_verdict_cache_avoids_a_second_llm_call(tmp_path):
    classifier, sender = FakeClassifier(), FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, classifier, sender)

    sticker = NS(file_id="sf", file_unique_id="su", emoji="😂", set_name="Pack",
                 is_animated=False, is_video=False, thumbnail=None)

    async def scenario():
        await storage.connect()
        for _ in range(3):
            m = make_message(text=None)
            m.sticker = sticker
            m.bot = NS(download=_fake_download)
            await on_group_message(m, rt)
        assert classifier.calls == 1
        assert (await storage.get_state(-100, 42, "2026-08-25")).count == 3
        await storage.close()

    asyncio.run(scenario())


async def _fake_download(file_id):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="PNG")
    buf.seek(0)
    return buf


class BrokenClassifier:
    """Stands in for an unreachable OpenRouter: every call returns 'unknown'."""

    def __init__(self):
        self.calls = 0

    async def classify(self, *, text="", hint="", image=None):
        self.calls += 1
        return None


def test_llm_outage_counts_nothing_and_caches_nothing(tmp_path):
    broken, sender = BrokenClassifier(), FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, broken, sender)

    sticker = NS(file_id="sf", file_unique_id="su", emoji="😂", set_name="Pack",
                 is_animated=False, is_video=False, thumbnail=None)

    async def scenario():
        await storage.connect()
        for _ in range(3):
            m = make_message(text=None)
            m.sticker = sticker
            m.bot = NS(download=_fake_download)
            await on_group_message(m, rt)

        assert sender.sent == []
        assert (await storage.get_state(-100, 42, "2026-08-25")).count == 0
        # crucially: the outage was not remembered as "not a meme"
        assert await storage.get_verdict(f"{rt.verdict_ns}:sticker:su") is None
        assert broken.calls == 3
        await storage.close()

    asyncio.run(scenario())


class FailingSender:
    """Telegram is down, or the warning asset is broken: nothing reaches the chat."""

    def __init__(self, fail_times=99):
        self.attempts = 0
        self.sent = []
        self._fail_times = fail_times

    async def send(self, message):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            return False
        self.sent.append(message)
        return True


def test_failed_delivery_does_not_burn_the_daily_warning(tmp_path):
    sender = FailingSender(fail_times=1)
    cfg, storage, rt = make_runtime(tmp_path, sender=sender)

    async def scenario():
        await storage.connect()
        for i in range(1, 7):
            await on_group_message(make_message(f"мем номер {i} про работу"), rt)
        # The 6th meme crossed the limit but the reply never left the process,
        # so the user must not be recorded as warned.
        assert sender.attempts == 1
        assert sender.sent == []
        assert (await storage.get_state(-100, 42, "2026-08-25")).warned is False

        # The next meme retries, succeeds, and only now is the warning spent.
        await on_group_message(make_message("седьмой мем подряд"), rt)
        assert sender.attempts == 2
        assert len(sender.sent) == 1
        assert (await storage.get_state(-100, 42, "2026-08-25")).warned is True

        # And it really is once per day from here on.
        await on_group_message(make_message("восьмой мем подряд"), rt)
        assert sender.attempts == 2
        await storage.close()

    asyncio.run(scenario())


def test_persistent_delivery_failure_keeps_retrying(tmp_path):
    sender = FailingSender()
    cfg, storage, rt = make_runtime(tmp_path, sender=sender)

    async def scenario():
        await storage.connect()
        for i in range(1, 10):
            await on_group_message(make_message(f"мем номер {i} про работу"), rt)
        assert sender.attempts == 4  # memes 6..9 each get a fresh attempt
        assert (await storage.get_state(-100, 42, "2026-08-25")).count == 9
        await storage.close()

    asyncio.run(scenario())


def test_verdict_cache_is_scoped_to_the_model_and_threshold(tmp_path):
    classifier, sender = FakeClassifier(), FakeSender()
    cfg, storage, rt = make_runtime(tmp_path, classifier, sender)

    sticker = NS(file_id="sf", file_unique_id="su", emoji="😂", set_name="Pack",
                 is_animated=False, is_video=False, thumbnail=None)

    async def scenario():
        await storage.connect()
        m = make_message(text=None)
        m.sticker = sticker
        m.bot = NS(download=_fake_download)
        await on_group_message(m, rt)
        assert classifier.calls == 1

        # Raising the threshold must not reuse a verdict computed under the old one.
        strict = Config(bot_token="t", openrouter_api_key="k", timezone=TZ,
                        db_path=cfg.db_path, warning_file="", min_confidence=0.99)
        rt2 = Runtime(strict, storage, classifier, sender)
        m2 = make_message(text=None)
        m2.sticker = sticker
        m2.bot = NS(download=_fake_download)
        await on_group_message(m2, rt2)
        assert classifier.calls == 2
        await storage.close()

    asyncio.run(scenario())

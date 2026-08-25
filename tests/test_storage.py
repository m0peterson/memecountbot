import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")


def run(coro):
    return asyncio.run(coro)


def fresh(tmp_path):
    s = Storage(str(tmp_path / "test.db"), TZ)
    run(s.connect())
    return s


def test_increment_counts_per_user_and_day(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        assert await s.increment(-100, 1, "2026-08-25") == 1
        assert await s.increment(-100, 1, "2026-08-25") == 2
        assert await s.increment(-100, 2, "2026-08-25") == 1     # other user
        assert await s.increment(-200, 1, "2026-08-25") == 1     # other chat
        assert await s.increment(-100, 1, "2026-08-26") == 1     # next day resets
        state = await s.get_state(-100, 1, "2026-08-25")
        assert state.count == 2 and state.warned is False
        assert (await s.get_state(-100, 999, "2026-08-25")).count == 0
        await s.close()

    run(scenario())


def test_concurrent_increments_do_not_lose_updates(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        await asyncio.gather(*(s.increment(-1, 7, "2026-08-25") for _ in range(25)))
        assert (await s.get_state(-1, 7, "2026-08-25")).count == 25
        await s.close()

    run(scenario())


def test_mark_warned_is_once_per_day(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        await s.increment(-1, 7, "2026-08-25")
        assert await s.mark_warned(-1, 7, "2026-08-25") is True
        assert await s.mark_warned(-1, 7, "2026-08-25") is False
        assert (await s.get_state(-1, 7, "2026-08-25")).warned is True
        assert (await s.get_state(-1, 7, "2026-08-26")).warned is False
        await s.close()

    run(scenario())


def test_leaderboard_and_reset(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        for _ in range(3):
            await s.increment(-1, 10, "2026-08-25")
        await s.increment(-1, 11, "2026-08-25")
        assert await s.leaderboard(-1, "2026-08-25") == [(10, 3), (11, 1)]
        assert await s.reset_chat_day(-1, "2026-08-25") == 2
        assert await s.leaderboard(-1, "2026-08-25") == []
        await s.close()

    run(scenario())


def test_purge_old_keeps_recent(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        today = datetime.now(TZ).date()
        await s.increment(-1, 1, (today - timedelta(days=90)).isoformat())
        await s.increment(-1, 1, today.isoformat())
        assert await s.purge_old(30) == 1
        assert (await s.get_state(-1, 1, today.isoformat())).count == 1
        assert await s.purge_old(0) == 0
        await s.close()

    run(scenario())


def test_verdict_cache_roundtrip(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        assert await s.get_verdict("sticker:abc") is None
        await s.put_verdict("sticker:abc", True, 0.91, "reaction sticker")
        cached = await s.get_verdict("sticker:abc")
        assert cached.is_meme is True and cached.confidence == 0.91
        await s.put_verdict("sticker:abc", False, 0.2, "changed my mind")
        assert (await s.get_verdict("sticker:abc")).is_meme is False
        await s.close()

    run(scenario())


def test_kv_roundtrip(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        assert await s.get_kv("warning") is None
        await s.set_kv("warning", "file-id-1")
        await s.set_kv("warning", "file-id-2")
        assert await s.get_kv("warning") == "file-id-2"
        await s.close()

    run(scenario())


def test_day_key_uses_configured_timezone(tmp_path):
    s = fresh(tmp_path)
    # 22:30 UTC is already the next day in Moscow (UTC+3).
    moment = datetime(2026, 8, 25, 22, 30, tzinfo=timezone.utc)
    assert s.today(moment) == "2026-08-26"
    run(s.close())


def test_increment_and_claim_is_atomic(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        # Below the threshold nobody claims the warning.
        for _ in range(5):
            count, claimed = await s.increment_and_claim(-1, 7, "2026-08-25", 6)
            assert claimed is False
        assert count == 5

        count, claimed = await s.increment_and_claim(-1, 7, "2026-08-25", 6)
        assert (count, claimed) == (6, True)
        # The claim is exclusive: later memes count but do not claim again.
        count, claimed = await s.increment_and_claim(-1, 7, "2026-08-25", 6)
        assert (count, claimed) == (7, False)
        await s.close()

    run(scenario())


def test_exactly_one_concurrent_meme_claims_the_warning(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        results = await asyncio.gather(
            *(s.increment_and_claim(-1, 7, "2026-08-25", 6) for _ in range(20))
        )
        counts = sorted(c for c, _ in results)
        assert counts == list(range(1, 21))          # no increment was lost
        claimants = [c for c, claimed in results if claimed]
        assert claimants == [6]                       # the 6th meme, and only it
        await s.close()

    run(scenario())


def test_clear_warned_allows_a_retry(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        await s.increment(-1, 7, "2026-08-25")
        assert await s.mark_warned(-1, 7, "2026-08-25") is True
        await s.clear_warned(-1, 7, "2026-08-25")
        assert (await s.get_state(-1, 7, "2026-08-25")).warned is False
        assert await s.mark_warned(-1, 7, "2026-08-25") is True
        await s.close()

    run(scenario())


def test_purge_old_also_evicts_stale_cached_verdicts(tmp_path):
    s = fresh(tmp_path)

    async def scenario():
        await s.put_verdict("ns:fresh", True, 0.9, "joke")
        await s.db.execute(
            "UPDATE verdict_cache SET created_at = ? WHERE cache_key = ?",
            ("2020-01-01T00:00:00+03:00", "ns:fresh"),
        )
        await s.put_verdict("ns:recent", False, 0.9, "dashboard")
        await s.db.commit()

        assert await s.purge_old(30) == 1
        assert await s.get_verdict("ns:fresh") is None
        assert await s.get_verdict("ns:recent") is not None
        await s.close()

    run(scenario())

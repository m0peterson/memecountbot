"""SQLite-backed daily counters, warning bookkeeping and verdict cache."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS meme_counts (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    warned_at   TEXT,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (chat_id, user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_meme_counts_day ON meme_counts(day);

CREATE TABLE IF NOT EXISTS verdict_cache (
    cache_key  TEXT PRIMARY KEY,
    is_meme    INTEGER NOT NULL,
    confidence REAL    NOT NULL DEFAULT 0,
    reason     TEXT,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class DailyState:
    """Snapshot of one user's state in one chat for the current day."""

    count: int
    warned: bool


@dataclass(frozen=True)
class CachedVerdict:
    is_meme: bool
    confidence: float
    reason: str


def day_key(tz: ZoneInfo, moment: datetime | None = None) -> str:
    """Return the YYYY-MM-DD bucket a message belongs to, in the configured timezone."""
    now = moment.astimezone(tz) if moment else datetime.now(tz)
    return now.date().isoformat()


class Storage:
    """Tiny async wrapper around a single SQLite connection.

    All writes go through one lock so concurrent updates (several memes arriving
    at once) cannot lose an increment.
    """

    def __init__(self, path: str, tz: ZoneInfo) -> None:
        self._path = path
        self._tz = tz
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        parent = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() must be awaited before use")
        return self._db

    def today(self, moment: datetime | None = None) -> str:
        return day_key(self._tz, moment)

    # ------------------------------------------------------------------ counts

    async def get_state(self, chat_id: int, user_id: int, day: str) -> DailyState:
        async with self.db.execute(
            "SELECT count, warned_at FROM meme_counts WHERE chat_id=? AND user_id=? AND day=?",
            (chat_id, user_id, day),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return DailyState(count=0, warned=False)
        return DailyState(count=row["count"], warned=row["warned_at"] is not None)

    async def increment_and_claim(
        self, chat_id: int, user_id: int, day: str, threshold: int
    ) -> tuple[int, bool]:
        """Count one meme and, in the same critical section, claim the warning.

        Returns ``(new_count, claimed)``. ``claimed`` is True only for the call
        that pushed the user to ``threshold`` first and found ``warned_at`` unset.
        Doing both under one lock is what keeps the warning attached to the
        message that actually crossed the limit when several arrive at once.
        """
        async with self._lock:
            count = await self._bump(chat_id, user_id, day)
            claimed = False
            if count >= threshold:
                claimed = await self._claim_warning(chat_id, user_id, day)
            await self.db.commit()
        return count, claimed

    async def increment(self, chat_id: int, user_id: int, day: str) -> int:
        """Increment the daily meme counter and return the new value."""
        async with self._lock:
            count = await self._bump(chat_id, user_id, day)
            await self.db.commit()
        return count

    async def mark_warned(self, chat_id: int, user_id: int, day: str) -> bool:
        """Flag the user as warned today. Returns False if already flagged."""
        async with self._lock:
            claimed = await self._claim_warning(chat_id, user_id, day)
            await self.db.commit()
            return claimed

    async def clear_warned(self, chat_id: int, user_id: int, day: str) -> None:
        """Give a claimed warning back, so a failed delivery can be retried."""
        async with self._lock:
            await self.db.execute(
                "UPDATE meme_counts SET warned_at = NULL "
                "WHERE chat_id=? AND user_id=? AND day=?",
                (chat_id, user_id, day),
            )
            await self.db.commit()

    async def _bump(self, chat_id: int, user_id: int, day: str) -> int:
        """Increment the counter and read it back. Caller holds the lock."""
        now = datetime.now(self._tz).isoformat(timespec="seconds")
        await self.db.execute(
            """
            INSERT INTO meme_counts (chat_id, user_id, day, count, updated_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chat_id, user_id, day)
            DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
            """,
            (chat_id, user_id, day, now),
        )
        async with self.db.execute(
            "SELECT count FROM meme_counts WHERE chat_id=? AND user_id=? AND day=?",
            (chat_id, user_id, day),
        ) as cur:
            row = await cur.fetchone()
        return int(row["count"]) if row else 1

    async def _claim_warning(self, chat_id: int, user_id: int, day: str) -> bool:
        """Set ``warned_at`` if it is unset. Caller holds the lock and commits."""
        now = datetime.now(self._tz).isoformat(timespec="seconds")
        cur = await self.db.execute(
            """
            UPDATE meme_counts SET warned_at = ?
            WHERE chat_id=? AND user_id=? AND day=? AND warned_at IS NULL
            """,
            (now, chat_id, user_id, day),
        )
        return cur.rowcount > 0

    async def leaderboard(self, chat_id: int, day: str, limit: int = 10) -> list[tuple[int, int]]:
        async with self.db.execute(
            """
            SELECT user_id, count FROM meme_counts
            WHERE chat_id=? AND day=? AND count > 0
            ORDER BY count DESC, user_id ASC LIMIT ?
            """,
            (chat_id, day, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [(int(r["user_id"]), int(r["count"])) for r in rows]

    async def reset_chat_day(self, chat_id: int, day: str) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "DELETE FROM meme_counts WHERE chat_id=? AND day=?", (chat_id, day)
            )
            await self.db.commit()
            return cur.rowcount

    async def purge_old(self, retention_days: int) -> int:
        """Drop counters and cached verdicts older than the retention window.

        ``created_at`` is an ISO timestamp, so comparing it to a ``YYYY-MM-DD``
        cutoff string is a correct date comparison.
        """
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(self._tz).date() - timedelta(days=retention_days)).isoformat()
        async with self._lock:
            counts = await self.db.execute("DELETE FROM meme_counts WHERE day < ?", (cutoff,))
            removed = counts.rowcount
            cache = await self.db.execute(
                "DELETE FROM verdict_cache WHERE created_at < ?", (cutoff,)
            )
            removed += cache.rowcount
            await self.db.commit()
            return removed

    # ------------------------------------------------------------------- cache

    async def get_verdict(self, cache_key: str) -> CachedVerdict | None:
        async with self.db.execute(
            "SELECT is_meme, confidence, reason FROM verdict_cache WHERE cache_key=?",
            (cache_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return CachedVerdict(
            is_meme=bool(row["is_meme"]),
            confidence=float(row["confidence"]),
            reason=row["reason"] or "cached",
        )

    async def put_verdict(
        self, cache_key: str, is_meme: bool, confidence: float, reason: str
    ) -> None:
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO verdict_cache (cache_key, is_meme, confidence, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    is_meme = excluded.is_meme,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (cache_key, int(is_meme), confidence, reason[:500], datetime.now(self._tz).isoformat()),
            )
            await self.db.commit()

    # ---------------------------------------------------------------------- kv

    async def get_kv(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self.db.commit()

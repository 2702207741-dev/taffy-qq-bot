"""Persisted validated daily broad-index closes with idempotent backfill.

The broad daily-index chart is fetched live for temperature calculations but
was never persisted.  This module stores one row per local date so the
``抄底`` command can run percentile and walk-forward scenario checks over a
growing sample instead of depending on a single live response.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import project_path_setting
from .local_store import connect_local_database
from .market_temperature import DailyChartFetcher, fetch_daily_chart, parse_daily_closes

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_HISTORY_PATH = PROJECT_ROOT / "data" / "market_history.db"
_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_DAILY_CHART_SOURCE = "steamdt-daily-chart"
_BACKFILL_MIN_INTERVAL_SECONDS = 3600

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS index_daily_closes (
    close_date TEXT PRIMARY KEY NOT NULL
        CHECK (close_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    close_value REAL NOT NULL CHECK (close_value > 0),
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 80),
    fetched_at INTEGER NOT NULL CHECK (fetched_at > 0)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_collection_state (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
) WITHOUT ROWID;
"""


class IndexHistoryStore:
    """SQLite-backed daily index closes shared with the market history file."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        fetcher: DailyChartFetcher = fetch_daily_chart,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = (
            path or project_path_setting("TAFFY_MARKET_HISTORY_DB", DEFAULT_INDEX_HISTORY_PATH)
        ).resolve()
        self._fetcher = fetcher
        self._clock = wall_clock
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        return connect_local_database(
            self.path, timeout_seconds=5.0, label="index history database"
        )

    async def initialize(self) -> None:
        if self._initialized:
            return

        def create() -> None:
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA)
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(create)
        self._initialized = True

    def _today(self, now: float) -> date:
        try:
            return datetime.fromtimestamp(now, _MARKET_TIMEZONE).date()
        except (OSError, OverflowError, ValueError):
            return date.today()

    async def latest_date(self) -> date | None:
        await self.initialize()

        def read() -> str | None:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT MAX(close_date) FROM index_daily_closes"
                ).fetchone()
                return str(row[0]) if row and row[0] is not None else None
            finally:
                connection.close()

        raw = await asyncio.to_thread(read)
        return date.fromisoformat(raw) if raw else None

    async def upsert_closes(
        self,
        closes: Mapping[date, float],
        *,
        source: str = _DAILY_CHART_SOURCE,
        now: int | None = None,
    ) -> int:
        """Insert or refresh daily closes with the day lifecycle.

        The observation day is ``now`` (default: the injected clock) in
        UTC+8 — a request that straddles midnight keeps classifying by its
        start time.  Rows and their ``pending_close:<date>`` marker are
        written in one transaction:

        * the current day may be inserted/refreshed and stays pending;
        * a past date with no row is first inserted as a local final value;
        * a past date with a pending marker is updated once (finalized) and
          the marker is deleted;
        * a past date with a row and no pending marker keeps its close
          value forever (frozen — truncated responses and force cannot
          rewrite it);
        * a pending date missing from the response keeps value and marker;
        * a future date raises and nothing is written.
        """

        await self.initialize()
        observed_now = int(now if now is not None else self._clock())
        observed = self._today(observed_now)

        def persist() -> int:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if closes:
                    for close_date, close in closes.items():
                        day_raw = close_date.isoformat()
                        if close_date > observed:
                            raise ValueError(f"daily close date {day_raw} is in the future")
                        pending_key = f"pending_close:{day_raw}"
                        marker = connection.execute(
                            "SELECT value FROM index_collection_state WHERE key = ?",
                            (pending_key,),
                        ).fetchone()
                        is_current = close_date == observed
                        has_pending = marker is not None
                        if has_pending and not is_current:
                            # Past date with a pending marker: accept this
                            # response once, then finalize (delete marker).
                            connection.execute(
                                """
                                INSERT INTO index_daily_closes
                                    (close_date, close_value, source, fetched_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(close_date) DO UPDATE SET
                                    close_value = excluded.close_value,
                                    source = excluded.source,
                                    fetched_at = excluded.fetched_at
                                """,
                                (day_raw, float(close), source, observed_now),
                            )
                            connection.execute(
                                "DELETE FROM index_collection_state WHERE key = ?",
                                (pending_key,),
                            )
                        elif is_current:
                            # Current day: insert or refresh in place, keep
                            # the pending marker so a later response can
                            # finalize it.
                            connection.execute(
                                """
                                INSERT INTO index_daily_closes
                                    (close_date, close_value, source, fetched_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(close_date) DO UPDATE SET
                                    close_value = excluded.close_value,
                                    source = excluded.source,
                                    fetched_at = excluded.fetched_at
                                """,
                                (day_raw, float(close), source, observed_now),
                            )
                            connection.execute(
                                """
                                INSERT INTO index_collection_state (key, value, updated_at)
                                VALUES (?, '1', ?)
                                ON CONFLICT(key) DO UPDATE SET
                                    value = excluded.value,
                                    updated_at = excluded.updated_at
                                """,
                                (pending_key, observed_now),
                            )
                        else:
                            # Past date without a pending marker: frozen.
                            # First sight inserts it as a local final value;
                            # later sights keep the stored close and only
                            # refresh provenance fields.
                            connection.execute(
                                """
                                INSERT INTO index_daily_closes
                                    (close_date, close_value, source, fetched_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(close_date) DO UPDATE SET
                                    source = excluded.source,
                                    fetched_at = excluded.fetched_at
                                """,
                                (day_raw, float(close), source, observed_now),
                            )
                connection.execute(
                    """
                    INSERT INTO index_collection_state (key, value, updated_at)
                    VALUES ('last_backfill_attempt', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (str(observed_now), observed_now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return len(closes)

        return await asyncio.to_thread(persist)

    async def load_closes(self) -> tuple[tuple[date, float], ...]:
        """Return strictly date-ordered finalized (date, close) rows.

        Only dates before the current UTC+8 day that carry no pending
        marker are served; intraday and unfinalized rows are excluded so a
        consumer never mistakes a live value for a close.
        """

        await self.initialize()

        def read() -> list[tuple[date, float]]:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT c.close_date, c.close_value
                    FROM index_daily_closes AS c
                    WHERE c.close_date < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM index_collection_state AS s
                          WHERE s.key = 'pending_close:' || c.close_date
                      )
                    ORDER BY c.close_date
                    """,
                    (self._today(int(self._clock())).isoformat(),),
                ).fetchall()
                return [
                    (date.fromisoformat(str(row[0])), float(row[1]))
                    for row in rows
                    if row[0] is not None and row[1] is not None
                ]
            finally:
                connection.close()

        return tuple(await asyncio.to_thread(read))

    async def backfill_if_due(self, *, force: bool = False) -> int:
        """Fetch and persist the daily chart when missing or stale.

        Failing attempts are throttled so the operations loop cannot hammer the
        upstream chart endpoint every 65 seconds.
        """

        await self.initialize()
        async with self._lock:
            now = int(self._clock())

            def state() -> int:
                connection = self._connect()
                try:
                    attempt = connection.execute(
                        """
                        SELECT value FROM index_collection_state
                        WHERE key = 'last_backfill_attempt'
                        """
                    ).fetchone()
                    return int(attempt[0]) if attempt and attempt[0] is not None else 0
                finally:
                    connection.close()

            last_attempt = await asyncio.to_thread(state)
            if not force and now - last_attempt < _BACKFILL_MIN_INTERVAL_SECONDS:
                return 0

            def record_attempt() -> None:
                connection = self._connect()
                try:
                    connection.execute(
                        """
                        INSERT INTO index_collection_state (key, value, updated_at)
                        VALUES ('last_backfill_attempt', ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at
                        """,
                        (str(now), now),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

            await asyncio.to_thread(record_attempt)
            payload = await self._fetcher()
            closes = parse_daily_closes(payload)
            # Classify by the request-start time captured before the fetch:
            # a response that arrives after midnight must not finalize the
            # day it started on.
            return await self.upsert_closes(closes, now=now)


index_history_store = IndexHistoryStore()


__all__ = [
    "IndexHistoryStore",
    "index_history_store",
]

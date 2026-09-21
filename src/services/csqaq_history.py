"""CSQAQ snapshot accumulation (independent store, zero main-DB migration).

CSQAQ exposes no history API: every detail fetch is a point-in-time
snapshot.  When ``TAFFY_CSQAQ_HISTORY_ENABLED`` is on, successful fetches
leave one row per item per market day (spread ratio, rent yield, sell
depth), so 比价 can show a short honest trend instead of a single moment.
The store is a standalone SQLite file (hoard-index precedent) — no
``taffy.db`` migration, no downtime.  All writes are fail-open: a
recording failure only logs, it never breaks the query it rode along with.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import env_bool, project_path_setting
from .local_store import connect_local_database

logger = logging.getLogger("taffy.csqaq_history")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "data" / "csqaq_history.db"
_MARKET_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
_RETENTION_DAYS = 180
_TREND_DAYS = 7
#: Spread denominator basis: SteamDT realtime sell (compare path).
BASIS_COMPARE = "compare"
#: Spread denominator basis: item daily close (scoring path).
BASIS_SCORE = "score"

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS csqaq_snapshots (
    good_id INTEGER NOT NULL CHECK (good_id > 0),
    market_name TEXT NOT NULL CHECK (length(market_name) BETWEEN 1 AND 240),
    snap_date TEXT NOT NULL CHECK (snap_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    spread_ratio REAL CHECK (spread_ratio IS NULL OR spread_ratio > 0),
    rent_annual REAL NULL,
    sell_total INTEGER CHECK (sell_total IS NULL OR sell_total >= 0),
    basis TEXT NOT NULL CHECK (basis IN ('compare', 'score')),
    fetched_at INTEGER NOT NULL CHECK (fetched_at > 0),
    PRIMARY KEY (good_id, snap_date, basis)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_csqaq_snapshots_name
    ON csqaq_snapshots (market_name, snap_date);
"""


def history_enabled() -> bool:
    """Kill switch (default off); recording and trend both honor it."""

    return env_bool("TAFFY_CSQAQ_HISTORY_ENABLED", False)


def _today() -> str:
    try:
        return datetime.now(_MARKET_TZ).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return datetime.now(timezone.utc).date().isoformat()


class CsqaqHistoryStore:
    """One-row-per-item-per-day CSQAQ snapshot log."""

    def __init__(
        self,
        path: Path | None = None,
    ) -> None:
        self.path = (
            path or project_path_setting("TAFFY_CSQAQ_HISTORY_DB", DEFAULT_HISTORY_PATH)
        ).resolve()
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        return connect_local_database(
            self.path, timeout_seconds=5.0, label="csqaq history database"
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

    async def record_snapshot(
        self,
        good_id: int,
        market_name: str,
        *,
        spread_ratio: float | None = None,
        rent_annual: float | None = None,
        sell_total: int | None = None,
        basis: str = BASIS_COMPARE,
        day: str | None = None,
    ) -> bool:
        """Upsert today's snapshot; ``False`` when disabled/empty/failed."""

        if not history_enabled():
            return False
        name = (market_name or "").strip()
        if good_id <= 0 or not name:
            return False
        if spread_ratio is None and rent_annual is None and sell_total is None:
            return False
        if basis not in (BASIS_COMPARE, BASIS_SCORE):
            return False
        snap_date = day or _today()
        now = int(time.time())
        cutoff = (datetime.now(_MARKET_TZ).date() - timedelta(days=_RETENTION_DAYS)).isoformat()
        await self.initialize()

        def persist() -> None:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO csqaq_snapshots
                        (good_id, market_name, snap_date, spread_ratio,
                         rent_annual, sell_total, basis, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(good_id, snap_date, basis) DO UPDATE SET
                        spread_ratio = excluded.spread_ratio,
                        rent_annual = excluded.rent_annual,
                        sell_total = excluded.sell_total,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        good_id,
                        name,
                        snap_date,
                        spread_ratio,
                        rent_annual,
                        sell_total,
                        basis,
                        now,
                    ),
                )
                connection.execute("DELETE FROM csqaq_snapshots WHERE snap_date < ?", (cutoff,))
                connection.commit()
            finally:
                connection.close()

        try:
            async with self._lock:
                await asyncio.to_thread(persist)
        except Exception as exc:
            logger.warning("CSQAQ snapshot record failed error=%s", type(exc).__name__)
            return False
        return True

    async def spread_series(
        self,
        market_name: str,
        *,
        basis: str = BASIS_COMPARE,
        days: int = _TREND_DAYS,
    ) -> list[tuple[str, float]]:
        """Recent ``(date, spread)`` points; empty when disabled or thin."""

        if not history_enabled():
            return []
        name = (market_name or "").strip()
        if not name:
            return []
        await self.initialize()
        cutoff = (datetime.now(_MARKET_TZ).date() - timedelta(days=max(1, days))).isoformat()

        def read() -> list[tuple[str, float]]:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT snap_date, spread_ratio FROM csqaq_snapshots
                    WHERE market_name = ? AND basis = ?
                      AND spread_ratio IS NOT NULL AND snap_date >= ?
                    ORDER BY snap_date
                    """,
                    (name, basis, cutoff),
                ).fetchall()
            finally:
                connection.close()
            return [(str(row[0]), float(row[1])) for row in rows if row[1] is not None]

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            logger.warning("CSQAQ spread series read failed error=%s", type(exc).__name__)
            return []

    async def render_trend_line(self, market_name: str, *, days: int = _TREND_DAYS) -> str | None:
        """One honest spread-trend line, or ``None`` below two points."""

        series = await self.spread_series(market_name, days=days)
        if len(series) < 2:
            return None
        first = series[0][1]
        last = series[-1][1]
        return f"📉 价差趋势：{first:.3f}→{last:.3f}（近 {len(series)} 次快照）"


csqaq_history_store = CsqaqHistoryStore()


__all__ = [
    "BASIS_COMPARE",
    "BASIS_SCORE",
    "CsqaqHistoryStore",
    "csqaq_history_store",
    "history_enabled",
]

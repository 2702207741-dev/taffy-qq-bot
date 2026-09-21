"""Privacy-safe five-minute operational aggregates.

No caller identifiers, query text, Steam IDs, item names, or replies are
accepted by this API or persisted to disk.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import project_path_setting
from .local_store import connect_local_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TELEMETRY_PATH = PROJECT_ROOT / "data" / "telemetry.db"
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,39}\Z")
_SENSITIVE_OPERATION_RE = re.compile(r"(?:\d{5,}|user|group|steam|qq)", re.IGNORECASE)
_OUTCOMES = frozenset({"success", "refused", "timeout", "error"})
_RETENTION_SECONDS = 14 * 24 * 60 * 60

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS command_telemetry (
    bucket_at INTEGER NOT NULL CHECK (bucket_at >= 0 AND bucket_at % 300 = 0),
    operation TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 40),
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'refused', 'timeout', 'error')),
    cache_hit INTEGER NOT NULL CHECK (cache_hit IN (0, 1)),
    count INTEGER NOT NULL CHECK (count > 0),
    latency_sum_ms INTEGER NOT NULL CHECK (latency_sum_ms >= 0),
    le_50 INTEGER NOT NULL CHECK (le_50 >= 0),
    le_200 INTEGER NOT NULL CHECK (le_200 >= 0),
    le_800 INTEGER NOT NULL CHECK (le_800 >= 0),
    le_2000 INTEGER NOT NULL CHECK (le_2000 >= 0),
    le_5000 INTEGER NOT NULL CHECK (le_5000 >= 0),
    gt_5000 INTEGER NOT NULL CHECK (gt_5000 >= 0),
    PRIMARY KEY (bucket_at, operation, outcome, cache_hit)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_command_telemetry_operation
    ON command_telemetry (operation, bucket_at);
"""


@dataclass(slots=True)
class _Aggregate:
    count: int = 0
    latency_sum_ms: int = 0
    le_50: int = 0
    le_200: int = 0
    le_800: int = 0
    le_2000: int = 0
    le_5000: int = 0
    gt_5000: int = 0

    def add(self, latency_ms: float) -> None:
        value = max(0, min(120_000, round(latency_ms)))
        self.count += 1
        self.latency_sum_ms += value
        if value <= 50:
            self.le_50 += 1
        elif value <= 200:
            self.le_200 += 1
        elif value <= 800:
            self.le_800 += 1
        elif value <= 2_000:
            self.le_2000 += 1
        elif value <= 5_000:
            self.le_5000 += 1
        else:
            self.gt_5000 += 1


@dataclass(frozen=True, slots=True)
class OperationSummary:
    operation: str
    total: int
    successes: int
    refused: int
    timeouts: int
    errors: int
    cache_hits: int
    average_ms: float
    p95_upper_ms: int

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0


class TelemetryRecorder:
    def __init__(
        self,
        path: Path | None = None,
        *,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = (
            path or project_path_setting("TAFFY_TELEMETRY_DB", DEFAULT_TELEMETRY_PATH)
        ).resolve()
        self._clock = wall_clock
        self._pending: dict[tuple[int, str, str, int], _Aggregate] = defaultdict(_Aggregate)
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        return connect_local_database(self.path, timeout_seconds=3.0, label="telemetry database")

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

    async def record(
        self,
        operation: str,
        outcome: str,
        latency_ms: float,
        *,
        cache_hit: bool = False,
    ) -> None:
        if (
            _OPERATION_RE.fullmatch(operation) is None
            or _SENSITIVE_OPERATION_RE.search(operation) is not None
        ):
            raise ValueError("telemetry operation is not allowlisted syntax")
        if outcome not in _OUTCOMES:
            raise ValueError("telemetry outcome is invalid")
        bucket = int(self._clock()) // 300 * 300
        async with self._lock:
            self._pending[(bucket, operation, outcome, int(cache_hit))].add(latency_ms)

    async def flush(self) -> int:
        await self.initialize()
        async with self._lock:
            if not self._pending:
                return 0
            pending = self._pending
            self._pending = defaultdict(_Aggregate)

        rows = [
            (
                *key,
                aggregate.count,
                aggregate.latency_sum_ms,
                aggregate.le_50,
                aggregate.le_200,
                aggregate.le_800,
                aggregate.le_2000,
                aggregate.le_5000,
                aggregate.gt_5000,
            )
            for key, aggregate in pending.items()
        ]

        def write() -> None:
            connection = self._connect()
            try:
                connection.executemany(
                    """
                    INSERT INTO command_telemetry (
                        bucket_at, operation, outcome, cache_hit, count, latency_sum_ms,
                        le_50, le_200, le_800, le_2000, le_5000, gt_5000
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket_at, operation, outcome, cache_hit) DO UPDATE SET
                        count = count + excluded.count,
                        latency_sum_ms = latency_sum_ms + excluded.latency_sum_ms,
                        le_50 = le_50 + excluded.le_50,
                        le_200 = le_200 + excluded.le_200,
                        le_800 = le_800 + excluded.le_800,
                        le_2000 = le_2000 + excluded.le_2000,
                        le_5000 = le_5000 + excluded.le_5000,
                        gt_5000 = gt_5000 + excluded.gt_5000
                    """,
                    rows,
                )
                cutoff = int(self._clock()) - _RETENTION_SECONDS
                connection.execute("DELETE FROM command_telemetry WHERE bucket_at < ?", (cutoff,))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        try:
            await asyncio.to_thread(write)
        except Exception:
            async with self._lock:
                field_names = (
                    "count",
                    "latency_sum_ms",
                    "le_50",
                    "le_200",
                    "le_800",
                    "le_2000",
                    "le_5000",
                    "gt_5000",
                )
                for key, aggregate in pending.items():
                    target = self._pending[key]
                    for field in field_names:
                        setattr(target, field, getattr(target, field) + getattr(aggregate, field))
            raise
        return len(rows)

    async def summary(self, *, hours: int = 24) -> tuple[OperationSummary, ...]:
        await self.flush()
        cutoff = int(self._clock()) - max(1, min(hours, 336)) * 3600

        def read() -> tuple[OperationSummary, ...]:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT operation,
                           SUM(count) AS total,
                           SUM(CASE WHEN outcome = 'success' THEN count ELSE 0 END) successes,
                           SUM(CASE WHEN outcome = 'refused' THEN count ELSE 0 END) refused,
                           SUM(CASE WHEN outcome = 'timeout' THEN count ELSE 0 END) timeouts,
                           SUM(CASE WHEN outcome = 'error' THEN count ELSE 0 END) errors,
                           SUM(CASE WHEN cache_hit = 1 THEN count ELSE 0 END) cache_hits,
                           SUM(latency_sum_ms) AS latency_sum_ms,
                           SUM(le_50) AS le_50, SUM(le_200) AS le_200,
                           SUM(le_800) AS le_800, SUM(le_2000) AS le_2000,
                           SUM(le_5000) AS le_5000, SUM(gt_5000) AS gt_5000
                    FROM command_telemetry
                    WHERE bucket_at >= ?
                    GROUP BY operation ORDER BY operation
                    """,
                    (cutoff,),
                ).fetchall()
            finally:
                connection.close()
            summaries: list[OperationSummary] = []
            bounds = (50, 200, 800, 2_000, 5_000, 120_000)
            for row in rows:
                total = int(row["total"])
                threshold = max(1, int(total * 0.95 + 0.9999))
                cumulative = 0
                upper = 120_000
                for bound, column in zip(
                    bounds,
                    ("le_50", "le_200", "le_800", "le_2000", "le_5000", "gt_5000"),
                    strict=True,
                ):
                    cumulative += int(row[column])
                    if cumulative >= threshold:
                        upper = bound
                        break
                summaries.append(
                    OperationSummary(
                        str(row["operation"]),
                        total,
                        int(row["successes"]),
                        int(row["refused"]),
                        int(row["timeouts"]),
                        int(row["errors"]),
                        int(row["cache_hits"]),
                        int(row["latency_sum_ms"]) / total if total else 0.0,
                        upper,
                    )
                )
            return tuple(summaries)

        return await asyncio.to_thread(read)


telemetry = TelemetryRecorder()


__all__ = ["OperationSummary", "TelemetryRecorder", "telemetry"]

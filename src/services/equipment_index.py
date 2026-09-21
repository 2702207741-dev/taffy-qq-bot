"""Equipment price index (V0.5 night batch 4).

Publishes an equal-weight log price index over a **fixed monthly component
list** once the readiness gate is met: at least 200 fixed components with
90% coverage (a component counts as covered when its latest hourly bucket
is fresh).  Base 100 at the component list's base day.

Honest gating: before the gate is met the command reports real progress
(component count and coverage) instead of inventing an index — the same
discipline as the ranking boards.

The component table lives in ``market_history.db`` via the idempotent
``CREATE TABLE IF NOT EXISTS`` schema (no main-db migration needed).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from .market_history import MarketHistoryStore

#: Readiness gate for publishing the index (FEATURES.md contract).
MIN_COMPONENTS = 200
MIN_COVERAGE_RATIO = 0.90
MAX_COMPONENTS = 400


@dataclass(frozen=True, slots=True)
class EquipmentIndexStatus:
    components: int
    covered: int
    coverage: float  # 0..1
    ready: bool
    base_day: str  # "YYYY-MM-DD" or ""
    latest_value: float | None  # None until ready


def _market_day(timestamp: int) -> str:
    """Market-timezone day (UTC+8) for a bucket timestamp, as ISO date."""

    from datetime import datetime, timedelta, timezone

    return (datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=8)).strftime(
        "%Y-%m-%d"
    )


class EquipmentIndexService:
    """Read-only index computation over the shared history database."""

    def __init__(self, store: MarketHistoryStore | None = None) -> None:
        self._store = store or MarketHistoryStore()

    async def _read_components(self) -> list[tuple[str, str, float]]:
        """Return ``[(market_name, base_day, base_price)]`` from the table."""

        await self._store.initialize()

        def read() -> list[tuple[str, str, float]]:
            connection = self._store._connect()
            try:
                rows = connection.execute(
                    "SELECT market_name, base_day, base_price FROM equipment_index_components"
                ).fetchall()
            finally:
                connection.close()
            return [(str(row[0]), str(row[1]), float(row[2])) for row in rows]

        return await asyncio.to_thread(read)

    async def status(self) -> EquipmentIndexStatus:
        """Readiness gate + current value; honest progress when not ready."""

        components = await self._read_components()
        if not components:
            return EquipmentIndexStatus(0, 0, 0.0, False, "", None)
        base_day = components[0][1]

        async def count_covered() -> int:
            await self._store.initialize()

            def read() -> int:
                connection = self._store._connect()
                try:
                    latest = connection.execute(
                        "SELECT MAX(bucket_at) FROM quote_snapshots"
                    ).fetchone()
                    if latest is None or latest[0] is None:
                        return 0
                    latest_bucket = int(latest[0])
                    names = [name for name, _, _ in components]
                    placeholders = ",".join("?" * len(names))
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) FROM quote_snapshots
                        WHERE bucket_at = ? AND market_name IN ({placeholders})
                        """,
                        (latest_bucket, *names),
                    ).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    connection.close()

            return await asyncio.to_thread(read)

        covered = await count_covered()
        coverage = covered / len(components)
        ready = (
            len(components) >= MIN_COMPONENTS
            and covered >= MIN_COMPONENTS
            and coverage >= MIN_COVERAGE_RATIO
        )
        latest_value = await self._compute_value(components) if ready else None
        return EquipmentIndexStatus(
            components=len(components),
            covered=covered,
            coverage=coverage,
            ready=ready,
            base_day=base_day,
            latest_value=latest_value,
        )

    async def _compute_value(self, components: list[tuple[str, str, float]]) -> float | None:
        """Equal-weight log index: exp(mean(log(latest / base))) × 100.

        ``base`` is the price recorded when the component list was built;
        a component contributes only when its latest bucket is available.
        The mean runs over the actually covered components.
        """

        await self._store.initialize()

        def read() -> float | None:
            connection = self._store._connect()
            try:
                latest = connection.execute("SELECT MAX(bucket_at) FROM quote_snapshots").fetchone()
                if latest is None or latest[0] is None:
                    return None
                latest_bucket = int(latest[0])
                logs: list[float] = []
                for name, _base_day, base_price in components:
                    row = connection.execute(
                        """
                        SELECT sell_price FROM quote_snapshots
                        WHERE market_name = ? AND bucket_at = ?
                        """,
                        (name, latest_bucket),
                    ).fetchone()
                    if row is None:
                        continue
                    latest_price = float(row[0])
                    if not latest_price > 0 or not base_price > 0:
                        continue
                    logs.append(math.log(latest_price / base_price))
                if len(logs) < MIN_COMPONENTS:
                    return None
                return math.exp(sum(logs) / len(logs)) * 100.0
            finally:
                connection.close()

        return await asyncio.to_thread(read)


def render_equipment_index(status: EquipmentIndexStatus) -> str:
    """Render the index card; honest progress before the gate is met."""

    if not status.ready or status.latest_value is None:
        return (
            "🎯 【装备指数】\n──────────\n"
            f"⏳ 固定成分与覆盖仍在建立：固定成分 {status.components} 件"
            f"（门槛 ≥{MIN_COMPONENTS}），最新桶有效覆盖 {status.covered} 件"
            f"（覆盖 {status.coverage * 100:.0f}%，门槛 ≥{MIN_COVERAGE_RATIO * 100:.0f}%）。\n"
            "达到门槛后才会以基期 100 发布等权对数价格指数。"
        )
    change = status.latest_value - 100.0
    return (
        "🎯 【装备指数】\n──────────\n"
        f"基期 100（{status.base_day}），当前 {status.latest_value:.2f}"
        f"（{change:+.2f}，{change / 100 * 100:+.2f}%）\n"
        f"固定成分 {status.components} 件，最新桶有效覆盖 {status.covered} 件"
        f"（{status.coverage * 100:.0f}%）。\n"
        "等权对数价格指数，成分按月固定；历史数据不构成收益承诺。"
    )

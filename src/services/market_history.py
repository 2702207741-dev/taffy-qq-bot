"""Rebuildable validated hourly market history and phased report gates."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import statistics
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import bounded_env_int, project_path_setting
from .equipment_rules import (
    EQUIPMENT_FALLBACK_MAX_AGE_SECONDS,
    EQUIPMENT_ORDER_BY,
)
from .local_store import connect_local_database
from .market import st_post
from .market_catalog_store import MarketCatalogStore, market_catalog_store
from .market_categories import CategorySpec, category_hint, resolve_category
from .market_validation import PRICE_MAX_AGE_SECONDS, mapping_timestamp
from .resource_governor import resource_governor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "data" / "market_history.db"
_BATCH_PATH = "/open/cs2/v1/price/batch"
_MIN_BATCH_INTERVAL = 61
_BATCH_SIZE = 100
_SAMPLE_POOL_SIZE = bounded_env_int("TAFFY_MARKET_SAMPLE_POOL_SIZE", 2_500, 500, 5_000)
_MIN_COLUMN_ITEMS = 500
_MIN_CATEGORY_ITEMS = 5
_RANKING_TITLES = {
    "异动": "🚨 【异动监测】",
    "涨跌榜": "📊 【涨跌榜】",
    "流动性榜": "💧 【流动性榜】",
}
_MIN_COVERAGE_RATIO = 0.80
_EQUIPMENT_COMPONENT_TARGET = 250
_MIN_CATALOG_ENTRIES = bounded_env_int(
    "TAFFY_MARKET_HISTORY_MIN_CATALOG",
    1_000,
    100,
    100_000,
)


def _market_day(timestamp: int) -> str:
    """Market-timezone (UTC+8) day of a bucket timestamp, as ISO date."""

    from datetime import datetime, timedelta, timezone

    return (datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=8)).strftime(
        "%Y-%m-%d"
    )


def _bucket_label(timestamp: int) -> str:
    """Market-timezone (UTC+8) hour label of a bucket timestamp."""

    from datetime import datetime, timedelta, timezone

    return (datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=8)).strftime(
        "%m-%d %H:00"
    )


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS quote_snapshots (
    bucket_at INTEGER NOT NULL CHECK (bucket_at >= 0 AND bucket_at % 3600 = 0),
    market_name TEXT NOT NULL CHECK (length(market_name) BETWEEN 1 AND 240),
    sell_price REAL NOT NULL CHECK (sell_price > 0),
    bid_price REAL CHECK (bid_price IS NULL OR bid_price > 0),
    sell_count INTEGER CHECK (sell_count IS NULL OR sell_count >= 0),
    bid_count INTEGER CHECK (bid_count IS NULL OR bid_count >= 0),
    data_time INTEGER NOT NULL CHECK (data_time > 0),
    PRIMARY KEY (bucket_at, market_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_quote_snapshots_name
    ON quote_snapshots (market_name, bucket_at);
CREATE TABLE IF NOT EXISTS collection_state (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS collection_runs (
    bucket_at INTEGER PRIMARY KEY NOT NULL,
    catalog_count INTEGER NOT NULL CHECK (catalog_count >= 0),
    requested_count INTEGER NOT NULL CHECK (requested_count >= 0),
    valid_count INTEGER NOT NULL CHECK (valid_count >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 120)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS equipment_index_components (
    market_name TEXT PRIMARY KEY NOT NULL
        CHECK (length(market_name) BETWEEN 1 AND 240),
    base_day TEXT NOT NULL CHECK (length(base_day) = 10),
    base_price REAL NOT NULL CHECK (base_price > 0),
    added_at INTEGER NOT NULL CHECK (added_at >= 0)
) WITHOUT ROWID;
"""


@dataclass(frozen=True, slots=True)
class HistoryStatus:
    catalog_count: int
    latest_count: int
    latest_bucket: int | None
    oldest_bucket: int | None
    hours_collected: float = 0.0
    ranking_overlap: int = 0

    @property
    def sample_count(self) -> int:
        """Current collection denominator stored in the legacy catalog_count column."""

        return self.catalog_count


@dataclass(frozen=True, slots=True)
class BreadthSnapshot:
    compared: int
    rising_ratio: float
    median_change: float
    sell_depth: int
    bid_depth: int
    latest_bucket: int


MarketPoster = Callable[[str, dict[str, Any] | None], Awaitable[Mapping[str, Any]]]


def _positive(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _count(value: object, *, positive: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < (1 if positive else 0):
        return None
    return value


def _quote_rows(item: Mapping[str, Any]) -> Sequence[object]:
    for key in ("dataList", "prices", "platformList", "quoteList", "priceList"):
        value = item.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return value
    return (item,)


def parse_batch_quotes(
    response: Mapping[str, Any],
    *,
    requested_names: set[str],
    now: float,
) -> list[tuple[str, float, float | None, int | None, int | None, int]]:
    if not response.get("success"):
        return []
    raw_data = response.get("data")
    raw_items: list[Mapping[str, Any]] = []
    if isinstance(raw_data, Mapping):
        for name, value in raw_data.items():
            if isinstance(value, Mapping):
                raw_items.append({"marketHashName": str(name), **value})
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                raw_items.append({"marketHashName": str(name), "prices": value})
    elif isinstance(raw_data, Sequence) and not isinstance(raw_data, (str, bytes)):
        raw_items.extend(item for item in raw_data if isinstance(item, Mapping))

    parsed: list[tuple[str, float, float | None, int | None, int | None, int]] = []
    seen: set[str] = set()
    container_time = mapping_timestamp(response)
    for item in raw_items:
        name = str(item.get("marketHashName") or item.get("market_name") or "").strip()
        if name not in requested_names or name in seen:
            continue
        candidates: list[tuple[int, float, float | None, int | None, int | None, int]] = []
        for quote in _quote_rows(item):
            if not isinstance(quote, Mapping):
                continue
            sell = _positive(quote.get("sellPrice"))
            timestamp = mapping_timestamp(quote) or mapping_timestamp(item) or container_time
            if (
                sell is None
                or timestamp is None
                or timestamp > now + 300
                or now - timestamp > PRICE_MAX_AGE_SECONDS
            ):
                continue
            bid = _positive(quote.get("biddingPrice"))
            if bid is not None and bid > sell * 1.05:
                bid = None
            sell_count = _count(quote.get("sellCount"))
            bid_count = _count(quote.get("biddingCount"), positive=True) if bid else None
            platform = str(quote.get("platform") or "")
            priority = 0 if platform == "YOUPIN" else 1 if platform == "BUFF" else 2
            candidates.append((priority, sell, bid, sell_count, bid_count, int(timestamp)))
        if not candidates:
            continue
        _, sell, bid, sell_count, bid_count, timestamp = min(
            candidates, key=lambda row: (row[0], row[1])
        )
        parsed.append((name, sell, bid, sell_count, bid_count, timestamp))
        seen.add(name)
    return parsed


class MarketHistoryStore:
    def __init__(
        self,
        *,
        path: Path | None = None,
        catalog: MarketCatalogStore = market_catalog_store,
        market_post: MarketPoster = st_post,
        wall_clock: Callable[[], float] = time.time,
        sample_pool_size: int = _SAMPLE_POOL_SIZE,
    ) -> None:
        self.path = (
            path or project_path_setting("TAFFY_MARKET_HISTORY_DB", DEFAULT_HISTORY_PATH)
        ).resolve()
        self.catalog = catalog
        self._market_post = market_post
        self._clock = wall_clock
        self._sample_pool_size = max(500, min(int(sample_pool_size), 5_000))
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        return connect_local_database(self.path, timeout_seconds=5.0, label="history database")

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

    async def collect_once(self) -> int:
        if resource_governor.game_running:
            return 0
        await self.initialize()
        async with self._lock:
            now = int(self._clock())

            def state() -> tuple[int, str]:
                connection = self._connect()
                try:
                    values = {
                        str(row[0]): str(row[1])
                        for row in connection.execute("SELECT key, value FROM collection_state")
                    }
                finally:
                    connection.close()
                # The cursor anchors on a market name instead of a numeric
                # offset: the sample ordering shifts whenever an item is
                # requested or refreshed, and a positional index would then
                # re-request whole batches or skip the tail systematically.
                return int(values.get("last_attempt_at", "0")), values.get("cursor_name", "")

            last_attempt, cursor_name = await asyncio.to_thread(state)
            if now - last_attempt < _MIN_BATCH_INTERVAL:
                return 0
            sample_provider = getattr(self.catalog, "sample_entries", None)
            if callable(sample_provider):
                entries = await sample_provider(self._sample_pool_size)
            else:
                entries = (await self.catalog.entries())[: self._sample_pool_size]
            if len(entries) < _MIN_CATALOG_ENTRIES:
                return 0
            names = [entry.market_name for entry in entries]
            # cursor_name is the first item of the next batch; if it vanished
            # from the sample, restart the sweep from the front.
            start = names.index(cursor_name) if cursor_name in names else 0
            selected = names[start : start + _BATCH_SIZE]
            if not selected:
                return 0
            following_index = (start + len(selected)) % len(names)
            following_name = names[following_index]
            response = await self._market_post(_BATCH_PATH, {"marketHashNames": selected})
            rows = parse_batch_quotes(response, requested_names=set(selected), now=now)
            bucket = now // 3600 * 3600

            def persist() -> None:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.executemany(
                        """
                        INSERT INTO quote_snapshots (
                            bucket_at, market_name, sell_price, bid_price,
                            sell_count, bid_count, data_time
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(bucket_at, market_name) DO UPDATE SET
                            sell_price = excluded.sell_price,
                            bid_price = excluded.bid_price,
                            sell_count = excluded.sell_count,
                            bid_count = excluded.bid_count,
                            data_time = excluded.data_time
                        """,
                        [(bucket, *row) for row in rows],
                    )
                    current_valid = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM quote_snapshots WHERE bucket_at = ?", (bucket,)
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        INSERT INTO collection_runs (
                            bucket_at, catalog_count, requested_count, valid_count,
                            updated_at, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(bucket_at) DO UPDATE SET
                            catalog_count = excluded.catalog_count,
                            requested_count = collection_runs.requested_count + excluded.requested_count,
                            valid_count = excluded.valid_count,
                            updated_at = excluded.updated_at,
                            last_error = excluded.last_error
                        """,
                        (
                            bucket,
                            len(names),
                            len(selected),
                            current_valid,
                            now,
                            None if rows else "no_valid_quotes",
                        ),
                    )
                    for key, value in (
                        ("last_attempt_at", str(now)),
                        ("cursor_name", following_name),
                        ("cursor", str(following_index)),
                    ):
                        connection.execute(
                            """
                            INSERT INTO collection_state (key, value, updated_at) VALUES (?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                updated_at = excluded.updated_at
                            """,
                            (key, value, now),
                        )
                    connection.execute(
                        "DELETE FROM quote_snapshots WHERE bucket_at < ?", (now - 90 * 86400,)
                    )
                    connection.execute(
                        "DELETE FROM collection_runs WHERE bucket_at < ?", (now - 90 * 86400,)
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

            await asyncio.to_thread(persist)
            return len(rows)

    async def status(self) -> HistoryStatus:
        await self.initialize()

        def read() -> HistoryStatus:
            connection = self._connect()
            try:
                latest = connection.execute(
                    "SELECT MAX(bucket_at), MIN(bucket_at) FROM quote_snapshots"
                ).fetchone()
                latest_bucket = int(latest[0]) if latest and latest[0] is not None else None
                oldest_bucket = int(latest[1]) if latest and latest[1] is not None else None
                latest_count = (
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM quote_snapshots WHERE bucket_at = ?",
                            (latest_bucket,),
                        ).fetchone()[0]
                    )
                    if latest_bucket is not None
                    else 0
                )
                run = connection.execute(
                    "SELECT catalog_count FROM collection_runs ORDER BY bucket_at DESC LIMIT 1"
                ).fetchone()
                catalog_count = int(run[0]) if run is not None else 0
                bucket_rows = connection.execute(
                    """
                    SELECT bucket_at, COUNT(*) AS item_count
                    FROM quote_snapshots
                    GROUP BY bucket_at
                    ORDER BY bucket_at DESC
                    """
                ).fetchall()
                continuous_buckets = 0
                expected_bucket = latest_bucket
                for bucket_row in bucket_rows:
                    bucket_at = int(bucket_row[0])
                    item_count = int(bucket_row[1])
                    if (
                        expected_bucket is None
                        or bucket_at != expected_bucket
                        or item_count < _MIN_COLUMN_ITEMS
                    ):
                        break
                    continuous_buckets += 1
                    expected_bucket -= 3600
                overlap = 0
                if latest_bucket is not None:
                    overlap = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM quote_snapshots current
                            JOIN quote_snapshots hourly USING (market_name)
                            JOIN quote_snapshots daily USING (market_name)
                            WHERE current.bucket_at = ?
                              AND hourly.bucket_at = ?
                              AND daily.bucket_at = ?
                            """,
                            (latest_bucket, latest_bucket - 3600, latest_bucket - 86400),
                        ).fetchone()[0]
                    )
                return HistoryStatus(
                    catalog_count,
                    latest_count,
                    latest_bucket,
                    oldest_bucket,
                    float(max(0, continuous_buckets - 1)),
                    overlap,
                )
            finally:
                connection.close()

        return await asyncio.to_thread(read)

    async def breadth(self) -> BreadthSnapshot | None:
        await self.initialize()

        def read() -> BreadthSnapshot | None:
            connection = self._connect()
            try:
                latest_row = connection.execute(
                    "SELECT MAX(bucket_at) FROM quote_snapshots"
                ).fetchone()
                latest = int(latest_row[0]) if latest_row and latest_row[0] is not None else None
                if latest is None:
                    return None
                current_bucket = int(self._clock()) // 3600 * 3600
                if latest > current_bucket or current_bucket - latest > 3600:
                    return None
                previous = latest - 3600
                rows = connection.execute(
                    """
                    SELECT current.sell_price, prior.sell_price,
                           current.sell_count, current.bid_count
                    FROM quote_snapshots current
                    JOIN quote_snapshots prior USING (market_name)
                    WHERE current.bucket_at = ? AND prior.bucket_at = ?
                    """,
                    (latest, previous),
                ).fetchall()
            finally:
                connection.close()
            if len(rows) < _MIN_COLUMN_ITEMS:
                return None
            changes = [(float(row[0]) / float(row[1]) - 1) * 100 for row in rows]
            return BreadthSnapshot(
                len(rows),
                sum(change > 0 for change in changes) / len(changes) * 100,
                statistics.median(changes),
                sum(int(row[2]) for row in rows if row[2] is not None),
                sum(int(row[3]) for row in rows if row[3] is not None),
                latest,
            )

        return await asyncio.to_thread(read)

    async def breadth_report(self) -> str:
        breadth = await self.breadth()
        status = await self.status()
        if breadth is None:
            return (
                "📚 广度与深度: 数据采集中\n"
                f" 当前小时有效覆盖: {status.latest_count}件（开放门槛 {_MIN_COLUMN_ITEMS}件）\n"
                f" 已积累: {status.hours_collected:.0f}小时"
            )
        return (
            f" 市场上涨占比: {breadth.rising_ratio:.1f}%（{breadth.compared}件同样本）\n"
            f" 市场中位涨跌: {breadth.median_change:+.2f}%\n"
            f" 挂单深度: 在售 {breadth.sell_depth} / 求购 {breadth.bid_depth}"
        )

    async def render_column(self, command: str) -> str:
        if command in {"热门榜", "热度榜"}:
            return (
                "🔥 【市场热度榜】\n──────────\n"
                "⚠️ 真实短时成交笔数仍不可用，本次不生成排名。\n"
                "ℹ️ K线与批量报价都不含成交数据，不使用挂单量冒充销量。\n"
                "ℹ️ 已评估 Steam 官方成交量来源（cs2.sh，$75~200/月），"
                "当前不付费接入，榜单保持诚实拒绝。"
            )
        if command == "预测":
            from .index_history import index_history_store
            from .market_prediction import render_scenario_forecast

            try:
                await index_history_store.backfill_if_due()
                closes = dict(await index_history_store.load_closes())
            except Exception:
                closes = {}
            if not closes:
                return (
                    "🔮 【短期走势预测｜历史情景】\n──────────\n"
                    "⏳ 大盘日线尚未积累，暂无法形成统计样本。\n"
                    "本次不输出伪概率。"
                )
            current_date = max(closes)
            temperature_line = ""
            try:
                from .market import get_broad_summary
                from .market_temperature import (
                    TemperatureUnavailable,
                    get_market_temperature,
                )

                broad = await get_broad_summary()
                temperature = await get_market_temperature(broad)
                if not isinstance(temperature, TemperatureUnavailable):
                    temperature_line = (
                        f"当前: 温度分 {temperature.score:+.0f}（{temperature.temperature_state}）｜"
                        f"3日动量 {temperature.change_3d:+.1f}%"
                    )
            except Exception:
                pass
            return render_scenario_forecast(closes, current_date, temperature_line=temperature_line)
        if command == "装备指数":
            from .equipment_index import EquipmentIndexService, render_equipment_index

            service = EquipmentIndexService(self)
            await service._store.initialize()
            return render_equipment_index(await service.status())
        return await self.render_ranking(command)

    async def render_ranking(
        self,
        board: str,
        category_query: str = "",
        limit: int = 10,
    ) -> str:
        """Render one ranking board with an optional category filter and size."""

        status = await self.status()
        current_bucket = int(self._clock()) // 3600 * 3600
        ready = self.ranking_ready(status, current_bucket=current_bucket)
        if not ready:
            title = _RANKING_TITLES.get(board, "📊 【市场栏目】")
            lines = [f"{title}", "──────────"]
            target = self.ranking_target(status)
            if (
                status.latest_bucket is None
                or not 0 <= current_bucket - status.latest_bucket <= 3600
            ):
                lines.append("⏳ 最新快照过期或缺失，暂不生成榜单。")
            if status.latest_count < target:
                lines.append(f"⏳ 本小时覆盖 {status.latest_count}/{target} 件")
            if status.ranking_overlap < _MIN_COLUMN_ITEMS:
                lines.append(
                    f"⏳ 三桶同样本 {status.ranking_overlap}/{_MIN_COLUMN_ITEMS} 件"
                    "（需最新/1小时前/24小时前快照齐备）"
                )
            lines.append("达到门槛前不生成榜单。")
            return "\n".join(lines)
        rows = await self._ranking_rows()
        if len(rows) < _MIN_COLUMN_ITEMS:
            return "⚠️ 24小时同样本不足，本次不生成榜单。"

        category: CategorySpec | None = None
        if category_query:
            category = resolve_category(category_query)
            if category is None:
                return (
                    f"⚠️ 未识别的分类「{category_query}」。\n"
                    f"可用分类: {category_hint()}。\n"
                    "格式: 异动榜/涨跌榜/流动性榜 分类 前N（N≤20）"
                )
            rows = [row for row in rows if category.matcher(row[0])]
            if len(rows) < _MIN_CATEGORY_ITEMS:
                scope = _RANKING_TITLES.get(board, "📊 【市场栏目】")
                return (
                    f"{scope}［{category.label}］\n──────────\n"
                    f"⏳ 该分类当前有效样本 {len(rows)} 件（需≥{_MIN_CATEGORY_ITEMS}），暂不生成榜单。\n"
                    "ℹ️ 全市场轮转采集中，分类覆盖会持续扩大。"
                )

        bounded = max(5, min(int(limit), 20))
        display_entries = await self.catalog.entries()
        display_names = {entry.market_name: entry.display_name for entry in display_entries}
        scope_suffix = f"［{category.label}］" if category is not None else ""
        window = ""
        if status.latest_bucket is not None:
            window = (
                f"｜窗口 {_bucket_label(status.latest_bucket - 86400)}"
                f"→{_bucket_label(status.latest_bucket)}"
            )
        sample_line = (
            f"有效同样本: {len(rows)}件{window}"
            if category is None
            else f"分类样本: {len(rows)}件{window}"
        )

        if board == "流动性榜":
            ranked = sorted(
                rows,
                key=lambda row: (row[4] + row[5], row[4], row[0]),
                reverse=True,
            )[:bounded]
            lines = [
                f"💧 【流动性榜】{scope_suffix}",
                "口径: 最新同小时在售量+求购量；不是成交量",
            ]
            if window:
                lines.append(f"数据{window.removeprefix('｜')}")
            for index, row in enumerate(ranked, 1):
                lines.append(
                    f"{index}. {display_names.get(row[0], row[0])}｜在售{row[4]} 求购{row[5]}"
                )
            return "\n".join(lines)

        if board == "涨跌榜":
            rising_count = (bounded + 1) // 2
            falling_count = bounded // 2
            rising = sorted(rows, key=lambda row: (row[2], row[0]), reverse=True)[:rising_count]
            rising_names = {row[0] for row in rising}
            # Only genuine declines belong under the 下跌 heading; on an
            # all-green day list fewer rows instead of showing positive moves.
            falling = [
                row
                for row in sorted(rows, key=lambda row: (row[2], row[0]))
                if row[0] not in rising_names and row[2] < 0
            ][:falling_count]
            lines = [f"📊 【24小时涨跌榜】{scope_suffix}", sample_line, "上涨:"]
            lines.extend(
                f" {index}. {display_names.get(row[0], row[0])}｜{row[2]:+.2f}%"
                for index, row in enumerate(rising, 1)
            )
            lines.append("下跌:")
            lines.extend(
                f" {index}. {display_names.get(row[0], row[0])}｜{row[2]:+.2f}%"
                for index, row in enumerate(falling, 1)
            )
            return "\n".join(lines)

        ranked = sorted(rows, key=lambda row: (abs(row[1]), abs(row[2]), row[0]), reverse=True)[
            :bounded
        ]
        lines = [
            f"🚨 【异动监测】{scope_suffix}",
            sample_line,
            "口径: 1小时绝对涨跌，附24小时方向",
        ]
        lines.extend(
            f"{index}. {display_names.get(row[0], row[0])}｜1h {row[1]:+.2f}% / 24h {row[2]:+.2f}%"
            for index, row in enumerate(ranked, 1)
        )
        return "\n".join(lines)

    @staticmethod
    def ranking_target(status: HistoryStatus) -> int:
        """Same-sample items needed to unlock ranking boards."""

        return max(
            _MIN_COLUMN_ITEMS,
            int((status.sample_count or 1) * _MIN_COVERAGE_RATIO),
        )

    @staticmethod
    def ranking_ready(status: HistoryStatus, *, current_bucket: int) -> bool:
        """Triple-bucket / coverage / freshness gate (panel + internal).

        Boards render from exactly three hourly buckets (latest, one hour
        ago, 24 hours ago), so the gate requires those three buckets to
        share at least 500 same-sample items — plus a fresh latest hour
        with ≥80% pool coverage. A 25-unbroken-hours rule used to sit here,
        but a free-tier rotating collector misses or under-fills some hours,
        so any single gap reset continuity to zero and the boards never
        unlocked despite thousands of usable snapshots.
        """

        coverage = status.latest_count / status.sample_count if status.sample_count > 0 else 0.0
        return (
            status.latest_bucket is not None
            and 0 <= current_bucket - status.latest_bucket <= 3600
            and status.latest_count >= _MIN_COLUMN_ITEMS
            and coverage >= _MIN_COVERAGE_RATIO
            and status.ranking_overlap >= _MIN_COLUMN_ITEMS
        )

    async def top_movers(
        self,
        limit: int = 5,
        *,
        baseline_hours: int = 24,
    ) -> list[tuple[str, float, float, float]] | None:
        """Return the largest same-sample movers over ``baseline_hours``.

        Rows are ``(market_name, change_percent, latest_price, data_time)``.
        ``None`` means the latest snapshot is stale or the same-sample set is
        too small to rank honestly.  No network I/O is performed.
        """

        await self.initialize()

        def read() -> list[tuple[str, float, float, float]]:
            connection = self._connect()
            try:
                latest_raw = connection.execute(
                    "SELECT MAX(bucket_at) FROM quote_snapshots"
                ).fetchone()
                latest = int(latest_raw[0]) if latest_raw and latest_raw[0] is not None else None
                if latest is None:
                    return []
                current_bucket = int(self._clock()) // 3600 * 3600
                if latest > current_bucket or current_bucket - latest > 3600:
                    return []
                baseline = latest - baseline_hours * 3600
                rows = connection.execute(
                    """
                    SELECT current.market_name, current.sell_price, prior.sell_price,
                           current.data_time
                    FROM quote_snapshots current
                    JOIN quote_snapshots prior USING (market_name)
                    WHERE current.bucket_at = ? AND prior.bucket_at = ?
                    """,
                    (latest, baseline),
                ).fetchall()
            finally:
                connection.close()
            movers: list[tuple[str, float, float, float]] = [
                (
                    str(row[0]),
                    (float(row[1]) / float(row[2]) - 1) * 100,
                    float(row[1]),
                    float(int(row[3])),
                )
                for row in rows
            ]
            movers.sort(key=lambda item: abs(item[1]), reverse=True)
            return movers[: max(1, int(limit))]

        rows = await asyncio.to_thread(read)
        return rows if len(rows) >= _MIN_COLUMN_ITEMS else None

    async def item_change(
        self,
        market_name: str,
        *,
        hours: int = 24,
    ) -> tuple[float, float] | None:
        """Return ``(latest_price, change_percent)`` for one item over ``hours``.

        ``None`` when the item is absent from the pool or the latest bucket is
        stale.  Reads stored snapshots only — no network I/O.
        """

        await self.initialize()

        def read() -> tuple[float, float] | None:
            connection = self._connect()
            try:
                latest_raw = connection.execute(
                    "SELECT MAX(bucket_at) FROM quote_snapshots"
                ).fetchone()
                latest = int(latest_raw[0]) if latest_raw and latest_raw[0] is not None else None
                if latest is None:
                    return None
                current_bucket = int(self._clock()) // 3600 * 3600
                if latest > current_bucket or current_bucket - latest > 3600:
                    return None
                current_row = connection.execute(
                    "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
                    (market_name, latest),
                ).fetchone()
                prior_row = connection.execute(
                    "SELECT sell_price FROM quote_snapshots WHERE market_name = ? AND bucket_at = ?",
                    (market_name, latest - hours * 3600),
                ).fetchone()
            finally:
                connection.close()
            if current_row is None or prior_row is None or not prior_row[0]:
                return None
            latest_price = float(current_row[0])
            return latest_price, (latest_price / float(prior_row[0]) - 1) * 100

        return await asyncio.to_thread(read)

    async def item_dip(
        self,
        market_name: str,
        *,
        days: int = 7,
    ) -> tuple[float, float, int] | None:
        """Return ``(latest_price, prior_min, prior_buckets)`` for one item.

        The prior minimum covers stored buckets strictly older than the
        latest sample within ``days`` days.  ``None`` means the item has no
        fresh sample or fewer than one day of prior history — not enough
        evidence to call anything a new low.  Reads stored snapshots only.
        """

        await self.initialize()

        def read() -> tuple[float, float, int] | None:
            connection = self._connect()
            try:
                latest_row = connection.execute(
                    """
                    SELECT bucket_at, sell_price FROM quote_snapshots
                    WHERE market_name = ?
                    ORDER BY bucket_at DESC LIMIT 1
                    """,
                    (market_name,),
                ).fetchone()
                if latest_row is None:
                    return None
                latest_bucket = int(latest_row[0])
                current_bucket = int(self._clock()) // 3600 * 3600
                if latest_bucket > current_bucket or current_bucket - latest_bucket > 3600:
                    return None
                prior_row = connection.execute(
                    """
                    SELECT MIN(sell_price) AS prior_min, COUNT(*) AS prior_buckets
                    FROM quote_snapshots
                    WHERE market_name = ? AND bucket_at < ? AND bucket_at >= ?
                    """,
                    (market_name, latest_bucket, latest_bucket - max(1, int(days)) * 86400),
                ).fetchone()
            finally:
                connection.close()
            if prior_row is None or prior_row[0] is None or int(prior_row[1]) < 24:
                return None
            return float(latest_row[1]), float(prior_row[0]), int(prior_row[1])

        return await asyncio.to_thread(read)

    async def item_daily_closes(
        self,
        market_name: str,
        *,
        days: int = 90,
    ) -> tuple[list[tuple[int, float]], int | None, int | None] | None:
        """Return ``(daily_closes, sell_count, bid_count)`` for one item.

        ``daily_closes`` is an ascending list of ``(day_number, close)`` —
        one close per market-timezone day, taken from that day's latest
        hourly bucket within the trailing ``days`` days.  The counts come
        from the item's latest hourly bucket (``None`` when the snapshot
        lacks them).  ``None`` overall means the item has no fresh sample
        at all.  Reads stored snapshots only — no network I/O.
        """

        await self.initialize()

        def read() -> tuple[list[tuple[int, float]], int | None, int | None] | None:
            connection = self._connect()
            try:
                latest_row = connection.execute(
                    """
                    SELECT bucket_at, sell_count, bid_count FROM quote_snapshots
                    WHERE market_name = ?
                    ORDER BY bucket_at DESC LIMIT 1
                    """,
                    (market_name,),
                ).fetchone()
                if latest_row is None:
                    return None
                latest_bucket = int(latest_row[0])
                current_bucket = int(self._clock()) // 3600 * 3600
                if latest_bucket > current_bucket or current_bucket - latest_bucket > 3600:
                    return None
                since = latest_bucket - max(1, int(days)) * 86400
                rows = connection.execute(
                    """
                    SELECT bucket_at, sell_price FROM quote_snapshots
                    WHERE market_name = ? AND bucket_at >= ?
                    ORDER BY bucket_at ASC
                    """,
                    (market_name, since),
                ).fetchall()
            finally:
                connection.close()
            daily: dict[int, float] = {}
            for bucket_at, sell_price in rows:
                price = float(sell_price)
                if not price > 0:
                    continue
                # Market day boundary at UTC+8 so "daily close" matches the
                # timezone used by every other market column.
                day = (int(bucket_at) + 8 * 3600) // 86400
                daily[day] = price
            closes = sorted(daily.items())
            sell_count = int(latest_row[1]) if latest_row[1] is not None else None
            bid_count = int(latest_row[2]) if latest_row[2] is not None else None
            return closes, sell_count, bid_count

        return await asyncio.to_thread(read)

    async def refresh_equipment_components(self, *, now: int | None = None) -> int:
        """Maintain the fixed monthly component list (batch-4 lifecycle).

        The baseline is frozen for its UTC+8 calendar month once it holds
        at least 200 components:

        * No components yet -> build from the trailing-30-day candidate
          rules; fewer than 200 components is honest accumulation progress.
        * Components below 200 -> a later refresh may rebuild, so the first
          small sample does not freeze the month.
        * At least 200 components all stamped with the current UTC+8 month
          -> frozen: names, base_day, base_price and added_at are left
          untouched and the existing actual count is returned.
        * A complete baseline entering a new month -> new candidates are
          computed first; when at least 200 valid components exist, the
          whole baseline is replaced atomically inside one transaction.
        * New-month candidates below 200 or empty -> the old baseline and
          old base date are kept (no fake new-month base) and the retained
          actual count is returned.
        * Any SELECTED member (top-250 by the shared rule) whose quote is
          invalid or older than 24h refuses the WHOLE refresh: the old
          component table stays complete, the remaining valid members are
          not written, and no lower-ranked name is pulled in as a refill —
          the same all-or-nothing rule the one-shot rebase transaction
          applies.

        One explicit transaction (``BEGIN IMMEDIATE`` before the first
        SELECT, commit on success, rollback + re-raise on any failure)
        covers the read of the existing baseline, the decide step and the
        replace, so a concurrent refresh cannot base its decision on a
        state it did not lock.  Returns the component count actually
        present after the refresh.
        """

        await self.initialize()
        clock = int(now if now is not None else self._clock())
        today = _market_day(clock)
        month_of_today = today[:7]

        def build() -> int:
            connection = self._connect()
            try:
                # One explicit write transaction covers the read of the
                # existing baseline, the decide step and the replace, so a
                # concurrent refresh cannot base its decision on a state it
                # did not lock.  BEGIN IMMEDIATE takes the reserved lock
                # before the first SELECT.
                connection.execute("BEGIN IMMEDIATE")
                existing_count = connection.execute(
                    "SELECT COUNT(*) FROM equipment_index_components"
                ).fetchone()
                existing_count = int(existing_count[0]) if existing_count else 0
                # The baseline is frozen only when it holds at least 200
                # rows AND every row's base day belongs to the current
                # UTC+8 month — never judged from a single arbitrary row.
                months = connection.execute(
                    "SELECT DISTINCT substr(base_day, 1, 7) FROM equipment_index_components"
                ).fetchall()
                all_current_month = len(months) == 1 and str(months[0][0]) == month_of_today
                frozen = existing_count >= 200 and all_current_month
                # A frozen (≥200, all same-month) baseline is not touched:
                # the index must measure against one fixed reference all
                # month.  Rule updates never rewrite a frozen baseline.
                if frozen:
                    connection.commit()
                    return existing_count
                # Not frozen yet: rank candidates on the shared rule (buckets
                # DESC, last_bucket DESC, market_name COLLATE BINARY ASC)
                # over the trailing 30 days ENDING at B = the latest bucket —
                # quotes after B never enter ranking or prices.  Membership
                # is fixed by the full sort first; a member with an invalid
                # or aged quote REFUSES the whole refresh below (no refill,
                # no partial write of the remaining valid members).
                latest_bucket = connection.execute(
                    "SELECT MAX(bucket_at) FROM quote_snapshots"
                ).fetchone()
                latest_bucket = int(latest_bucket[0]) if latest_bucket and latest_bucket[0] else 0
                since = clock - 30 * 86400
                rows = connection.execute(
                    """
                    SELECT market_name,
                           COUNT(*) AS buckets,
                           MAX(bucket_at) AS last_bucket
                    FROM quote_snapshots
                    WHERE bucket_at >= ? AND bucket_at <= ?
                    GROUP BY market_name
                    """
                    + EQUIPMENT_ORDER_BY
                    + """
                    LIMIT ?
                    """,
                    (since, latest_bucket, _EQUIPMENT_COMPONENT_TARGET),
                ).fetchall()
                entries: list[tuple[str, str, float, int]] = []
                rejected = 0
                for row in rows:
                    name = str(row[0])
                    price_row = connection.execute(
                        """
                        SELECT sell_price FROM quote_snapshots
                        WHERE market_name = ? AND bucket_at = ?
                        """,
                        (name, latest_bucket),
                    ).fetchone()
                    base_price = _positive(price_row[0] if price_row is not None else None)
                    if base_price is None:
                        # Fall back to the item's most recent price at-or-
                        # before B; the fallback must be fresh enough
                        # (0 <= B - source_bucket <= 24h) and finite positive.
                        source_row = connection.execute(
                            """
                            SELECT MAX(bucket_at) FROM quote_snapshots
                            WHERE market_name = ? AND bucket_at <= ?
                            """,
                            (name, latest_bucket),
                        ).fetchone()
                        if source_row is None or source_row[0] is None:
                            rejected += 1
                            continue
                        source_bucket = int(source_row[0])
                        age = latest_bucket - source_bucket
                        if age < 0 or age > EQUIPMENT_FALLBACK_MAX_AGE_SECONDS:
                            rejected += 1
                            continue
                        price_row = connection.execute(
                            """
                            SELECT sell_price FROM quote_snapshots
                            WHERE market_name = ? AND bucket_at = ?
                            """,
                            (name, source_bucket),
                        ).fetchone()
                        base_price = _positive(price_row[0] if price_row is not None else None)
                        if base_price is None:
                            rejected += 1
                            continue
                    entries.append((name, today, base_price, clock))
                if rejected:
                    # A selected member has no valid in-age quote: the whole
                    # refresh is refused — the old component table stays
                    # complete and untouched, the remaining valid members are
                    # never written (same all-or-nothing rule as the rebase
                    # transaction; no refill from lower-ranked names).
                    connection.commit()
                    return existing_count
                if not entries:
                    # Nothing collected: keep an existing complete baseline
                    # untouched; an empty table stays empty (no data to
                    # build from).
                    connection.commit()
                    return existing_count if existing_count else 0
                if len(entries) < 200 and existing_count >= 200:
                    # The new month cannot build a complete list yet while
                    # a complete baseline exists: keep the old baseline
                    # (and old base day), never shrink or half-replace it.
                    connection.commit()
                    return existing_count
                # First build with whatever the market offers (accumulation
                # progress) or a full cross-month/accumulation rebuild —
                # both replace inside the locked transaction.
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO equipment_index_components
                        (market_name, base_day, base_price, added_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    entries,
                )
                # Remove names that no longer make the top list (monthly churn).
                keep = [name for name, _, _, _ in entries]
                if keep:
                    placeholders = ",".join("?" * len(keep))
                    connection.execute(
                        f"DELETE FROM equipment_index_components"
                        f" WHERE market_name NOT IN ({placeholders})",
                        keep,
                    )
                connection.commit()
                return len(entries)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        return await asyncio.to_thread(build)

    async def item_snapshot(self, market_name: str) -> dict[str, float] | None:
        """Return one item's latest sampled price, 7-day average and change.

        Reads only stored snapshots; ``None`` means the item is not part of
        the sampled pool.  ``data_time`` is the latest sample's timestamp.
        """

        await self.initialize()

        def read() -> dict[str, float] | None:
            connection = self._connect()
            try:
                latest_row = connection.execute(
                    """
                    SELECT sell_price, data_time FROM quote_snapshots
                    WHERE market_name = ?
                    ORDER BY bucket_at DESC LIMIT 1
                    """,
                    (market_name,),
                ).fetchone()
                if latest_row is None:
                    return None
                latest_price = float(latest_row[0])
                latest_time = int(latest_row[1])
                week_rows = connection.execute(
                    """
                    SELECT sell_price FROM quote_snapshots
                    WHERE market_name = ? AND bucket_at >= ?
                    """,
                    (market_name, latest_time - 7 * 86400),
                ).fetchall()
                prices = [float(row[0]) for row in week_rows if row[0] is not None]
                week_ago_row = connection.execute(
                    """
                    SELECT sell_price FROM quote_snapshots
                    WHERE market_name = ? AND bucket_at <= ?
                    ORDER BY bucket_at DESC LIMIT 1
                    """,
                    (market_name, latest_time - 7 * 86400),
                ).fetchone()
            finally:
                connection.close()
            average = sum(prices) / len(prices) if prices else latest_price
            change = (
                (latest_price / float(week_ago_row[0]) - 1) * 100
                if week_ago_row is not None and week_ago_row[0]
                else 0.0
            )
            return {
                "latest_price": latest_price,
                "data_time": float(latest_time),
                "week_average": average,
                "week_change": change,
                "samples": float(len(prices)),
            }

        return await asyncio.to_thread(read)

    async def _ranking_rows(self) -> list[tuple[str, float, float, float, int, int]]:
        """Return latest price changes with real depth for one identical sample set."""

        await self.initialize()

        def read() -> list[tuple[str, float, float, float, int, int]]:
            connection = self._connect()
            try:
                latest_raw = connection.execute(
                    "SELECT MAX(bucket_at) FROM quote_snapshots"
                ).fetchone()
                latest = int(latest_raw[0]) if latest_raw and latest_raw[0] is not None else None
                if latest is None:
                    return []
                current_bucket = int(self._clock()) // 3600 * 3600
                if latest > current_bucket or current_bucket - latest > 3600:
                    return []
                one = latest - 3600
                day = latest - 86400
                raw_rows = connection.execute(
                    """
                    SELECT current.market_name, current.sell_price,
                           hourly.sell_price, daily.sell_price,
                           current.sell_count, current.bid_count
                    FROM quote_snapshots current
                    JOIN quote_snapshots hourly USING (market_name)
                    JOIN quote_snapshots daily USING (market_name)
                    WHERE current.bucket_at = ? AND hourly.bucket_at = ? AND daily.bucket_at = ?
                    """,
                    (latest, one, day),
                ).fetchall()
            finally:
                connection.close()
            return [
                (
                    str(row[0]),
                    (float(row[1]) / float(row[2]) - 1) * 100,
                    (float(row[1]) / float(row[3]) - 1) * 100,
                    float(row[1]),
                    int(row[4]) if row[4] is not None else 0,
                    int(row[5]) if row[5] is not None else 0,
                )
                for row in raw_rows
            ]

        return await asyncio.to_thread(read)


market_history = MarketHistoryStore()


__all__ = [
    "BreadthSnapshot",
    "HistoryStatus",
    "MarketHistoryStore",
    "market_history",
    "parse_batch_quotes",
]

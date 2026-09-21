from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.services.market_catalog import CatalogEntry
from src.services.market_history import MarketHistoryStore, parse_batch_quotes
from src.services.resource_governor import resource_governor

NOW = 1_800_000_000


class Catalog:
    def __init__(self, count: int) -> None:
        self.items = tuple(CatalogEntry(f"Item {index}", f"饰品 {index}") for index in range(count))

    async def entries(self) -> tuple[CatalogEntry, ...]:
        return self.items

    async def full_count(self) -> int:
        return len(self.items)

    async def sample_entries(self, limit: int) -> tuple[CatalogEntry, ...]:
        return self.items[:limit]


def test_parse_batch_quotes_validates_fresh_fields_and_platform_priority() -> None:
    response = {
        "success": True,
        "timestamp": NOW,
        "data": {
            "Item A": {
                "prices": [
                    {
                        "platform": "BUFF",
                        "sellPrice": 100,
                        "biddingPrice": 99,
                        "sellCount": 5,
                        "biddingCount": 2,
                    },
                    {
                        "platform": "YOUPIN",
                        "sellPrice": "101.5",
                        "biddingPrice": 200,
                        "sellCount": 8,
                        "biddingCount": 3,
                    },
                ]
            },
            "not requested": {"sellPrice": 1},
        },
    }
    rows = parse_batch_quotes(response, requested_names={"Item A"}, now=NOW)
    assert rows == [("Item A", 101.5, None, 8, None, NOW)]


def test_parse_batch_quotes_accepts_documented_data_list_shape() -> None:
    response = {
        "success": True,
        "data": [
            {
                "marketHashName": "Item A",
                "dataList": [
                    {
                        "platform": "YOUPIN",
                        "sellPrice": 101.5,
                        "biddingPrice": 100,
                        "sellCount": 8,
                        "biddingCount": 3,
                        "updateTime": NOW,
                    }
                ],
            }
        ],
    }

    assert parse_batch_quotes(response, requested_names={"Item A"}, now=NOW) == [
        ("Item A", 101.5, 100.0, 8, 3, NOW)
    ]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"success": False, "data": []},
        {"success": True, "data": "bad"},
        {"success": True, "data": [{"marketHashName": "A", "sellPrice": -1}]},
        {
            "success": True,
            "data": [{"marketHashName": "A", "sellPrice": 1, "timestamp": NOW - 100_000}],
        },
        {
            "success": True,
            "data": [{"marketHashName": "A", "sellPrice": 1, "timestamp": NOW + 301}],
        },
    ],
)
def test_parse_batch_quotes_rejects_unusable_payloads(response: dict[str, object]) -> None:
    assert parse_batch_quotes(response, requested_names={"A"}, now=NOW) == []


@pytest.mark.asyncio
async def test_collection_requires_full_catalog_and_respects_one_minute_quota(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    small_post = AsyncMock()
    small = MarketHistoryStore(
        path=tmp_path / "small.db",
        catalog=Catalog(999),  # type: ignore[arg-type]
        market_post=small_post,
        wall_clock=lambda: clock[0],
    )
    assert await small.collect_once() == 0
    small_post.assert_not_awaited()

    async def post(_path: str, body: dict[str, object] | None) -> dict[str, object]:
        assert body is not None
        names = body["marketHashNames"]
        assert isinstance(names, list)
        return {
            "success": True,
            "timestamp": clock[0],
            "data": [
                {
                    "marketHashName": name,
                    "sellPrice": 100,
                    "biddingPrice": 99,
                    "sellCount": 5,
                    "biddingCount": 2,
                }
                for name in names
            ],
        }

    full_post = AsyncMock(side_effect=post)
    store = MarketHistoryStore(
        path=tmp_path / "history.db",
        catalog=Catalog(1_000),  # type: ignore[arg-type]
        market_post=full_post,
        wall_clock=lambda: clock[0],
    )
    assert await store.collect_once() == 100
    assert await store.collect_once() == 0
    clock[0] += 60
    assert await store.collect_once() == 0
    clock[0] += 1
    assert await store.collect_once() == 100
    assert full_post.await_count == 2
    status = await store.status()
    assert status.catalog_count == 1_000 and status.latest_count == 200


@pytest.mark.asyncio
async def test_collection_pauses_during_game(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "game.db",
        catalog=Catalog(1_000),  # type: ignore[arg-type]
        market_post=AsyncMock(),
        wall_clock=lambda: NOW,
    )
    original = resource_governor._game_running
    try:
        resource_governor._game_running = True
        assert await store.collect_once() == 0
    finally:
        resource_governor._game_running = original


@pytest.mark.asyncio
async def test_large_full_catalog_uses_bounded_sample_as_coverage_denominator(
    tmp_path: Path,
) -> None:
    async def post(_path: str, body: dict[str, object] | None) -> dict[str, object]:
        assert body is not None
        names = body["marketHashNames"]
        assert isinstance(names, list)
        return {
            "success": True,
            "timestamp": NOW,
            "data": [
                {"marketHashName": name, "sellPrice": 100, "platform": "YOUPIN"} for name in names
            ],
        }

    store = MarketHistoryStore(
        path=tmp_path / "sample.db",
        catalog=Catalog(10_000),  # type: ignore[arg-type]
        market_post=post,
        wall_clock=lambda: NOW,
        sample_pool_size=2_500,
    )

    assert await store.collect_once() == 100
    status = await store.status()
    assert status.sample_count == 2_500
    assert status.catalog_count == 2_500
    assert status.latest_count == 100


async def _seed_ready_history(
    store: MarketHistoryStore,
    *,
    buckets: tuple[int, ...] | None = None,
    catalog_count: int = 600,
) -> None:
    await store.initialize()
    latest = NOW // 3600 * 3600
    selected_buckets = buckets or tuple(range(latest - 86400, latest + 1, 3600))

    def seed() -> None:
        connection = store._connect()
        try:
            rows = []
            for bucket_index, bucket in enumerate(selected_buckets):
                for index in range(500):
                    base = 100 + index / 100
                    price = base * (1 + bucket_index * 0.01 + (0.002 if index % 2 else -0.001))
                    rows.append((bucket, f"Item {index}", price, price - 1, 10 + index, 5, bucket))
            connection.executemany(
                """
                INSERT INTO quote_snapshots (
                    bucket_at, market_name, sell_price, bid_price,
                    sell_count, bid_count, data_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                INSERT INTO collection_runs (
                    bucket_at, catalog_count, requested_count, valid_count,
                    updated_at, last_error
                ) VALUES (?, ?, 500, 500, ?, NULL)
                """,
                (latest, catalog_count, latest),
            )
            connection.commit()
        finally:
            connection.close()

    await asyncio.to_thread(seed)


@pytest.mark.asyncio
async def test_breadth_and_phased_rankings_use_identical_real_samples(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "ready.db",
        catalog=Catalog(500),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    await _seed_ready_history(store)
    status = await store.status()
    assert status.latest_count == 500 and status.hours_collected == 24
    breadth = await store.breadth()
    assert breadth is not None and breadth.compared == 500
    assert breadth.sell_depth > 0 and breadth.bid_depth > 0
    breadth_text = await store.breadth_report()
    assert "市场上涨占比" in breadth_text and "挂单深度" in breadth_text
    for command, marker in (
        ("异动", "1h"),
        ("涨跌榜", "24小时涨跌榜"),
        ("流动性榜", "不是成交量"),
    ):
        assert marker in await store.render_column(command)


@pytest.mark.asyncio
async def test_sparse_history_never_masquerades_as_exact_1h_or_24h_data(
    tmp_path: Path,
) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "sparse.db",
        catalog=Catalog(500),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    latest = NOW // 3600 * 3600
    await _seed_ready_history(
        store,
        buckets=(latest - 90000, latest - 7200, latest),
    )

    status = await store.status()
    assert status.hours_collected == 0
    assert await store.breadth() is None
    assert "三桶同样本 0/500" in await store.render_column("异动")


def _triple_store(tmp_path: Path, name: str, **kwargs) -> MarketHistoryStore:
    return MarketHistoryStore(path=tmp_path / name, catalog=Catalog(500), **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ranking_unlocks_on_triple_bucket_without_continuity(tmp_path: Path) -> None:
    """榜单门禁只看渲染实际消费的三桶（回归：免费轮转采集总有缺小时，
    旧 25 连桶门禁让榜单永远打不开——生产实测三桶交集 1899 件仍显示积累中）。"""

    store = _triple_store(tmp_path, "triple.db", wall_clock=lambda: NOW)
    latest = NOW // 3600 * 3600
    await _seed_ready_history(store, buckets=(latest - 86400, latest - 3600, latest))
    status = await store.status()
    assert status.hours_collected == 1  # 仅最新两桶连续，远不到 24
    assert status.ranking_overlap == 500
    assert MarketHistoryStore.ranking_ready(status, current_bucket=latest)
    board = await store.render_column("涨跌榜")
    assert "24小时涨跌榜" in board
    assert "有效同样本: 500件｜窗口 01-14 16:00→01-15 16:00" in board


@pytest.mark.asyncio
async def test_ranking_stays_closed_without_day_bucket(tmp_path: Path) -> None:
    store = _triple_store(tmp_path, "noday.db", wall_clock=lambda: NOW)
    latest = NOW // 3600 * 3600
    await _seed_ready_history(store, buckets=(latest - 3600, latest))
    status = await store.status()
    assert status.ranking_overlap == 0
    assert not MarketHistoryStore.ranking_ready(status, current_bucket=latest)
    assert "三桶同样本 0/500" in await store.render_column("异动")


@pytest.mark.asyncio
async def test_ranking_closed_on_thin_overlap(tmp_path: Path) -> None:
    store = _triple_store(tmp_path, "thin.db", wall_clock=lambda: NOW)
    latest = NOW // 3600 * 3600
    await _seed_ready_history(store, buckets=(latest - 86400, latest - 3600, latest))

    def thin() -> None:
        connection = store._connect()
        try:
            placeholders = ",".join("?" * 100)
            connection.execute(
                f"DELETE FROM quote_snapshots WHERE bucket_at = ? AND market_name NOT IN ({placeholders})",
                (latest - 86400, *(f"Item {index}" for index in range(100))),
            )
            connection.commit()
        finally:
            connection.close()

    await asyncio.to_thread(thin)
    status = await store.status()
    assert status.ranking_overlap == 100
    assert not MarketHistoryStore.ranking_ready(status, current_bucket=latest)
    assert "三桶同样本 100/500" in await store.render_column("涨跌榜")


@pytest.mark.asyncio
async def test_ranking_closed_when_latest_stale(tmp_path: Path) -> None:
    store = _triple_store(tmp_path, "stale.db", wall_clock=lambda: NOW + 7200)
    latest = NOW // 3600 * 3600
    await _seed_ready_history(store, buckets=(latest - 86400, latest - 3600, latest))
    status = await store.status()
    assert not MarketHistoryStore.ranking_ready(status, current_bucket=latest + 7200)
    assert "最新快照过期或缺失" in await store.render_column("异动")


@pytest.mark.asyncio
async def test_ranking_closed_on_low_coverage(tmp_path: Path) -> None:
    store = _triple_store(tmp_path, "lowcov.db", wall_clock=lambda: NOW)
    latest = NOW // 3600 * 3600
    await _seed_ready_history(
        store, buckets=(latest - 86400, latest - 3600, latest), catalog_count=2500
    )
    status = await store.status()
    assert status.ranking_overlap == 500
    assert not MarketHistoryStore.ranking_ready(status, current_bucket=latest)
    assert "本小时覆盖 500/2000" in await store.render_column("涨跌榜")


@pytest.mark.asyncio
async def test_closed_columns_explain_exact_missing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "empty.db",
        catalog=Catalog(0),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    # The 预测 column calls the live index-history singleton's
    # backfill_if_due (a real SteamDT fetch) before it can see that no
    # closes exist.  Stub the singleton so this column renders its
    # honest "no sample" answer without any network attempt.
    from src.services import index_history as index_history_module

    monkeypatch.setattr(
        index_history_module.index_history_store,
        "backfill_if_due",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        index_history_module.index_history_store,
        "load_closes",
        AsyncMock(return_value=()),
    )
    assert "数据采集中" in await store.breadth_report()
    assert "不使用挂单量冒充销量" in await store.render_column("热门榜")
    assert "预测" in await store.render_column(
        "预测"
    )  # 概率化预测输出（纯函数逻辑见 test_market_prediction）
    assert "门槛 ≥200" in await store.render_column("装备指数")
    assert "暂不生成榜单" in await store.render_column("异动")
    assert "市场栏目" in await store.render_column("未知")


@pytest.mark.asyncio
async def test_heat_board_stays_honest_without_real_volume(tmp_path: Path) -> None:
    """窗口B：热门榜在无真实成交量数据时永远诚实拒绝（fail-closed）。

    已评估 Steam 官方成交量来源（cs2.sh 付费 API，$75~200/月）后放弃接入；
    成交量数据断供的语义是"永久拒绝 + 说明原因"，而不是用挂单量凑数。
    此测试钉死该契约，未来若接入真量源需同步改写并回退本断言。
    """

    store = MarketHistoryStore(
        path=tmp_path / "empty.db",
        catalog=Catalog(0),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    text = await store.render_column("热门榜")
    assert "不生成排名" in text
    assert "不使用挂单量冒充销量" in text
    assert "cs2.sh" in text
    assert "不付费接入" in text
    # 热度榜别名走同一拒绝路径
    alias = await store.render_column("热度榜")
    assert alias == text


# ─── 分类榜单 / TOP N ─────────────────────────────────────


async def _seed_mixed_category_history(store: MarketHistoryStore) -> None:
    """Seed 500 weapon items, 12 sticker items and 4 knife items."""

    await store.initialize()
    latest = NOW // 3600 * 3600
    buckets = tuple(range(latest - 86400, latest + 1, 3600))
    groups = {
        "AK-47 | Test Skin {index} (Field-Tested)": 500,
        "Sticker | Hello {index}": 12,
        "★ Karambit | Fade {index} (Factory New)": 4,
    }

    def seed() -> None:
        connection = store._connect()
        try:
            rows = []
            for bucket_index, bucket in enumerate(buckets):
                flat = []
                for name, count in groups.items():
                    for index in range(count):
                        flat.append(name.format(index=index))
                for item_index, name in enumerate(flat):
                    base = 100 + item_index / 10
                    # Alternate rise/fall across the window so both ranking
                    # sections always have genuine candidates.
                    direction = 0.02 if item_index % 2 else -0.02
                    price = base * (1 + bucket_index * direction + (0.003 if item_index % 2 else 0))
                    rows.append(
                        (bucket, name, price, price - 1, 50 - (item_index % 45), 10, bucket)
                    )
            connection.executemany(
                """
                INSERT INTO quote_snapshots (
                    bucket_at, market_name, sell_price, bid_price,
                    sell_count, bid_count, data_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                INSERT INTO collection_runs (
                    bucket_at, catalog_count, requested_count, valid_count,
                    updated_at, last_error
                ) VALUES (?, 600, 516, 516, ?, NULL)
                """,
                (latest, latest),
            )
            connection.commit()
        finally:
            connection.close()

    await asyncio.to_thread(seed)


@pytest.mark.asyncio
async def test_ranking_category_filter_and_sample_floor(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "categories.db",
        catalog=Catalog(500),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    await _seed_mixed_category_history(store)

    total = await store.render_ranking("异动")
    assert "有效同样本: 516件" in total and "［" not in total

    weapons = await store.render_ranking("异动", "武器")
    assert "［武器］" in weapons and "分类样本: 500件" in weapons
    assert "AK-47" in weapons
    assert "Sticker" not in weapons and "Karambit" not in weapons

    stickers = await store.render_ranking("涨跌榜", "印花")
    assert "［印花］" in stickers and "分类样本: 12件" in stickers

    # Knives only have 2 samples: below the honest floor, no ranking.
    knives = await store.render_ranking("异动", "刀具")
    assert "有效样本 4 件" in knives and "暂不生成榜单" in knives

    unknown = await store.render_ranking("异动", "不存在的分类xyz")
    assert "未识别的分类" in unknown and "刀具" in unknown


@pytest.mark.asyncio
async def test_ranking_limit_is_bounded_and_split_for_movers(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "limits.db",
        catalog=Catalog(500),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    await _seed_mixed_category_history(store)

    # 30 weapon samples, limit clamped down from 99 to 20.
    long_board = await store.render_ranking("流动性榜", "武器", 99)
    assert (
        long_board.count("\n1. ")
        + sum(1 for line in long_board.splitlines() if line[:3] in {f"{n}. " for n in range(1, 21)})
        >= 10
    )
    assert len([line for line in long_board.splitlines() if line and line[0].isdigit()]) == 20

    # Movers board splits the budget evenly: limit 20 → 10 rising + 10 falling.
    movers = await store.render_ranking("涨跌榜", "武器", 20)
    rising_section = movers.split("上涨:")[1].split("下跌:")[0]
    falling_section = movers.split("下跌:")[1]
    assert len([line for line in rising_section.splitlines() if line.strip()]) == 10
    assert len([line for line in falling_section.splitlines() if line.strip()]) == 10

    # Default limit 10 keeps the legacy 5+5 split byte-compatible.
    default_movers = await store.render_ranking("涨跌榜", "武器")
    default_rising = default_movers.split("上涨:")[1].split("下跌:")[0]
    default_falling = default_movers.split("下跌:")[1]
    assert len([line for line in default_rising.splitlines() if line.strip()]) == 5
    assert len([line for line in default_falling.splitlines() if line.strip()]) == 5


@pytest.mark.asyncio
async def test_movers_rising_and_falling_sets_are_disjoint(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "disjoint.db",
        catalog=Catalog(500),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    # Sticker category has 12 samples, below the 10/10 split budget: a buggy
    # implementation would repeat items across both sections.
    await _seed_mixed_category_history(store)

    movers = await store.render_ranking("涨跌榜", "印花", 20)
    assert "［印花］" in movers
    rising_section = movers.split("上涨:")[1].split("下跌:")[0]
    falling_section = movers.split("下跌:")[1]

    def names(section: str) -> set[str]:
        return {
            line.strip()
            for line in section.splitlines()
            if line.strip()[:3] in {f"{n}. " for n in range(1, 21)}
        }

    rising_names = names(rising_section)
    falling_names = names(falling_section)
    assert rising_names and falling_names
    assert rising_names.isdisjoint(falling_names)


@pytest.mark.asyncio
async def test_movers_falling_section_never_lists_positive_moves(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "allgreen.db",
        catalog=Catalog(500),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    # _seed_ready_history grows every item across the window: an all-green day.
    await _seed_ready_history(store)

    movers = await store.render_ranking("涨跌榜")
    falling_section = movers.split("下跌:")[1]
    positive_rows = [
        line
        for line in falling_section.splitlines()
        if line.strip()[:3] in {f"{n}. " for n in range(1, 21)} and "+" in line
    ]
    assert positive_rows == []

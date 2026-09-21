"""V0.5 night batch 4: equipment price index (equal-weight log, base 100).

The index publishes only after the gate (≥200 fixed components with ≥90%
coverage); before that it shows honest progress.  The component table lives
in the market-history database (idempotent DDL, no main-db migration).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.equipment_index import (
    MIN_COMPONENTS,
    EquipmentIndexService,
    render_equipment_index,
)
from src.services.market_history import MarketHistoryStore

NOW = 1_800_000_000  # fixed wall clock
_BASE_DAY_SECONDS = NOW - 30 * 86400


def _insert_snapshot(
    store: MarketHistoryStore,
    name: str,
    bucket_at: int,
    price: float,
) -> None:
    import sqlite3

    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO quote_snapshots
                (bucket_at, market_name, sell_price, bid_price,
                 sell_count, bid_count, data_time)
            VALUES (?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (bucket_at, name, price, bucket_at),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def store(tmp_path: Path) -> MarketHistoryStore:
    s = MarketHistoryStore(path=tmp_path / "history.db", wall_clock=lambda: NOW)
    return s


@pytest.mark.asyncio
async def test_index_not_ready_shows_progress(store: MarketHistoryStore) -> None:
    await store.initialize()
    for index in range(10):
        _insert_snapshot(store, f"item-{index}", NOW // 3600 * 3600, 100.0 + index)
    service = EquipmentIndexService(store)
    status = await service.status()
    assert status.ready is False
    assert status.components == 0
    rendered = render_equipment_index(status)
    assert "固定成分与覆盖仍在建立" in rendered
    assert f"门槛 ≥{MIN_COMPONENTS}" in rendered


@pytest.mark.asyncio
async def test_component_builder_fills_table(store: MarketHistoryStore) -> None:
    await store.initialize()
    latest_bucket = NOW // 3600 * 3600
    for index in range(240):
        # Each item appears in 5 buckets to be selected by the builder.
        for offset in range(5):
            _insert_snapshot(
                store, f"item-{index:03d}", latest_bucket - offset * 3600, 100.0 + index
            )
    count = await store.refresh_equipment_components(now=NOW)
    assert count == 240
    service = EquipmentIndexService(store)
    status = await service.status()
    assert status.components == 240
    assert status.covered == 240
    assert status.coverage == pytest.approx(1.0)
    assert status.ready is True
    assert status.latest_value is not None


@pytest.mark.asyncio
async def test_index_value_equal_weight_log(store: MarketHistoryStore) -> None:
    await store.initialize()
    latest_bucket = NOW // 3600 * 3600
    base_bucket = latest_bucket - 3600
    # 210 components: 105 double and 105 halve -> geometric mean = 1.0 -> index 100
    names = [f"item-{index:03d}" for index in range(210)]
    for index, name in enumerate(names):
        _insert_snapshot(store, name, base_bucket, 100.0)
        if index % 2 == 0:
            _insert_snapshot(store, name, latest_bucket, 200.0)
        else:
            _insert_snapshot(store, name, latest_bucket, 50.0)
    # Seed the component table directly (builder requires 30-day history).
    import sqlite3

    connection = sqlite3.connect(store.path)
    for name in names:
        connection.execute(
            "INSERT INTO equipment_index_components"
            " (market_name, base_day, base_price, added_at) VALUES (?, ?, ?, ?)",
            (name, "1970-01-01", 100.0, NOW),
        )
    connection.commit()
    connection.close()
    service = EquipmentIndexService(store)
    status = await service.status()
    assert status.ready is True
    assert status.latest_value == pytest.approx(100.0, abs=1e-6)
    rendered = render_equipment_index(status)
    assert "基期 100" in rendered
    assert "等权对数价格指数" in rendered


@pytest.mark.asyncio
async def test_component_builder_rolls_base_day_by_month(
    store: MarketHistoryStore,
) -> None:
    await store.initialize()
    latest_bucket = NOW // 3600 * 3600
    for index in range(210):
        _insert_snapshot(store, f"item-{index:03d}", latest_bucket, 100.0)
    # First build at NOW
    await store.refresh_equipment_components(now=NOW)
    # Second build ~60 days later crosses a month boundary -> base day rolls.
    later = NOW + 60 * 86400
    later_bucket = later // 3600 * 3600
    for index in range(210):
        _insert_snapshot(store, f"item-{index:03d}", later_bucket, 100.0)
    import sqlite3

    connection = sqlite3.connect(store.path)
    base = connection.execute("SELECT base_day FROM equipment_index_components LIMIT 1").fetchone()
    connection.close()
    assert base is not None and base[0]
    first_base = str(base[0])
    await store.refresh_equipment_components(now=later)
    connection = sqlite3.connect(store.path)
    base2 = connection.execute("SELECT base_day FROM equipment_index_components LIMIT 1").fetchone()
    connection.close()
    assert str(base2[0]) != first_base  # month rolled


@pytest.mark.asyncio
async def test_missing_base_price_skips_component(store: MarketHistoryStore) -> None:
    await store.initialize()
    latest_bucket = NOW // 3600 * 3600
    base_bucket = latest_bucket - 3600
    for index in range(210):
        _insert_snapshot(store, f"item-{index:03d}", base_bucket, 100.0)
    # Only 100 items have a latest-bucket price -> coverage ~48% < 90%
    for index in range(100):
        _insert_snapshot(store, f"item-{index:03d}", latest_bucket, 100.0)
    await store.refresh_equipment_components(now=NOW)
    service = EquipmentIndexService(store)
    status = await service.status()
    assert status.covered == 100
    assert status.ready is False
    assert status.latest_value is None

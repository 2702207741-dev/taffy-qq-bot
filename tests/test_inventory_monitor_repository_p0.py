"""P0 hoard-radar repository tests: anchors, reattachment, float cache, trajectory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.services.float_resolver import ItemFloatAnchors
from src.state.inventory_monitor_repository import (
    InventoryMonitorRepository,
)


def _item(name: str) -> str:
    return json.dumps({"name": name}, ensure_ascii=False)


async def _subscribed_repository(tmp_path: Path, name: str) -> InventoryMonitorRepository:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / name))
    await repository.initialize()
    await repository.subscribe(
        101,
        "private",
        101,
        steam_id="76561198000000000",
        nickname="account",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("AK-47 | 红线")},
        now=100,
    )
    return repository


@pytest.mark.asyncio
async def test_commit_persists_anchors_and_reattaches_same_finish(
    tmp_path: Path,
) -> None:
    """同款同磨损 asset_id 变化时重挂接回原 history 行，不产生重复档案。"""

    repository = await _subscribed_repository(tmp_path, "reattach.db")
    claim = await repository.claim_account("76561198000000000", 60, now=200)
    assert claim is not None
    committed = await repository.commit_snapshot(
        claim,
        nickname="account",
        snapshot_json='{"version":2}',
        snapshot_hash="b" * 64,
        source_time=190,
        items={"fp:new": _item("AK-47 | 红线")},
        removed_item_keys=("asset:1",),
        message_text=None,
        anchors={
            "fp:new": ("AK-47 | 红线", 302, 0.12345, None, None),
        },
        reattach_keys=(("fp:new", "asset:1"),),
        now=200,
    )

    assert committed.applied
    rows = await repository.database.read(
        """
        SELECT item_key, market_hash_name, paintwear, last_missing_at
        FROM inventory_monitor_item_history WHERE steam_id = '76561198000000000'
        """
    )
    # The revived original row keeps its identity; no duplicate second row.
    assert len(rows) == 1
    assert rows[0]["item_key"] == "asset:1"
    assert rows[0]["market_hash_name"] == "AK-47 | 红线"
    assert float(rows[0]["paintwear"]) == pytest.approx(0.12345)
    assert rows[0]["last_missing_at"] is None
    missing = await repository.missing_item_keys("76561198000000000")
    assert "asset:1" not in missing


@pytest.mark.asyncio
async def test_anchor_upsert_keeps_first_wear_and_fills_nulls_only(
    tmp_path: Path,
) -> None:
    repository = await _subscribed_repository(tmp_path, "anchors.db")
    claim = await repository.claim_account("76561198000000000", 60, now=200)
    assert claim is not None
    await repository.commit_snapshot(
        claim,
        nickname="account",
        snapshot_json='{"version":2}',
        snapshot_hash="b" * 64,
        source_time=190,
        items={"asset:1": _item("AK-47 | 红线")},
        removed_item_keys=(),
        message_text=None,
        anchors={"asset:1": ("AK-47 | 红线", 302, 0.42, 777, '["印花"]')},
        now=200,
    )
    claim = await repository.claim_account("76561198000000000", 60, now=300)
    assert claim is not None
    # Second round has no float result: existing anchors must be preserved.
    await repository.commit_snapshot(
        claim,
        nickname="account",
        snapshot_json='{"version":3}',
        snapshot_hash="c" * 64,
        source_time=290,
        items={"asset:1": _item("AK-47 | 红线")},
        removed_item_keys=(),
        message_text=None,
        now=300,
    )
    row = (
        await repository.database.read(
            """
            SELECT market_hash_name, paint_index, paintwear, pattern_index, stickers_json
            FROM inventory_monitor_item_history WHERE item_key = 'asset:1'
            """
        )
    )[0]
    assert row["market_hash_name"] == "AK-47 | 红线"
    assert int(row["paint_index"]) == 302
    assert float(row["paintwear"]) == pytest.approx(0.42)
    assert int(row["pattern_index"]) == 777
    assert json.loads(row["stickers_json"]) == ["印花"]


@pytest.mark.asyncio
async def test_trajectory_requires_subscribed_session(tmp_path: Path) -> None:
    from src.services.inventory_monitor import InventoryMonitorService

    repository = await _subscribed_repository(tmp_path, "traj.db")
    service = InventoryMonitorService(repository, wall_clock=lambda: 10_000)

    blocked = await service.trajectory(202, "private", 202, "76561198000000000")
    assert "尚未监控" in blocked

    malformed = await service.trajectory(101, "private", 101, "not-a-steam-id")
    assert "未能确认" in malformed

    ok = await service.trajectory(101, "private", 101, "76561198000000000", days="7")
    assert "库存轨迹" in ok
    bad_days = await service.trajectory(101, "private", 101, "76561198000000000", days="9999")
    assert "天数需为" in bad_days


@pytest.mark.asyncio
async def test_float_cache_roundtrip(tmp_path: Path) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "float.db"))
    await repository.initialize()
    digest = "e" * 64
    anchors = ItemFloatAnchors(
        paintwear=0.0877,
        market_hash_name="M4A1-S | 印花集",
        paint_index=812,
        pattern_index=None,
        stickers=("AMS",),
    )
    written = await repository.save_float_cache({digest: (anchors, 5_000)})
    assert written == 1
    loaded = await repository.load_float_cache([digest])
    assert loaded[digest].paintwear == pytest.approx(0.0877)
    assert loaded[digest].stickers == ("AMS",)
    # Unknown and malformed digests simply miss.
    assert await repository.load_float_cache(["f" * 64]) == {}
    assert await repository.load_float_cache(["zz"]) == {}


@pytest.mark.asyncio
async def test_trajectory_snapshot_projects_history_rows(tmp_path: Path) -> None:
    repository = await _subscribed_repository(tmp_path, "proj.db")
    record = await repository.trajectory_snapshot("76561198000000000", since=50)
    assert record is not None
    nickname, rows = record
    assert nickname == "account"
    assert len(rows) == 1
    assert rows[0].name == "AK-47 | 红线"
    assert rows[0].last_missing_at is None

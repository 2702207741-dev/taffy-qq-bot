"""P0 hoard-radar service tests: anchors, reattachment, no-float degradation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.services.inventory_monitor import (
    InventoryMonitorService,
    anchor_matches,
    compare_inventory_snapshots,
    format_trajectory_report,
    match_history_reattachment,
    parse_monitor_snapshot,
)
from src.state.inventory_monitor_repository import InventoryMonitorRepository

STEAM_ID = "76561198000000000"


def _response(
    source_time: int,
    items: list[tuple[str, str, str, bool, int | None, float]],
    *,
    actions: Mapping[str, list[dict[str, str]]] | None = None,
) -> dict[str, object]:
    assets: list[dict[str, object]] = []
    classinfos: dict[str, dict[str, object]] = {}
    for index, (asset_id, fingerprint, name, tradable, tradable_time, price) in enumerate(items):
        key = f"class-{index}"
        asset: dict[str, object] = {
            "assetId": asset_id,
            "fingerprint": fingerprint,
            "classinfoKey": key,
            "evaluatePrice": price,
        }
        if actions and asset_id in actions:
            asset["actions"] = actions[asset_id]
        assets.append(asset)
        classinfos[key] = {
            "name": name,
            "tradable": tradable,
            "tradableTime": tradable_time,
        }
    return {
        "success": True,
        "data": {
            "steamAccount": {"nickname": "AI"},
            "inventory": {
                "summary": {
                    "privacy": False,
                    "notPrivacy": True,
                    "empty": False,
                    "lastFetchTime": source_time,
                    "total": len(items),
                },
                "assets": assets,
                "classinfos": classinfos,
            },
        },
    }


class RecordingResolver:
    """Test double resolving preconfigured URLs while recording traffic."""

    def __init__(self, table: dict[str, tuple[float, str]]) -> None:
        self.table = table
        self.calls: list[tuple[str, ...]] = []

    async def resolve(self, urls):
        self.calls.append(tuple(urls))
        from src.services.float_resolver import ItemFloatAnchors

        return {
            url: ItemFloatAnchors(paintwear=wear, market_hash_name=name)
            for url, (wear, name) in self.table.items()
            if url in urls
        }


def _static_fetcher(response: dict[str, object]):
    async def fetch(_steam_id: str) -> dict[str, object]:
        return response

    return fetch


def _service_with(
    repository: InventoryMonitorRepository,
    response: dict[str, object],
    wall_clock=float,
    float_resolver=None,
) -> InventoryMonitorService:
    return InventoryMonitorService(
        repository,
        fetcher=_static_fetcher(response),
        wall_clock=wall_clock,
        float_resolver=float_resolver,
    )


@pytest.mark.asyncio
async def test_two_same_ak_different_wears_anchor_to_distinct_rows(tmp_path: Path) -> None:
    """有 float 时：同款 AK 两把不同磨损分别建档，互不混淆。"""

    inspect_a = "steam://connect/1/a"
    inspect_b = "steam://connect/2/b"
    response = _response(
        900,
        [
            ("1", "fpA", "AK-47 | 红线", True, None, 10.0),
            ("2", "fpB", "AK-47 | 红线", True, None, 12.0),
        ],
        actions={
            "1": [{"link": inspect_a}],
            "2": [{"link": inspect_b}],
        },
    )
    resolver = RecordingResolver(
        {inspect_a: (0.1234, "AK-47 | 红线"), inspect_b: (0.2567, "AK-47 | 红线")}
    )
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "s1.db"))
    await repository.initialize()
    service = _service_with(
        repository,
        response,
        wall_clock=lambda: 1_000,
        float_resolver=resolver,
    )
    result = await service.start(101, "private", 101, STEAM_ID)
    assert not isinstance(result, str)
    _, snapshot = result
    # Both physical AKs keep separate wear anchors; the resolver saw both URLs.
    assert len(resolver.calls) == 1
    wears = sorted(item.paintwear for item in snapshot.items if item.paintwear is not None)
    assert len(wears) == 2
    assert wears[0] == pytest.approx(0.1234)
    assert wears[1] == pytest.approx(0.2567)
    rows = await repository.database.read(
        "SELECT market_hash_name, paintwear FROM inventory_monitor_item_history "
        "WHERE steam_id = ? ORDER BY paintwear",
        (STEAM_ID,),
    )
    assert [float(row["paintwear"]) for row in rows] == pytest.approx([0.1234, 0.2567])


@pytest.mark.asyncio
async def test_without_float_env_system_runs_with_null_anchors(tmp_path: Path) -> None:
    """无 float 环境（API base 空）：全套功能正常，anchors 列为 NULL。"""

    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "s2.db"))
    await repository.initialize()
    service = _service_with(
        repository,
        _response(900, [("1", "fpA", "M4A1", True, None, 5.0)]),
        wall_clock=lambda: 1_000,
        # No float_resolver configured (= INVENTORY_FLOAT_API_BASE unset).
    )
    result = await service.start(101, "private", 101, STEAM_ID)
    assert not isinstance(result, str)
    _, snapshot = result
    assert all(item.paintwear is None for item in snapshot.items)

    rows = await repository.database.read(
        "SELECT paintwear FROM inventory_monitor_item_history WHERE steam_id = ?",
        (STEAM_ID,),
    )
    assert rows
    assert all(row["paintwear"] is None for row in rows)


def test_anchor_matches_tolerance_windows() -> None:
    assert anchor_matches("AK-47", 0.1000, "AK-47", 0.10005)
    assert anchor_matches("AK-47", 0.9999, "AK-47", 1.0)
    assert not anchor_matches("AK-47", 0.1000, "AK-47", 0.1002)
    assert not anchor_matches("AK-47", 0.1, "M4A1", 0.1)
    assert not anchor_matches(None, 0.1, "AK-47", 0.1)


def test_match_history_reattachment_relinks_missing_row_only_within_tolerance() -> None:
    from src.services.inventory_monitor import MonitoredInventoryItem

    history = {
        "old-key": ("AK-47 | 红线", 0.1234, True),  # missing
        "live-key": ("AK-47 | 红线", 0.5000, False),  # still present
    }
    new_item = MonitoredInventoryItem(
        item_key="new-key",
        asset_id="9",
        fingerprint="x",
        name="AK-47 | 红线",
        is_tradable=True,
        tradable_after=None,
        price=None,
        market_hash_name="AK-47 | 红线",
        paintwear=0.12345,
    )
    matches = match_history_reattachment([new_item], history, candidate_keys={"new-key"})
    assert matches == {"new-key": "old-key"}

    drift = replace_wear(new_item, 0.1240)  # outside ±0.0001
    assert match_history_reattachment([drift], history, candidate_keys={"new-key"}) == {}
    live = replace_key(new_item, "live-key-match")
    assert match_history_reattachment([live], history, candidate_keys={live.item_key}) == {}


def replace_wear(item, wear: float):
    from dataclasses import replace

    return replace(item, paintwear=wear)


def replace_key(item, key: str):
    from dataclasses import replace

    return replace(item, item_key=key, paintwear=0.5000)


def test_diff_is_idempotent_for_repeated_snapshots() -> None:
    first = parse_monitor_snapshot(
        _response(900, [("1", "a", "A", True, None, 1.0), ("2", "b", "B", False, None, 2.0)]),
        STEAM_ID,
        now=900,
    )
    second = parse_monitor_snapshot(
        _response(900, [("1", "a", "A", True, None, 1.0), ("2", "b", "B", False, None, 2.0)]),
        STEAM_ID,
        now=901,
    )
    diff = compare_inventory_snapshots(first, second)
    assert not diff.changed
    again = compare_inventory_snapshots(second, first)
    assert not again.changed


def test_format_trajectory_report_hides_unknown_destinations() -> None:
    from src.services.inventory_monitor import TrajectorySummary

    summary = TrajectorySummary(
        added=2,
        removed=1,
        present=3,
        hidden_periods=1,
        top_added=(("AK-47 | 红线", 2),),
        top_removed=(("印花 | 旧", 1),),
        timeline=(("08-01 10:00 + AK-47 | 红线",),),
    )
    report = format_trajectory_report("AI", summary, window_days=30, window_start=0, checked_at=1)
    assert "机器推断" not in report
    assert "不可见" in report
    assert "近30天" in report

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.core.database import StateDatabase
from src.services.inventory_monitor import (
    InventoryMonitorError,
    InventoryMonitorService,
    compare_inventory_snapshots,
    format_inventory_change,
    parse_monitor_snapshot,
)
from src.state.inventory_monitor_repository import InventoryMonitorRepository


def _response(
    source_time: int,
    items: list[tuple[str, str, str, bool, int | None, float]],
    *,
    privacy: bool = False,
    empty: bool = False,
) -> dict[str, object]:
    assets: list[dict[str, object]] = []
    classinfos: dict[str, dict[str, object]] = {}
    for index, (asset_id, fingerprint, name, tradable, tradable_time, price) in enumerate(items):
        key = f"class-{index}"
        assets.append(
            {
                "assetId": asset_id,
                "fingerprint": fingerprint,
                "classinfoKey": key,
                "evaluatePrice": price,
            }
        )
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
                    "privacy": privacy,
                    "notPrivacy": not privacy,
                    "empty": empty,
                    "lastFetchTime": source_time,
                    "total": len(items),
                },
                "assets": assets,
                "classinfos": classinfos,
            },
        },
    }


def test_snapshot_hash_ignores_price_but_tracks_trade_state() -> None:
    first = parse_monitor_snapshot(
        _response(900, [("1", "fingerprint", "item", True, None, 10.0)]),
        "76561198000000000",
        now=1_000,
    )
    price_changed = parse_monitor_snapshot(
        _response(950, [("1", "fingerprint", "item", True, None, 99.0)]),
        "76561198000000000",
        now=1_000,
    )
    locked = parse_monitor_snapshot(
        _response(950, [("1", "fingerprint", "item", True, 2_000, 99.0)]),
        "76561198000000000",
        now=1_000,
    )

    assert first.comparison_hash == price_changed.comparison_hash
    assert locked.comparison_hash != first.comparison_hash


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response(900, [], privacy=True), "私密"),
        (_response(900, [], empty=True), "显示为空"),
        (_response(1, [("1", "fp", "item", True, None, 1.0)]), "超过24小时"),
    ],
)
def test_invalid_snapshot_never_becomes_a_baseline(
    response: Mapping[str, object],
    message: str,
) -> None:
    with pytest.raises(InventoryMonitorError, match=message):
        parse_monitor_snapshot(response, "76561198000000000", now=100_000)


def test_diff_separates_reappearance_addition_removal_and_unlock() -> None:
    previous = parse_monitor_snapshot(
        _response(
            900,
            [
                ("1", "one", "removed", True, None, 10.0),
                ("2", "two", "unlock", True, 2_000, 20.0),
            ],
        ),
        "76561198000000000",
        now=1_000,
    )
    current = parse_monitor_snapshot(
        _response(
            2_100,
            [
                ("2", "two", "unlock", True, 2_000, 20.0),
                ("3", "three", "reappeared", True, None, 30.0),
                ("4", "four", "new locked", True, 3_000, 40.0),
            ],
        ),
        "76561198000000000",
        now=2_100,
    )
    key_three = current.item_map()[
        next(key for key in current.item_map() if current.item_map()[key].asset_id == "3")
    ].item_key
    difference = compare_inventory_snapshots(
        previous,
        current,
        previously_missing=frozenset({key_three}),
    )
    message = format_inventory_change(current, difference, checked_at=2_100)

    assert [item.name for item in difference.reappeared] == ["reappeared"]
    assert [item.name for item in difference.added] == ["new locked"]
    assert [item.name for item in difference.removed] == ["removed"]
    assert difference.state_changes[0][1].name == "unlock"
    assert "曾从公开库存消失后重新出现" in message
    assert "可能是从仓库组件取出、交易返回或其他回流" in message
    assert "市场购买或交易收货均有可能" in message
    assert "交易限制已解除" in message
    assert "出售、交易或存入仓库组件均有可能" in message


@pytest.mark.asyncio
async def test_equal_account_fetches_are_singleflight(tmp_path: Path) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    release = asyncio.Event()
    calls = 0

    async def fetcher(_: str) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        await release.wait()
        return _response(900, [("1", "one", "item", True, None, 10.0)])

    service = InventoryMonitorService(repository, fetcher=fetcher, wall_clock=lambda: 1_000)
    first = asyncio.create_task(service.snapshot("76561198000000000"))
    second = asyncio.create_task(service.snapshot("76561198000000000"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert calls == 1
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_manual_checks_update_history_and_detect_storage_reappearance(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    fetcher = AsyncMock(
        side_effect=[
            _response(
                900,
                [
                    ("1", "same-item", "AWP | Wildfire", True, None, 10.0),
                    ("2", "anchor", "Storage Unit", False, None, 1.0),
                ],
            ),
            _response(1_000, [("2", "anchor", "Storage Unit", False, None, 1.0)]),
            _response(
                1_100,
                [
                    ("1", "same-item", "AWP | Wildfire", True, None, 10.0),
                    ("2", "anchor", "Storage Unit", False, None, 1.0),
                ],
            ),
        ]
    )
    clock = iter(
        (
            1_000.0,
            1_000.0,
            1_100.0,
            1_100.0,
            1_100.0,
            1_200.0,
            1_200.0,
            1_200.0,
        )
    )
    service = InventoryMonitorService(
        repository,
        fetcher=fetcher,
        wall_clock=lambda: next(clock),
    )
    started = await service.start(101, "private", 101, "76561198000000000")
    assert not isinstance(started, str)

    removed_message, removed_changed = await service.check_now(
        101,
        "private",
        101,
        "76561198000000000",
    )
    assert removed_changed
    assert "已离开公开库存" in removed_message

    reappeared_message, reappeared_changed = await service.check_now(
        101,
        "private",
        101,
        "76561198000000000",
    )
    assert reappeared_changed
    assert "曾从公开库存消失后重新出现" in reappeared_message
    assert "可能是从仓库组件取出、交易返回或其他回流" in reappeared_message
    assert fetcher.await_count == 3


@pytest.mark.asyncio
async def test_manual_check_cannot_cross_from_private_monitor_into_group(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    fetcher = AsyncMock(return_value=_response(900, [("1", "one", "item", True, None, 10.0)]))
    service = InventoryMonitorService(
        repository,
        fetcher=fetcher,
        wall_clock=lambda: 1_000,
    )
    started = await service.start(101, "private", 101, "76561198000000000")
    assert not isinstance(started, str)

    message, changed = await service.check_now(
        101,
        "group",
        9001,
        "76561198000000000",
    )

    assert not changed
    assert "当前会话尚未监控" in message
    assert fetcher.await_count == 1


def test_change_message_aggregates_duplicate_items() -> None:
    previous = parse_monitor_snapshot(
        _response(900, []),
        "76561198000000000",
        now=1_000,
    )
    current = parse_monitor_snapshot(
        _response(
            2_100,
            [
                ("1", "a", "AK-47 | Redline (Field-Tested)", True, None, 10.0),
                ("2", "b", "AK-47 | Redline (Field-Tested)", True, None, 10.0),
                ("3", "c", "AK-47 | Redline (Field-Tested)", True, None, 10.0),
                ("4", "d", "武器箱", True, None, 5.0),
            ],
        ),
        "76561198000000000",
        now=2_100,
    )
    difference = compare_inventory_snapshots(previous, current)
    message = format_inventory_change(current, difference, checked_at=2_100)

    assert message.count("AK-47 | Redline (Field-Tested)") == 1  # aggregated once
    assert "× 3" in message  # count suffix on duplicates
    assert "武器箱（可交易）" in message  # single item keeps no suffix
    assert "× 1" not in message


def test_change_message_does_not_count_aggregated_items_as_omitted() -> None:
    """A16 regression: the omitted count must compare aggregated rows."""

    previous = parse_monitor_snapshot(_response(900, []), "76561198000000000", now=1_000)

    # 30 identical items aggregate to one displayed row: nothing is omitted.
    duplicates = parse_monitor_snapshot(
        _response(2_100, [(str(i), f"f{i}", "同名物品", True, None, 1.0) for i in range(30)]),
        "76561198000000000",
        now=2_100,
    )
    duplicate_message = format_inventory_change(
        duplicates, compare_inventory_snapshots(previous, duplicates), checked_at=2_100
    )
    assert "已省略" not in duplicate_message

    # More distinct rows than _MAX_DETAILS still reports the honest remainder.
    many_distinct = parse_monitor_snapshot(
        _response(2_100, [(str(i), f"g{i}", f"物品 {i}", True, None, 1.0) for i in range(25)]),
        "76561198000000000",
        now=2_100,
    )
    distinct_message = format_inventory_change(
        many_distinct, compare_inventory_snapshots(previous, many_distinct), checked_at=2_100
    )
    assert "其余 5 项变化已省略" in distinct_message


@pytest.mark.asyncio
async def test_completed_inflight_task_is_reaped_for_next_caller(tmp_path: Path) -> None:
    """A17 regression: an unawaited finished fetch must not serve stale data."""

    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state17.db"))
    await repository.initialize()
    release = asyncio.Event()
    calls = 0

    async def fetcher(_: str) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
            return _response(900, [("1", "one", "旧物品", True, None, 10.0)])
        return _response(1_200, [("2", "two", "新物品", True, None, 20.0)])

    service = InventoryMonitorService(repository, fetcher=fetcher, wall_clock=lambda: 1_000)
    steam_id = "76561198000000000"

    first = asyncio.create_task(service.snapshot(steam_id))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert calls == 1

    # The orphaned inner task finishes with nobody awaiting it; its slot must
    # be reaped so the next caller triggers a fresh fetch.
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    result = await service.snapshot(steam_id)

    assert calls == 2
    assert result.source_time == 1_200

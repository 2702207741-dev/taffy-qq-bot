from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.chat.commands import ChatCommandService
from src.services.steam_inventory import (
    SteamInventoryService,
    checksum_timestamp,
    format_inventory_snapshot,
    parse_inventory_identifier,
)


def _snapshot(now: int) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "steamAccount": {"steamId": "76561198000000000", "nickname": "测试账号"},
            "inventory": {
                "summary": {
                    "privacy": False,
                    "notPrivacy": True,
                    "empty": False,
                    "lastFetchTime": now - 30,
                    "total": 2,
                    "totalTradableCount": 2,
                    "totalPrice": 345.67,
                },
                "assets": [
                    {"classinfoKey": "one", "evaluatePrice": 300},
                    {"classinfoKey": "two", "evaluatePrice": None},
                ],
                "classinfos": {
                    "one": {"name": "AK-47 | 红线 (久经沙场)"},
                    "two": {"name": "武器箱"},
                },
            },
        },
    }


def _empty_snapshot(now: int) -> dict[str, object]:
    response = _snapshot(now)
    inventory = response["data"]["inventory"]  # type: ignore[index]
    inventory["summary"]["empty"] = True  # type: ignore[index]
    inventory["summary"]["total"] = 0  # type: ignore[index]
    inventory["summary"]["totalTradableCount"] = 0  # type: ignore[index]
    inventory["summary"]["totalPrice"] = 0  # type: ignore[index]
    inventory["assets"] = []  # type: ignore[index]
    inventory["classinfos"] = {}  # type: ignore[index]
    return response


def test_identifier_parser_accepts_steam64_and_trade_link() -> None:
    assert parse_inventory_identifier("76561198000000000") is not None
    parsed = parse_inventory_identifier(
        "https://steamcommunity.com/tradeoffer/new/?partner=39734272&token=abc"
    )
    assert parsed is not None
    assert parsed.steam_id == str(76_561_197_960_265_728 + 39_734_272)
    assert parse_inventory_identifier("某个昵称") is None


def test_checksum_timestamp_matches_web_client_algorithm() -> None:
    assert checksum_timestamp("1783954657951") == "1783954657959"
    assert len(checksum_timestamp("1783954657951")) == 13


def test_inventory_formatter_reports_time_coverage_and_read_only_semantics() -> None:
    now = 1_800_000_000
    result = format_inventory_snapshot(_snapshot(now), now=now)

    assert "账号: 测试账号" in result
    assert "物品总数: 2｜可交易: 2" in result
    assert "快照总估值: ¥345.67" in result
    assert "可估价覆盖: 1/2" in result
    assert "AK-47 | 红线" in result
    assert "未触发库存刷新" in result


def test_inventory_formatter_lists_all_assets_sorted_by_subtotal() -> None:
    now = 1_800_000_000
    response = _snapshot(now)
    inventory = response["data"]["inventory"]  # type: ignore[index]
    inventory["summary"]["total"] = 25  # type: ignore[index]
    inventory["summary"]["totalTradableCount"] = 25  # type: ignore[index]
    inventory["assets"] = [
        {"classinfoKey": f"item-{index}", "evaluatePrice": float(index)} for index in range(1, 26)
    ]
    inventory["classinfos"] = {
        f"item-{index}": {"name": f"测试饰品 {index}"} for index in range(1, 26)
    }

    result = format_inventory_snapshot(response, now=now)

    assert "全部单品（按估值降序，同名聚合）" in result
    assert result.count("小计 ¥") == 25
    assert "1. 测试饰品 25 × 1｜¥25.00/件｜小计 ¥25.00" in result
    assert "25. 测试饰品 1 × 1｜¥1.00/件｜小计 ¥1.00" in result
    assert "略" not in result


def test_inventory_formatter_aggregates_same_name_items() -> None:
    now = 1_800_000_000
    response = _snapshot(now)
    inventory = response["data"]["inventory"]  # type: ignore[index]
    inventory["summary"]["total"] = 6  # type: ignore[index]
    inventory["summary"]["totalTradableCount"] = 6  # type: ignore[index]
    inventory["assets"] = [
        {"classinfoKey": "red-1", "evaluatePrice": 150.0},
        {"classinfoKey": "red-2", "evaluatePrice": 150.0},
        {"classinfoKey": "red-3", "evaluatePrice": 150.0},
        {"classinfoKey": "case-1", "evaluatePrice": 8.5},
        {"classinfoKey": "case-2", "evaluatePrice": 8.5},
        {"classinfoKey": "knife-1", "evaluatePrice": 400.0},
    ]
    inventory["classinfos"] = {
        "red-1": {"name": "AK-47 | 红线 (久经沙场)"},
        "red-2": {"name": "AK-47 | 红线 (久经沙场)"},
        "red-3": {"name": "AK-47 | 红线 (久经沙场)"},
        "case-1": {"name": "武器箱"},
        "case-2": {"name": "武器箱"},
        "knife-1": {"name": "★ 刺刀 (崭新出厂)"},
    }

    result = format_inventory_snapshot(response, now=now)

    lines = result.splitlines()
    item_lines = [line for line in lines if "小计" in line]
    assert len(item_lines) == 3
    # Aggregated subtotal 450 outranks the single knife at 400; cases at 17 come last.
    assert "1. AK-47 | 红线 (久经沙场) × 3｜¥150.00/件｜小计 ¥450.00" in item_lines[0]
    assert "2. ★ 刺刀 (崭新出厂) × 1｜¥400.00/件｜小计 ¥400.00" in item_lines[1]
    assert "3. 武器箱 × 2｜¥8.50/件｜小计 ¥17.00" in item_lines[2]


def test_inventory_formatter_truncates_rows_with_notice() -> None:
    from unittest.mock import patch

    now = 1_800_000_000
    response = _snapshot(now)
    inventory = response["data"]["inventory"]  # type: ignore[index]
    count = 130
    inventory["summary"]["total"] = count  # type: ignore[index]
    inventory["summary"]["totalTradableCount"] = count  # type: ignore[index]
    inventory["assets"] = [
        {"classinfoKey": f"item-{index}", "evaluatePrice": float(index)}
        for index in range(1, count + 1)
    ]
    inventory["classinfos"] = {
        f"item-{index}": {"name": f"测试饰品 {index}"} for index in range(1, count + 1)
    }

    with patch("src.services.steam_inventory._MAX_DETAIL_ROWS", 100):
        result = format_inventory_snapshot(response, now=now)

    assert result.count("小计 ¥") == 100
    assert "…其余 30 种物品略（按估值降序）" in result


def test_inventory_formatter_empty_assets_returns_dedicated_notice() -> None:
    now = 1_800_000_000
    result = format_inventory_snapshot(_empty_snapshot(now), now=now)
    assert "全部单品" not in result
    assert "库存快照暂时显示为空" in result


def test_inventory_formatter_distinguishes_private_and_stale_snapshots() -> None:
    now = 1_800_000_000
    private = _snapshot(now)
    private["data"]["inventory"]["summary"]["privacy"] = True  # type: ignore[index]
    assert "库存为私密" in format_inventory_snapshot(private, now=now)

    stale = _snapshot(now)
    stale["data"]["inventory"]["summary"]["lastFetchTime"] = now - 90_000  # type: ignore[index]
    stale_result = format_inventory_snapshot(stale, now=now)
    assert "超过24小时" in stale_result
    assert "不会自动刷新" in stale_result


@pytest.mark.asyncio
async def test_inventory_service_singleflights_equal_requests() -> None:
    now = 1_800_000_000
    fetcher = AsyncMock(return_value=_snapshot(now))
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    results = await asyncio.gather(*(service.report("76561198000000000") for _ in range(10)))

    assert all("测试账号" in result for result in results)
    fetcher.assert_awaited_once_with("76561198000000000")


@pytest.mark.asyncio
async def test_inventory_service_clears_cancelled_singleflight_before_retry() -> None:
    now = 1_800_000_000
    fetcher = AsyncMock(side_effect=[asyncio.CancelledError(), _snapshot(now)])
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    with pytest.raises(asyncio.CancelledError):
        await service.report("76561198000000000")

    assert not service._inflight
    assert "测试账号" in await service.report("76561198000000000")
    assert fetcher.await_count == 2


@pytest.mark.asyncio
async def test_successful_inventory_snapshot_is_fetched_again_without_local_cooldown() -> None:
    now = 1_800_000_000
    fetcher = AsyncMock(side_effect=[_snapshot(now), _snapshot(now)])
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    assert "测试账号" in await service.report("76561198000000000")
    assert "测试账号" in await service.report("76561198000000000")

    assert fetcher.await_count == 2


@pytest.mark.asyncio
async def test_empty_placeholder_is_rechecked_and_can_recover_immediately() -> None:
    now = 1_800_000_000
    fetcher = AsyncMock(side_effect=[_empty_snapshot(now), _snapshot(now)])
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    result = await service.report("76561198000000000")

    assert "测试账号" in result
    assert "库存为空" not in result
    assert fetcher.await_count == 2


@pytest.mark.asyncio
async def test_confirmed_empty_snapshot_is_not_cached() -> None:
    now = 1_800_000_000
    fetcher = AsyncMock(side_effect=[_empty_snapshot(now), _empty_snapshot(now), _snapshot(now)])
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    first = await service.report("76561198000000000")
    second = await service.report("76561198000000000")

    assert "连续两次实时读取均显示为空" in first
    assert "请直接重试" in first
    assert "测试账号" in second
    assert fetcher.await_count == 3


@pytest.mark.asyncio
async def test_stale_snapshot_is_rechecked_and_can_recover_immediately() -> None:
    now = 1_800_000_000
    stale = _snapshot(now)
    stale["data"]["inventory"]["summary"]["lastFetchTime"] = now - 90_000  # type: ignore[index]
    fetcher = AsyncMock(side_effect=[stale, _snapshot(now)])
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    result = await service.report("76561198000000000")

    assert "测试账号" in result
    assert "超过24小时" not in result
    assert fetcher.await_count == 2


@pytest.mark.asyncio
async def test_stale_warning_can_be_retried_immediately() -> None:
    now = 1_800_000_000
    stale = _snapshot(now)
    stale["data"]["inventory"]["summary"]["lastFetchTime"] = now - 90_000  # type: ignore[index]
    fetcher = AsyncMock(return_value=stale)
    service = SteamInventoryService(
        fetcher=fetcher,
        wall_clock=lambda: now,
    )

    first = await service.report("76561198000000000")
    assert "自动复查后仍未更新" in first
    assert fetcher.await_count == 2

    assert await service.report("76561198000000000") == first
    assert fetcher.await_count == 4


@pytest.mark.asyncio
async def test_stale_recheck_failure_is_reported_honestly() -> None:
    now = 1_800_000_000
    stale = _snapshot(now)
    stale["data"]["inventory"]["summary"]["lastFetchTime"] = now - 90_000  # type: ignore[index]
    fetcher = AsyncMock(side_effect=[stale, TimeoutError("upstream down")])
    service = SteamInventoryService(fetcher=fetcher, wall_clock=lambda: now)

    result = await service.report("76561198000000000")

    assert "自动复查失败" in result
    assert "自动复查后仍未更新" not in result
    assert fetcher.await_count == 2


@pytest.mark.asyncio
async def test_inventory_command_never_falls_through_to_chat_model() -> None:
    inventory = AsyncMock(return_value="真实库存快照")
    service = ChatCommandService(
        market_get=AsyncMock(),
        market_post=AsyncMock(),
        inventory_provider=inventory,
    )

    result = await service.execute("库存查询 76561198000000000")

    assert result == "真实库存快照"
    inventory.assert_awaited_once_with("76561198000000000")

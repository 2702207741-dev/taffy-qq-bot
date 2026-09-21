from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import inventory_monitor
from src.services.inventory_monitor import DueCheckSummary
from src.state.inventory_monitor_repository import (
    InventoryDelivery,
    SubscribeResult,
)


@pytest.mark.asyncio
async def test_start_command_creates_baseline_and_explains_daily_schedule() -> None:
    event = SimpleNamespace(
        user_id=101,
        get_plaintext=lambda: "库存监控 76561198000000000",
    )
    snapshot = SimpleNamespace(
        nickname="AI",
        steam_id="76561198000000000",
        source_time=1_000,
    )
    service = SimpleNamespace(
        start=AsyncMock(return_value=(SubscribeResult(True, False, None), snapshot))
    )
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(inventory_monitor, "inventory_monitor_service", service),
        patch.object(
            inventory_monitor._COMMAND_LIMITER,
            "allow",
            AsyncMock(return_value=True),
        ),
        patch.object(inventory_monitor.inventory_monitor_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await inventory_monitor.handle_inventory_monitor_command(
            SimpleNamespace(),
            event,  # type: ignore[arg-type]
        )

    service.start.assert_awaited_once_with(
        101,
        "private",
        101,
        "76561198000000000",
    )
    rendered = str(finish.await_args.args[0])
    assert "每天一次" in rendered
    assert "首次建立基准不会发送历史变化" in rendered


@pytest.mark.asyncio
async def test_manual_command_delivers_other_subscribers_then_replies() -> None:
    event = SimpleNamespace(
        user_id=101,
        get_plaintext=lambda: "立即检查库存 76561198000000000",
    )
    service = SimpleNamespace(check_now=AsyncMock(return_value=("inventory changed", True)))
    finish = AsyncMock(side_effect=FinishedException())
    deliver = AsyncMock(return_value=1)
    bot = SimpleNamespace()
    with (
        patch.object(inventory_monitor, "inventory_monitor_service", service),
        patch.object(
            inventory_monitor._COMMAND_LIMITER,
            "allow",
            AsyncMock(return_value=True),
        ),
        patch.object(inventory_monitor, "process_inventory_monitor_deliveries", deliver),
        patch.object(inventory_monitor.inventory_monitor_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await inventory_monitor.handle_inventory_monitor_command(
            bot,  # type: ignore[arg-type]
            event,  # type: ignore[arg-type]
        )

    deliver.assert_awaited_once_with(bot)
    assert "inventory changed" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_group_and_private_deliveries_use_the_correct_qq_destination() -> None:
    bot = SimpleNamespace(
        send_group_msg=AsyncMock(return_value={"message_id": 1}),
        send_private_msg=AsyncMock(return_value={"message_id": 2}),
    )
    group = InventoryDelivery(
        event_id="event",
        owner_user_id=101,
        destination_type="group",
        destination_id=9001,
        message_text="changed",
        attempts=1,
        lease_token="token",
    )
    private = InventoryDelivery(
        event_id="event",
        owner_user_id=202,
        destination_type="private",
        destination_id=202,
        message_text="changed",
        attempts=1,
        lease_token="token",
    )

    await inventory_monitor._send_delivery(bot, group)  # type: ignore[arg-type]
    await inventory_monitor._send_delivery(bot, private)  # type: ignore[arg-type]

    bot.send_group_msg.assert_awaited_once()
    assert bot.send_group_msg.await_args.kwargs["group_id"] == 9001
    assert "[CQ:at,qq=101]" in str(bot.send_group_msg.await_args.kwargs["message"])
    bot.send_private_msg.assert_awaited_once()
    assert bot.send_private_msg.await_args.kwargs["user_id"] == 202


@pytest.mark.asyncio
async def test_daily_tick_checks_accounts_before_delivering_events() -> None:
    service = SimpleNamespace(check_due=AsyncMock(return_value=DueCheckSummary(1, 1, 1, 0)))
    deliver = AsyncMock(return_value=2)
    bot = SimpleNamespace()
    with (
        patch.object(inventory_monitor, "inventory_monitor_service", service),
        patch.object(inventory_monitor, "process_inventory_monitor_deliveries", deliver),
    ):
        await inventory_monitor.check_inventory_monitors(bot)  # type: ignore[arg-type]

    service.check_due.assert_awaited_once_with()
    deliver.assert_awaited_once_with(bot)


@pytest.mark.asyncio
async def test_list_command_only_requests_the_current_group_destination() -> None:
    event = SimpleNamespace(
        user_id=101,
        get_plaintext=lambda: "库存监控列表",
    )
    service = SimpleNamespace(list_destination=AsyncMock(return_value=()))
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(inventory_monitor, "inventory_monitor_service", service),
        patch.object(
            inventory_monitor._COMMAND_LIMITER,
            "allow",
            AsyncMock(return_value=True),
        ),
        patch.object(
            inventory_monitor,
            "_destination",
            return_value=("group", 9001),
        ),
        patch.object(inventory_monitor.inventory_monitor_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await inventory_monitor.handle_inventory_monitor_command(
            SimpleNamespace(),
            event,  # type: ignore[arg-type]
        )

    service.list_destination.assert_awaited_once_with(101, "group", 9001)

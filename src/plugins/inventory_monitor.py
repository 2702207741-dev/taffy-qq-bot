"""QQ commands and durable delivery for daily inventory monitoring."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Never

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.rule import Rule

from src.core.rate_limit import SlidingWindowLimiter
from src.services.config import bounded_env_int
from src.services.float_resolver import build_default_resolver
from src.services.inventory_monitor import InventoryMonitorService
from src.services.notify_email import shared_email_notifier
from src.services.tz import configured_timezone
from src.state.inventory_monitor_repository import (
    DestinationType,
    InventoryDelivery,
    InventoryMonitorSubscription,
)
from src.state.runtime import inventory_monitors

from .feature_gate import audited_finish, check_feature

_MARKET_TZ = configured_timezone()

logger = logging.getLogger("taffy.inventory_monitor")

_COMMAND_LIMITER = SlidingWindowLimiter(20, 60)
#: Profile generation burns LLM tokens: at most one per owner per 5 minutes.
_PROFILE_WINDOW = bounded_env_int("TAFFY_HOARD_PROFILE_PER_OWNER_SECONDS", 300, 60, 3_600)
_PROFILE_LIMITER = SlidingWindowLimiter(1, _PROFILE_WINDOW)
_SEND_TIMEOUT_SECONDS = 20.0
_DELIVERY_BATCH_SIZE = 50
_DELIVERY_LEASE_SECONDS = 180

# None when INVENTORY_FLOAT_API_BASE is unset: the monitor then runs exactly
# as before and history anchor columns stay NULL (hard no-float degradation).
# SMTP_* unset disables the mail side channel the same way.
inventory_monitor_service = InventoryMonitorService(
    inventory_monitors,
    float_resolver=build_default_resolver(inventory_monitors),
    email_notifier=shared_email_notifier(),
)


def _command_text(event: Event) -> str:
    text = re.sub(r"\[CQ:at,qq=\d+\]", "", event.get_plaintext()).strip()
    return re.sub(r"@\S+\s*", "", text).strip()


def _is_inventory_monitor_command(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = _command_text(event)
    return (
        text.startswith("库存监控")
        or text.startswith("取消库存监控")
        or text.startswith("立即检查库存")
        or text.startswith("库存轨迹")
        or text.startswith("囤货画像")
    )


inventory_monitor_cmd = on_message(
    rule=Rule(_is_inventory_monitor_command),
    priority=7,
    block=True,
)


def _destination(event: MessageEvent) -> tuple[DestinationType, int]:
    if isinstance(event, GroupMessageEvent):
        return "group", int(event.group_id)
    return "private", int(event.user_id)


async def _finish(event: MessageEvent, text: str) -> Never:
    await audited_finish(inventory_monitor_cmd, event, "inventory", text)
    raise RuntimeError("matcher.finish returned unexpectedly")


def _format_subscriptions(
    subscriptions: tuple[InventoryMonitorSubscription, ...],
) -> str:
    if not subscriptions:
        return "📦 【库存监控】\n你还没有监控任何账号。"
    lines = ["📦 【库存监控列表】"]
    for index, raw in enumerate(subscriptions, 1):
        subscription = raw
        destination = (
            f"群 {subscription.destination_id}"
            if subscription.destination_type == "group"
            else "私聊"
        )
        next_check = datetime.fromtimestamp(subscription.next_check_at, tz=_MARKET_TZ).strftime(
            "%m-%d %H:%M"
        )
        lines.extend(
            [
                f"{index}. {subscription.nickname}",
                f"   SteamID: {subscription.steam_id}",
                f"   通知: {destination}｜下次检查: {next_check}",
            ]
        )
    lines.append("──────────\n发送“立即检查库存 SteamID”可主动检查。")
    return "\n".join(lines)


@inventory_monitor_cmd.handle()
async def handle_inventory_monitor_command(bot: Bot, event: MessageEvent) -> None:
    text = _command_text(event)
    owner = int(event.user_id)
    if not await check_feature(inventory_monitor_cmd, event, "inventory"):
        return
    destination_type, destination_id = _destination(event)
    limiter_key = f"{owner}:{destination_type}:{destination_id}"
    if not await _COMMAND_LIMITER.allow(limiter_key):
        await _finish(event, "库存监控操作太频繁了，请稍后再试。")

    if text == "库存监控列表":
        subscriptions = await inventory_monitor_service.list_destination(
            owner,
            destination_type,
            destination_id,
        )
        await _finish(event, _format_subscriptions(subscriptions))

    if text == "库存监控":
        await _finish(
            event,
            "📦 【库存监控】\n"
            "开启: 库存监控 SteamID\n"
            "主动检查: 立即检查库存 SteamID\n"
            "查看: 库存监控列表\n"
            "轨迹: 库存轨迹 SteamID [天数]\n"
            "画像: 囤货画像 SteamID｜状态: 囤货画像状态\n"
            "取消: 取消库存监控 SteamID\n"
            "──────────\n每天自动检查一次，仅在发现变化时提醒。",
        )

    if text.startswith("库存轨迹"):
        rest = text[len("库存轨迹") :].strip()
        parts = rest.split()
        if not parts or len(parts) > 2:
            await _finish(
                event,
                "格式: 库存轨迹 7656119xxxxxxxxxx [天数]\n天数省略时默认统计近30天。",
            )
        raw_identifier = parts[0]
        report = await inventory_monitor_service.trajectory(
            owner,
            destination_type,
            destination_id,
            raw_identifier,
            days=parts[1] if len(parts) == 2 else None,
        )
        # Group queries always answer privately so group members cannot learn
        # which accounts this owner watches; direct chats reply in place.
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.send_private_msg(
                    user_id=owner,
                    message=Message(MessageSegment.text(report)),
                )
            except Exception:
                logger.warning("Inventory trajectory private echo failed user=%s", owner)
            await _finish(event, "📦 轨迹报告已私聊发送。")
        await _finish(event, report)

    if text == "囤货画像状态":
        report = await inventory_monitor_service.profile_status()
        await _finish(event, report)

    if text.startswith("囤货画像"):
        rest = text[len("囤货画像") :].strip()
        parts = rest.split()
        if len(parts) != 1:
            await _finish(event, "格式: 囤货画像 7656119xxxxxxxxxx")
        owner = int(event.user_id)
        if not await _PROFILE_LIMITER.allow(f"profile:{owner}"):
            await _finish(
                event,
                f"画像生成较耗资源，同一账号每 {_PROFILE_WINDOW // 60} 分钟限一次，请稍后再试。",
            )
        raw_identifier = parts[0]
        report = await inventory_monitor_service.profile(
            owner,
            destination_type,
            destination_id,
            raw_identifier,
        )
        # Same privacy discipline as trajectories: group queries answer via
        # private echo so the watched relationship stays invisible in-group.
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.send_private_msg(
                    user_id=owner,
                    message=Message(MessageSegment.text(report)),
                )
            except Exception:
                logger.warning("Inventory profile private echo failed user=%s", owner)
            await _finish(event, "📊 画像报告已私聊发送。")
        await _finish(event, report)

    if text.startswith("取消库存监控"):
        raw_identifier = text[len("取消库存监控") :].strip()
        if not raw_identifier:
            await _finish(event, "格式: 取消库存监控 7656119xxxxxxxxxx")
        cancelled = await inventory_monitor_service.cancel(
            owner,
            destination_type,
            destination_id,
            raw_identifier,
        )
        if cancelled is None:
            await _finish(event, "未能确认账号，请提供17位 Steam64 ID 或完整交易链接。")
        if not cancelled:
            await _finish(event, "当前会话没有监控这个账号。")
        await _finish(event, "✅ 已取消当前会话中的库存监控。")

    if text.startswith("立即检查库存"):
        raw_identifier = text[len("立即检查库存") :].strip()
        if not raw_identifier:
            await _finish(event, "格式: 立即检查库存 7656119xxxxxxxxxx")
        message, _ = await inventory_monitor_service.check_now(
            owner,
            destination_type,
            destination_id,
            raw_identifier,
        )
        try:
            await process_inventory_monitor_deliveries(bot)
        except Exception:
            logger.exception("Unable to deliver inventory changes after manual check")
        await _finish(event, message)

    if text.startswith("库存监控"):
        raw_identifier = text[len("库存监控") :].strip()
        if not raw_identifier:
            await _finish(event, "格式: 库存监控 7656119xxxxxxxxxx")
        started = await inventory_monitor_service.start(
            owner,
            destination_type,
            destination_id,
            raw_identifier,
        )
        if isinstance(started, str):
            await _finish(event, started)
        subscribe_result, snapshot = started
        if subscribe_result.global_quota_exceeded:
            await _finish(event, "库存监控总容量已满，本次没有保存；请先取消不需要的监控。")
        if subscribe_result.quota_exceeded:
            await _finish(event, "每位用户最多监控20个账号，请先取消不需要的监控。")
        source_time = datetime.fromtimestamp(snapshot.source_time, tz=_MARKET_TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if not subscribe_result.created:
            await _finish(
                event,
                "📦 【库存监控】\n"
                f"账号: {snapshot.nickname}\n"
                "当前会话已经在监控这个账号。\n"
                f"数据时间: {source_time}",
            )
        await _finish(
            event,
            "✅ 【库存监控已开启】\n"
            f"账号: {snapshot.nickname}\n"
            f"SteamID: {snapshot.steam_id}\n"
            f"基准时间: {source_time}\n"
            "检查频率: 每天一次，可随时主动检查\n"
            "──────────\n"
            "首次建立基准不会发送历史变化。\n"
            "仅监控接口返回的公开饰品；仓库组件内物品不可见。",
        )


def _message_id(response: object) -> str | int | None:
    if isinstance(response, dict):
        value = response.get("message_id")
        return value if isinstance(value, (str, int)) else None
    return None


async def _send_delivery(bot: Bot, delivery: InventoryDelivery) -> object:
    async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
        if delivery.destination_type == "group":
            message = MessageSegment.at(delivery.owner_user_id) + MessageSegment.text(
                f" {delivery.message_text}"
            )
            return await bot.send_group_msg(
                group_id=delivery.destination_id,
                message=message,
            )
        return await bot.send_private_msg(
            user_id=delivery.destination_id,
            message=Message(MessageSegment.text(delivery.message_text)),
        )


async def process_inventory_monitor_deliveries(bot: Bot) -> int:
    deliveries = await inventory_monitors.claim_deliveries(
        _DELIVERY_BATCH_SIZE,
        _DELIVERY_LEASE_SECONDS,
    )
    delivered = 0
    for delivery in deliveries:
        try:
            response = await _send_delivery(bot, delivery)
            if await inventory_monitors.mark_delivery(
                delivery,
                _message_id(response),
            ):
                delivered += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Inventory monitor delivery failed event=%s error=%s",
                delivery.event_id,
                type(exc).__name__,
            )
            await inventory_monitors.record_delivery_failure(
                delivery,
                type(exc).__name__,
            )
    return delivered


async def check_inventory_monitors(bot: Bot) -> None:
    summary = await inventory_monitor_service.check_due()
    delivered = await process_inventory_monitor_deliveries(bot)
    if summary.claimed or delivered:
        logger.info(
            "Inventory monitor tick claimed=%s completed=%s changed=%s failed=%s delivered=%s",
            summary.claimed,
            summary.completed,
            summary.changed,
            summary.failed,
            delivered,
        )


__all__ = [
    "check_inventory_monitors",
    "inventory_monitor_service",
    "process_inventory_monitor_deliveries",
]

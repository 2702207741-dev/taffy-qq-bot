"""Price-alert command and delivery service backed by transactional SQLite."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Mapping
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
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.rate_limit import SlidingWindowLimiter
from src.services.config import bounded_env_float, bounded_env_int
from src.services.market_validation import (
    MarketDataError,
    preferred_quote,
    validate_quote_response,
)
from src.state.alert_repository import (
    MAX_ALERTS_PER_OWNER,
    MAX_MARKET_NAME_LENGTH,
    AlertDirection,
    AlertRecord,
    price_to_cents,
)
from src.state.runtime import alerts as alert_repository

from .feature_gate import audited_finish, check_feature

logger = logging.getLogger("taffy.alert")

MAX_ALERTS_PER_USER = MAX_ALERTS_PER_OWNER
MAX_ALERT_NAME_LENGTH = MAX_MARKET_NAME_LENGTH
_BASE_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 60 * 60
_CHECK_BATCH_SIZE = bounded_env_int("TAFFY_ALERT_BATCH_SIZE", 50, 1, 1_000)
_ALERT_CONCURRENCY = bounded_env_int("TAFFY_ALERT_CONCURRENCY", 5, 1, 20)
_UNMET_TARGET_RECHECK_SECONDS = 30
_ALERT_SEND_TIMEOUT_SECONDS = bounded_env_float(
    "TAFFY_ALERT_SEND_TIMEOUT_SECONDS",
    20.0,
    1.0,
    120.0,
)
_alerts_check_lock = asyncio.Lock()
_evaluation_semaphore = asyncio.Semaphore(_ALERT_CONCURRENCY)
_command_limiter = SlidingWindowLimiter(20, 60)


class AlertEvaluationError(RuntimeError):
    """A retryable market-data evaluation failed."""


def _claim_lease_seconds() -> int:
    waves = math.ceil(_CHECK_BATCH_SIZE / _ALERT_CONCURRENCY)
    safe_minimum = math.ceil((45.0 + _ALERT_SEND_TIMEOUT_SECONDS + 2.0) * waves + 90.0)
    configured = bounded_env_int("TAFFY_ALERT_LEASE_SECONDS", 180, 1, 86_400)
    return max(configured, safe_minimum)


def _retry_at(alert: AlertRecord, now: int) -> int:
    delay = min(
        _MAX_RETRY_SECONDS,
        _BASE_RETRY_SECONDS * (2 ** min(alert.attempts, 6)),
    )
    return now + delay


def _evaluation_retry_at(alert: AlertRecord, now: int) -> int:
    delay = min(
        _MAX_RETRY_SECONDS,
        _BASE_RETRY_SECONDS * (2 ** min(alert.evaluation_failures, 6)),
    )
    return now + delay


def _message_id(response: object) -> str | int | None:
    if isinstance(response, Mapping):
        value = response.get("message_id")
        if isinstance(value, bool):
            return None
        return value if isinstance(value, (str, int)) else None
    return None


async def _send_alert(bot: Bot, alert: AlertRecord, current_cents: int) -> object:
    current = current_cents / 100
    target = alert.target_price_cents / 100
    direction = "涨到" if alert.direction == "above" else "跌到"
    text = (
        f"🔔 价格预警触发\n{alert.market_name}\n"
        f"当前 ¥{current:.2f}，已{direction}目标价 ¥{target:.2f}"
    )
    async with asyncio.timeout(_ALERT_SEND_TIMEOUT_SECONDS):
        if alert.group_id is not None:
            message = MessageSegment.at(alert.owner_user_id) + MessageSegment.text(f" {text}")
            return await bot.send_group_msg(group_id=alert.group_id, message=message)
        return await bot.send_private_msg(
            user_id=alert.owner_user_id,
            message=Message(MessageSegment.text(text)),
        )


async def _evaluate_claim(bot: Bot, alert: AlertRecord) -> None:
    token = alert.lease_token
    if token is None:
        logger.error("Claimed alert is missing its lease token id=%s", alert.id)
        return

    now = int(time.time())
    try:
        from .chat_utils import st_get

        response = await st_get(
            "/open/cs2/v1/price/single",
            {"marketHashName": alert.market_name},
        )
        try:
            quote = preferred_quote(validate_quote_response(response, now=time.time()))
            current_cents = price_to_cents(quote.sell_price)
        except (MarketDataError, ValueError) as exc:
            reason = exc.code if isinstance(exc, MarketDataError) else "market_price_unavailable"
            raise AlertEvaluationError(reason) from exc
    except AlertEvaluationError as exc:
        logger.warning("Alert market evaluation deferred id=%s reason=%s", alert.id, exc)
        try:
            mutation = await alert_repository.record_evaluation_failure(
                alert.id,
                token,
                exc,
                _evaluation_retry_at(alert, int(time.time())),
                now=int(time.time()),
            )
            if not mutation.applied:
                logger.warning("Alert lease was lost before evaluation defer id=%s", alert.id)
        except Exception as state_error:
            logger.exception(
                "Alert evaluation state could not be committed id=%s error=%s",
                alert.id,
                type(state_error).__name__,
            )
        return
    except Exception as exc:
        logger.warning(
            "Alert upstream request deferred id=%s error=%s", alert.id, type(exc).__name__
        )
        try:
            mutation = await alert_repository.record_evaluation_failure(
                alert.id,
                token,
                exc,
                _evaluation_retry_at(alert, int(time.time())),
                now=int(time.time()),
            )
            if not mutation.applied:
                logger.warning("Alert lease was lost before evaluation defer id=%s", alert.id)
        except Exception as state_error:
            logger.exception(
                "Alert evaluation state could not be committed id=%s error=%s",
                alert.id,
                type(state_error).__name__,
            )
        return

    triggered = (
        current_cents >= alert.target_price_cents
        if alert.direction == "above"
        else current_cents <= alert.target_price_cents
    )
    if not triggered:
        mutation = await alert_repository.defer_unmet_target(
            alert.id,
            token,
            current_cents,
            next_check_at=now + _UNMET_TARGET_RECHECK_SECONDS,
            now=now,
        )
        if not mutation.applied:
            logger.warning("Alert lease was lost before defer id=%s", alert.id)
        return

    try:
        send_result = await _send_alert(bot, alert, current_cents)
        mutation = await alert_repository.mark_delivered(
            alert.id,
            token,
            observed_price_cents=current_cents,
            message_id=_message_id(send_result),
            delivered_at=int(time.time()),
        )
        if not mutation.applied:
            # QQ and SQLite cannot share a transaction.  This explicit signal
            # identifies the narrow at-least-once duplicate-delivery window.
            logger.critical("Alert was sent but delivery state was not committed id=%s", alert.id)
    except Exception as exc:
        logger.exception("Alert QQ delivery failed id=%s", alert.id)
        try:
            mutation = await alert_repository.record_failure(
                alert.id,
                token,
                type(exc).__name__,
                _retry_at(alert, int(time.time())),
                now=int(time.time()),
            )
            if not mutation.applied:
                logger.warning("Alert lease was lost before failure update id=%s", alert.id)
        except Exception as state_error:
            logger.exception(
                "Alert failure state could not be committed id=%s error=%s",
                alert.id,
                type(state_error).__name__,
            )


async def check_alerts(bot: Bot) -> None:
    """Claim and evaluate a bounded batch without holding DB transactions over I/O."""

    async with _alerts_check_lock:
        claimed = await alert_repository.claim_due(
            _CHECK_BATCH_SIZE,
            _claim_lease_seconds(),
        )
    if not claimed:
        return

    async def bounded(alert: AlertRecord) -> None:
        async with _evaluation_semaphore:
            await _evaluate_claim(bot, alert)

    await asyncio.gather(*(bounded(alert) for alert in claimed))


def _command_text(event: Event) -> str:
    text = re.sub(r"\[CQ:at,qq=\d+\]", "", event.get_plaintext()).strip()
    return re.sub(r"@\S+\s*", "", text).strip()


def _is_alert_command(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = _command_text(event)
    return (
        text.startswith("预警")
        or text.startswith("取消预警")
        or text.startswith("重试预警")
        or text == "我的预警"
    )


alert_cmd = on_message(rule=Rule(_is_alert_command), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> Never:
    await audited_finish(alert_cmd, event, "alert", text)
    raise RuntimeError("matcher.finish returned unexpectedly")


@alert_cmd.handle()
async def handle_alert_command(_bot: Bot, event: MessageEvent) -> None:
    text = _command_text(event)
    owner_user_id = int(event.user_id)
    if not await check_feature(alert_cmd, event, "alert"):
        return
    if not await _command_limiter.allow(str(owner_user_id)):
        await _finish(event, "预警操作太频繁了，请稍后再试。")

    try:
        if text == "我的预警":
            mine = await alert_repository.list_owner(owner_user_id)
            if not mine:
                await _finish(event, "暂无预警")
            lines = ["🔔 我的预警"]
            for index, alert in enumerate(mine, 1):
                if alert.status in {"pending", "claimed"}:
                    status = "🟢"
                elif alert.status == "delivered":
                    status = "🔴(已触发)"
                else:
                    status = "⚠️(通知失败，可重试)"
                lines.append(
                    f"  {index}. {status} {alert.market_name} "
                    f"{'涨到' if alert.direction == 'above' else '跌到'} "
                    f"¥{alert.target_price_cents / 100:.2f}"
                )
            await _finish(event, "\n".join(lines))

        if text.startswith("取消预警"):
            raw_index = text.removeprefix("取消预警").strip()
            if not raw_index.isdecimal() or int(raw_index) < 1:
                await _finish(event, "格式: 取消预警 1（编号从 1 开始）")
            removed = await alert_repository.cancel_at(owner_user_id, int(raw_index) - 1)
            if not removed.applied or removed.alert is None:
                await _finish(event, "没有这个预警，请先发送“我的预警”查看编号")
            await _finish(event, f"已取消: {removed.alert.market_name}")

        if text.startswith("重试预警"):
            raw_index = text.removeprefix("重试预警").strip()
            if not raw_index.isdecimal() or int(raw_index) < 1:
                await _finish(event, "格式: 重试预警 1（编号从 1 开始）")
            mine = await alert_repository.list_owner(owner_user_id)
            index = int(raw_index) - 1
            if index < 0 or index >= len(mine):
                await _finish(event, "没有这个预警，请先发送“我的预警”查看编号")
            selected = mine[index]
            if selected.status != "failed":
                await _finish(event, "这个预警不在通知失败状态，无需重试")
            retried = await alert_repository.retry_failed(
                selected.id,
                owner_user_id=owner_user_id,
            )
            if not retried.applied:
                await _finish(event, "预警状态已变化，请重新查看后再试")
            await _finish(event, f"已重新排队: {selected.market_name}")

        if not text.startswith("预警"):
            await _finish(
                event,
                "格式:\n  设置: 预警 涨到/跌到 饰品名 目标价\n  查看: 我的预警"
                "\n  重试: 重试预警 1\n  删除: 取消预警 1",
            )

        payload = text.removeprefix("预警").strip()
        direction_match = re.match(r"^(涨到|高于|上穿|跌到|低于|下穿)\s+(.+)$", payload)
        if direction_match is None:
            await _finish(
                event,
                "请明确预警方向。\n"
                "格式: 预警 涨到/跌到 饰品名 价格\n"
                "例如: 预警 跌到 AK红线 久经 180",
            )
        direction_word, remainder = direction_match.groups()
        direction: AlertDirection = (
            "above" if direction_word in {"涨到", "高于", "上穿"} else "below"
        )
        try:
            name, price_text = remainder.rsplit(maxsplit=1)
            target_cents = price_to_cents(price_text)
        except (ValueError, TypeError):
            await _finish(
                event, "格式: 预警 涨到/跌到 饰品名 价格\n例如: 预警 跌到 AK红线 久经 180"
            )
        name = name.strip()
        if not name or len(name) > MAX_ALERT_NAME_LENGTH:
            await _finish(event, f"饰品名不能为空且不能超过 {MAX_ALERT_NAME_LENGTH} 个字符")

        from .chat import resolve_name
        from .chat.market import resolution_failure_message
        from .chat_utils import st_get

        market_name = await resolve_name(name)
        if market_name is None:
            await _finish(event, resolution_failure_message(name))
        response = await st_get(
            "/open/cs2/v1/price/single",
            {"marketHashName": market_name},
        )
        try:
            current_quote = preferred_quote(validate_quote_response(response, now=time.time()))
            current_cents = price_to_cents(current_quote.sell_price)
        except (MarketDataError, ValueError) as exc:
            detail = exc.user_message if isinstance(exc, MarketDataError) else "当前报价无效。"
            await _finish(event, f"预警未保存：{detail.removeprefix('⚠️ ')}")

        already_reached = (
            current_cents >= target_cents if direction == "above" else current_cents <= target_cents
        )
        if already_reached:
            action = "高于" if direction == "above" else "低于"
            await _finish(
                event,
                f"预警未保存：当前价 ¥{current_cents / 100:.2f} 已经{action}目标价 "
                f"¥{target_cents / 100:.2f}。",
            )

        group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
        result = await alert_repository.create(
            owner_user_id,
            market_name,
            price_text,
            direction=direction,
            group_id=group_id,
        )
        if result.quota_exceeded:
            await _finish(event, f"每位用户最多设置 {MAX_ALERTS_PER_USER} 个预警，请先删除旧预警")
        if not result.created or result.alert is None:
            await _finish(event, "预警保存失败，请稍后再试")
        await _finish(
            event,
            f"🔔 已设置预警: {market_name}\n"
            f"  方向: {'涨到' if direction == 'above' else '跌到'}\n"
            f"  当前价: ¥{current_cents / 100:.2f}\n"
            f"  目标价: ¥{result.alert.target_price_cents / 100:.2f}\n"
            "  价格到位时taffy会通知你",
        )
    except MatcherException:
        raise
    except Exception as exc:
        logger.exception(
            "Alert command failed user=%s error=%s",
            owner_user_id,
            type(exc).__name__,
        )
        await _finish(event, "预警数据暂时不可用，请稍后再试")

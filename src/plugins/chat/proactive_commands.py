"""Opt-in commands for the proactive morning digest.

The switch is per-user and durable.  Recording it works even while the
global ``TAFFY_PROACTIVE_ENABLED`` switch is off — the reply then says so —
so users can subscribe before an administrator ever enables pushing.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, MessageEvent
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.proactive import proactive_enabled, proactive_morning_hour

from ..feature_gate import check_feature, record_event_usage

_log = logging.getLogger("taffy.proactive_commands")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)

_EXACT_COMMANDS = {"早盘提醒", "关闭早盘提醒"}


def _is_proactive_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    return event.get_plaintext().strip() in _EXACT_COMMANDS


proactive_cmd = on_message(rule=Rule(_is_proactive_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="watchlist",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await proactive_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@proactive_cmd.handle()
async def handle_proactive(_bot: Bot, event: MessageEvent) -> None:
    message = event.get_plaintext().strip()
    if not await check_feature(proactive_cmd, event, "watchlist"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "操作太频繁了，请稍后再试。")
    try:
        from src.state.runtime import proactive_deliveries

        if message == "早盘提醒":
            await proactive_deliveries.set_optin(int(event.user_id), True)
            hour = proactive_morning_hour()
            reply = (
                f"已开启早盘提醒：每个工作日 {hour:02d}:00 私聊推送你的自选前 5 件与大盘概览"
                "（数据来自本地快照）。发送“关闭早盘提醒”可随时关闭。"
            )
            if not proactive_enabled():
                reply += "\nℹ️ 管理员当前未启用主动推送，开启后会自动生效。"
            await _finish(event, reply)
        if message == "关闭早盘提醒":
            await proactive_deliveries.set_optin(int(event.user_id), False)
            await _finish(event, "已关闭早盘提醒，不再接收早盘推送与新低提醒。")
        await _finish(event, "格式：早盘提醒｜关闭早盘提醒")
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Proactive command failed error=%s", type(exc).__name__)
        await _finish(event, "早盘提醒这次没设置好，请稍后再试。")


__all__ = ["handle_proactive", "proactive_cmd"]

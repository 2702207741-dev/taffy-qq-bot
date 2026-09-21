"""Movement-reason and market-brief interpretation commands."""

from __future__ import annotations

import logging
from typing import NoReturn

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.exception import MatcherException
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.market_insight import (
    MOVEMENT_THRESHOLD_PERCENT,
    explain_movement,
    interpret_market,
)

from ..feature_gate import check_feature, record_event_usage

_log = logging.getLogger("taffy.insight")
_RATE_LIMITER = SlidingWindowLimiter(6, 300)
_MOVEMENT_PREFIXES = ("异动原因 ", "为什么涨 ", "为什么跌 ", "涨跌原因 ")


def _is_movement_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return any(text.startswith(prefix) and len(text) > len(prefix) for prefix in _MOVEMENT_PREFIXES)


def _is_brief_request(event: Event) -> bool:
    return isinstance(event, (GroupMessageEvent, PrivateMessageEvent)) and (
        event.get_plaintext().strip() == "盘面解读"
    )


movement_cmd = on_message(rule=Rule(_is_movement_request), priority=7, block=True)
brief_cmd = on_message(rule=Rule(_is_brief_request), priority=7, block=True)


async def _finish(matcher: type[Matcher], event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await matcher.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


async def _movement_reply(market_name: str, *, user_query: str | None = None) -> str:
    explanation = await explain_movement(market_name, user_query=user_query)
    if explanation.kind == "no_data":
        return "本地快照还没有这个饰品的 24 小时对比数据，暂时无法判断异动。"
    if explanation.kind == "no_movement":
        assert explanation.change_percent is not None
        return (
            f"该饰品近 24h 无异动（{explanation.change_percent:+.2f}%，"
            f"未达 ±{MOVEMENT_THRESHOLD_PERCENT:.0f}% 门槛）。"
        )
    if explanation.kind == "no_sources":
        base = "未发现公开原因，可能为市场情绪波动。"
        if explanation.extra_lines:
            return base + "\n" + "\n".join(explanation.extra_lines)
        return base
    assert explanation.content is not None
    lines = [explanation.content.strip()]
    if explanation.source_lines:
        lines.append("🔗 核验来源")
        lines.extend(f"  {line}" for line in explanation.source_lines)
    if explanation.extra_lines:
        lines.append("🔗 补充数据")
        lines.extend(f"  {line}" for line in explanation.extra_lines)
    lines.append("解读基于公开来源，仅供参考。")
    return "\n".join(lines)


@movement_cmd.handle()
async def handle_movement(_bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(movement_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(movement_cmd, event, "解读查询太频繁了，请稍后再试。")
    query = next(
        text[len(prefix) :].strip()
        for prefix in _MOVEMENT_PREFIXES
        if text.startswith(prefix) and len(text) > len(prefix)
    )
    try:
        from . import resolve_name

        market_name = await resolve_name(query)
        if market_name is None:
            from .market import resolution_failure_message

            await _finish(movement_cmd, event, resolution_failure_message(query))
        await _finish(movement_cmd, event, await _movement_reply(market_name, user_query=query))
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Movement reason command failed error=%s", type(exc).__name__)
        await _finish(movement_cmd, event, "异动解读这次没算出来，请稍后再试。")


@brief_cmd.handle()
async def handle_brief(_bot: Bot, event: MessageEvent) -> None:
    if not await check_feature(brief_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(brief_cmd, event, "解读查询太频繁了，请稍后再试。")
    try:
        brief = await interpret_market()
        if brief.kind == "no_data":
            await _finish(brief_cmd, event, "盘面数据还在积累，稍后再来看看。")
        if brief.kind == "template":
            await _finish(brief_cmd, event, f"{brief.content}\n（模板版解读）")
        await _finish(
            brief_cmd,
            event,
            f"{brief.content}\n────────\n💡 历史情景概率，不构成交易建议",
        )
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Market brief command failed error=%s", type(exc).__name__)
        await _finish(brief_cmd, event, "盘面解读这次没生成好，请稍后再试。")


__all__ = ["brief_cmd", "handle_brief", "handle_movement", "movement_cmd"]

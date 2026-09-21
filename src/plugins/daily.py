"""Template-rendered market reports built strictly from local data.

Reports prefer the PNG card (with a 24h/7d ranking); the text report is the
fallback when rendering fails.  Both render exclusively from the structured
local report data — no LLM and no live market request.
"""

from __future__ import annotations

import asyncio
import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.rate_limit import SlidingWindowLimiter
from src.services.card_renderer import render_daily_card
from src.services.market_report import build_report_data, render_report_text

from .chat.media import send_generated_image
from .feature_gate import audited_finish, check_feature, record_event_usage

_log = logging.getLogger("taffy.daily")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)
_DISABLED_MESSAGE = (
    "⏸️ 日报/周报正在重建，本次不生成。\n"
    "旧版本无法证明历史点对应准确的1日/7日，也不会再让聊天模型补写趋势。"
)


def _is_report_request(event: Event) -> bool:
    return isinstance(event, (GroupMessageEvent, PrivateMessageEvent)) and (
        event.get_plaintext().strip() in {"日报", "周报"}
    )


daily_cmd = on_message(rule=Rule(_is_report_request), priority=7, block=True)


@daily_cmd.handle()
async def handle_report(event: MessageEvent, bot: Bot | None = None) -> None:
    if not await check_feature(daily_cmd, event, "market"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await audited_finish(daily_cmd, event, "market", "报告查询太频繁了，请稍后再试。")
    kind = "weekly" if event.get_plaintext().strip() == "周报" else "daily"
    try:
        data = await build_report_data(kind)
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Market report failed error=%s", type(exc).__name__)
        data = None
    if data is None:
        await audited_finish(daily_cmd, event, "market", _DISABLED_MESSAGE)

    if bot is None:
        await audited_finish(daily_cmd, event, "market", render_report_text(data))
    try:
        sent = await send_generated_image(
            bot, event, await asyncio.to_thread(render_daily_card, data)
        )
        if sent:
            # The card path previously skipped the usage audit entirely.
            input_text = event.get_plaintext().strip()
            await record_event_usage(
                event=event,
                feature="market",
                input_text=input_text,
                output_text="[daily-card]",
            )
            return
    except MatcherException:
        raise
    except Exception as exc:
        _log.warning("Daily card render failed error=%s; falling back to text", type(exc).__name__)
    await audited_finish(daily_cmd, event, "market", render_report_text(data))


__all__ = ["_DISABLED_MESSAGE", "_is_report_request", "daily_cmd", "handle_report"]

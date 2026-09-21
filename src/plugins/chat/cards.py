"""Market snapshot card commands rendered from local data only."""

from __future__ import annotations

import asyncio
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
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.card_renderer import render_broad_card, render_item_card
from src.services.index_history import index_history_store
from src.services.market_history import market_history

from ..feature_gate import check_feature, record_event_usage
from .media import send_generated_image

_log = logging.getLogger("taffy.market_card")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)


def _is_card_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return text == "卡片大盘" or (text.startswith("卡片 ") and len(text) > 3)


card_cmd = on_message(rule=Rule(_is_card_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="market",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await card_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@card_cmd.handle()
async def handle_card(bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(card_cmd, event, "market"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "卡片查询太频繁了，请稍后再试。")
    try:
        if text == "卡片大盘":
            closes = await index_history_store.load_closes()
            if len(closes) < 2:
                await _finish(event, "大盘日线还在积累，暂不能出卡片。")
            await send_generated_image(
                bot, event, await asyncio.to_thread(render_broad_card, closes)
            )
            return

        query = text.removeprefix("卡片").strip()
        from . import resolve_name

        market_name = await resolve_name(query)
        if market_name is None:
            from .market import resolution_failure_message

            await _finish(event, resolution_failure_message(query))
        snapshot = await market_history.item_snapshot(market_name)
        if snapshot is None:
            await _finish(
                event,
                "本地样本池还没有这个饰品的快照，卡片只支持样本池内饰品，先用“查询”看实时价。",
            )
        await send_generated_image(
            bot, event, await asyncio.to_thread(render_item_card, market_name, snapshot)
        )
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Card command failed error=%s", type(exc).__name__)
        await _finish(event, "卡片这次没画出来，请稍后再试。")


__all__ = ["card_cmd", "handle_card"]

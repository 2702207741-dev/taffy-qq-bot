"""``查磨损 检视链接`` — SteamDT float/seed/sticker wear lookup.

A thin adapter: detection, fetch and rendering all live in
``src.services.steamdt_wear``; every failure mode answers honestly
instead of improvising a number.
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

from ..feature_gate import check_feature, record_event_usage

_log = logging.getLogger("taffy.wear_commands")
_RATE_LIMITER = SlidingWindowLimiter(5, 60)
_PREFIXES = ("查磨损",)
_MAX_BODY = 600


def _is_wear_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return any(text.startswith(prefix) for prefix in _PREFIXES)


wear_cmd = on_message(rule=Rule(_is_wear_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip()[:200],
        output_text=text,
    )
    await wear_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@wear_cmd.handle()
async def handle_wear(_bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(wear_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "查磨损查询太频繁了，请稍后再试。")
    body = text
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            body = text[len(prefix) :].strip()
            break
    if not body or len(body) > _MAX_BODY:
        body = ""
    from src.services.steamdt_wear import build_wear_reply

    try:
        reply = await build_wear_reply(body)
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Wear command failed error=%s", type(exc).__name__)
        await _finish(event, "查磨损这次没查出来，请稍后再试。")
    await _finish(event, reply)


__all__ = ["handle_wear", "wear_cmd"]

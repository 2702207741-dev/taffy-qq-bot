"""``开箱EV 箱子名`` — case-opening expected value over verified quotes.

A thin adapter: the reply is rendered entirely by ``src.services.case_ev``
from cached metadata and validated SteamDT quotes.  No LLM participates;
every failure mode answers honestly instead of improvising a number.
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
from .routing import persona_scope

_log = logging.getLogger("taffy.case_ev_commands")
_RATE_LIMITER = SlidingWindowLimiter(3, 60)
_PREFIXES = ("开箱EV", "开箱ev", "开箱ev ")
_MAX_QUERY = 60


def _is_case_ev_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return any(text.startswith(prefix) for prefix in _PREFIXES)


case_ev_cmd = on_message(rule=Rule(_is_case_ev_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip()[:200],
        output_text=text,
    )
    await case_ev_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@case_ev_cmd.handle()
async def handle_case_ev(_bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(case_ev_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "开箱EV 查询太频繁了，请稍后再试。")
    query = ""
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            query = text[len(prefix) :].strip()
            break
    if not query or len(query) > _MAX_QUERY:
        await _finish(event, "格式：开箱EV 箱子名（例如：开箱EV 梦魇武器箱）")
    scope = persona_scope(event)
    if scope.group_id:
        # Market data is public, but the upstream fan-out is heavy: keep the
        # per-scope rate limit honest without leaking anything about callers.
        _log.debug("case EV group query scope=%d", scope.group_id)
    from src.services.case_ev import build_case_ev_reply

    try:
        reply = await build_case_ev_reply(query)
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Case EV command failed error=%s", type(exc).__name__)
        await _finish(event, "开箱EV 这次没算出来，请稍后再试。")
    await _finish(event, reply)


__all__ = ["case_ev_cmd", "handle_case_ev"]

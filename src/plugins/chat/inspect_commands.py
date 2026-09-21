"""``检视 检视链接`` — SteamDT front/back/detail screenshots.

A thin adapter over ``src.services.steamdt_inspect``: quota, wear-first
chain and honest failures live in the service; this matcher only moves
bytes through the guarded image path.
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
from .media import send_generated_image

_log = logging.getLogger("taffy.inspect_commands")
_RATE_LIMITER = SlidingWindowLimiter(3, 300)
_PREFIXES = ("检视",)
_MAX_BODY = 600


def _is_inspect_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return any(text.startswith(prefix) for prefix in _PREFIXES)


inspect_cmd = on_message(rule=Rule(_is_inspect_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip()[:200],
        output_text=text,
    )
    await inspect_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@inspect_cmd.handle()
async def handle_inspect(bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(inspect_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "检视查询太频繁了，请稍后再试。")
    body = text
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            body = text[len(prefix) :].strip()
            break
    if not body or len(body) > _MAX_BODY:
        body = ""
    from src.services.steamdt_inspect import build_inspect_result

    try:
        caption, images = await build_inspect_result(body, event.user_id)
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Inspect command failed error=%s", type(exc).__name__)
        await _finish(event, "检视这次没生成出来，请稍后再试。")
    for png in images[:3]:
        await send_generated_image(bot, event, png)
    await _finish(event, caption)


__all__ = ["handle_inspect", "inspect_cmd"]

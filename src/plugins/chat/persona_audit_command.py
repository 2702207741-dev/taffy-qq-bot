"""Administrator command for the persona drift audit (V0.5).

``塔菲人格体检`` runs one InCharacter-style scoring pass over recent reply
samples.  Superuser-only, factory-off behind ``TAFFY_PERSONA_AUDIT_ENABLED``
and fail-closed: the report either carries judge-calibrated numbers or an
honest refusal.
"""

from __future__ import annotations

import re

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter

from ..feature_gate import check_feature

_AUDIT_LIMITER = SlidingWindowLimiter(1, 300)


def _superusers() -> set[str]:
    configured: object = getattr(get_driver().config, "superusers", set())
    if not isinstance(configured, (set, frozenset, list, tuple)):
        return set()
    return {str(value) for value in configured}


def _is_audit_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    if str(event.user_id) not in _superusers():
        return False
    return bool(re.fullmatch(r"塔菲人格体检", event.get_plaintext().strip()))


persona_audit_cmd = on_message(rule=Rule(_is_audit_request), priority=7, block=True)


@persona_audit_cmd.handle()
async def handle_persona_audit(_bot: Bot, event: MessageEvent) -> None:
    import asyncio

    from src.services.persona_eval import format_audit_report, run_persona_audit

    if not await check_feature(persona_audit_cmd, event, "chat"):
        return
    if not await _AUDIT_LIMITER.allow(str(event.user_id)):
        await persona_audit_cmd.finish(text_message("体检刚跑过一次，稍等几分钟再来喵。"))
        return
    await persona_audit_cmd.send(
        text_message("开始体检：先校准评委，再逐项打分，大约需要半分钟喵。")
    )
    try:
        async with asyncio.timeout(180):
            summary = await run_persona_audit()
    except TimeoutError:
        summary = {"status": "judge_failed", "completed": 0}
    await persona_audit_cmd.finish(text_message(format_audit_report(summary)))


__all__ = ["handle_persona_audit", "persona_audit_cmd"]

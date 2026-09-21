"""Shared feature-permission gate and usage-audit helpers for plugins.

Plugins call :func:`check_feature` at the top of every command handler; a
denied user receives a static notice and the handler stops.  Audit records
are best-effort: a failed write is logged and never blocks the command.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, MessageEvent
from nonebot.matcher import Matcher

from src.core.messages import text_message
from src.services.authority import DENIED_MESSAGE
from src.state.runtime import authority, usage_log

_log = logging.getLogger("taffy.feature_gate")


def _group_id(event: Event) -> int:
    return int(event.group_id) if isinstance(event, GroupMessageEvent) else 0


async def check_feature(matcher: type[Matcher], event: MessageEvent, feature: str) -> bool:
    """Finish with a denial notice when ``feature`` is not allowed."""

    user_id = int(event.user_id)
    if await authority.allowed(user_id, feature):
        return True
    await matcher.finish(text_message(DENIED_MESSAGE))
    return False


async def audited_finish(
    matcher: type[Matcher],
    event: MessageEvent,
    feature: str,
    text: str,
) -> NoReturn:
    """Send ``text`` and append it (with the raw input) to the audit log."""

    await record_event_usage(
        event=event,
        feature=feature,
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await matcher.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


async def record_usage(
    *,
    user_id: int,
    group_id: int = 0,
    feature: str,
    command: str,
    input_text: str,
    output_text: str | None = None,
) -> None:
    """Best-effort append to the audit log; never raise into the caller."""

    try:
        await usage_log.record(
            user_id=user_id,
            group_id=group_id,
            feature=feature,
            command=command,
            input_text=input_text,
            output_text=output_text,
        )
    except Exception as exc:
        _log.warning(
            "Usage audit record failed user=%s feature=%s error=%s",
            user_id,
            feature,
            type(exc).__name__,
        )


async def record_event_usage(
    event: MessageEvent,
    *,
    feature: str,
    input_text: str,
    output_text: str | None = None,
) -> None:
    """Record usage derived from a OneBot event."""

    await record_usage(
        user_id=int(event.user_id),
        group_id=_group_id(event),
        feature=feature,
        command=input_text[:200],
        input_text=input_text,
        output_text=output_text,
    )

"""Safe OneBot message construction helpers.

OneBot parses ``[CQ:...]`` sequences found in plain strings.  Every value that
can contain user, search-engine, or model-controlled text must therefore be
wrapped in an explicit text segment before it is sent.
"""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import Message, MessageSegment


def text_message(value: object) -> Message:
    """Return *value* as exactly one text segment, never parsed as CQ code."""

    return Message(MessageSegment.text(str(value)))


def text_with_at(user_id: int | str, value: object) -> Message:
    """Build a structured mention followed by untrusted plain text."""

    return MessageSegment.at(int(user_id)) + MessageSegment.text(f" {value}")


def split_text(value: object, limit: int = 800) -> list[str]:
    """Split long QQ text on line boundaries without dropping characters."""

    if limit < 2:
        raise ValueError("limit must be at least 2")

    remaining = str(value)
    if not remaining:
        return [""]

    parts: list[str] = []
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    parts.append(remaining)
    return parts

"""Expression-dictionary service: prompt injection plus optional mining.

Injection always honours the learning master switch.  The frequency miner
stays factory-off (``TAFFY_SLANG_AUTOMINE_ENABLED``) and — even when on —
only ever proposes ``pending`` candidates built from isolated tokens; it
never stores the surrounding sentences.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter

from .config import runtime_setting
from .learning import learning_enabled

logger = logging.getLogger("taffy.services.slang")

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,6}")
#: Command vocabulary and generic words never become "group slang".
_TOKEN_STOPWORDS = frozenset(
    {
        "大盘",
        "查询",
        "饰品",
        "K线",
        "卡片",
        "预警",
        "自选",
        "库存",
        "帮助",
        "菜单",
        "日报",
        "周报",
        "预测",
        "开箱",
        "炼金",
        "新闻",
        "爆料",
        "一个",
        "什么",
        "怎么",
        "可以",
        "没有",
        "不是",
        "这个",
        "那个",
    }
)


def automine_enabled() -> bool:
    raw = runtime_setting("TAFFY_SLANG_AUTOMINE_ENABLED")
    if raw is None:
        return False
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


async def prompt_terms(
    scope_group_id: int, *, limit: int = 20, created_by: int | None = None
) -> tuple[str, ...]:
    """Accepted terms formatted for the persona prompt, or empty when off.

    ``limit`` comes from the intimacy-band allowance: closer scopes may
    draw from more taught expressions.  ``created_by`` restricts a private
    (``scope_group_id == 0``) read to one user; without it every user's
    privately taught terms would be injected into this user's prompt.
    """

    if not learning_enabled():
        return ()
    from src.state.runtime import persona_slang

    entries = await persona_slang.accepted_for_injection(
        scope_group_id, limit=limit, created_by=created_by
    )
    return tuple(f"{entry.term}（{entry.meaning}）" for entry in entries)


#: User-facing correction phrasings that disable a taught term.  The reply
#: to a bot utterance like "不可以/达咩/别用XX" is the community-proven
#: negative-feedback channel (nonebot-plugin-learning-chat style).
_NEGATION_MARKERS = ("别用", "不要用", "别再说", "不要再说", "别说", "不可以说", "达咩", "不可以")
_NEGATION_MIN_CHARS = 2
_NEGATION_MAX_CHARS = 120


def _negation_target(message: str) -> str:
    """Return the message when it reads as a correction, else empty."""

    text = message.strip()
    if not _NEGATION_MIN_CHARS <= len(text) <= _NEGATION_MAX_CHARS:
        return ""
    lowered = text.casefold()
    return text if any(marker in lowered or marker in text for marker in _NEGATION_MARKERS) else ""


async def negative_feedback(
    scope_group_id: int, message: str, *, owner_user_id: int | None = None
) -> str | None:
    """Disable an accepted term the user just corrected; None when unrelated.

    Deterministic and zero-LLM: the message must contain a negation marker
    AND one of this scope's accepted terms.  The record is soft-disabled
    (never destroyed) and stays re-enableable via ``启用话术``.
    """

    if not learning_enabled():
        return None
    correction = _negation_target(message)
    if not correction:
        return None
    from src.state.runtime import persona_slang

    accepted = await persona_slang.accepted_for_injection(scope_group_id, limit=50)
    if not accepted:
        return None
    for entry in accepted:
        if entry.term in correction:
            disabled = await persona_slang.set_enabled(
                entry.id,
                enabled=False,
                scope_group_id=scope_group_id,
                created_by=owner_user_id,
            )
            if disabled is not None:
                return f"好啦，taffy以后不说“{entry.term}”了喵。"
    return None


async def _read_chat_inputs(since: int, limit: int) -> list[str]:
    """Raw audit-log inputs for mining; patch seam for tests."""

    from src.state.runtime import database

    rows = await database.read(
        """
        SELECT input_text FROM usage_log
        WHERE created_at >= ? AND feature = 'chat'
        ORDER BY id DESC LIMIT ?
        """,
        (since, limit),
    )
    return [str(row["input_text"]) for row in rows]


async def automine_once(*, now: float | None = None, limit: int = 5) -> int:
    """Propose pending candidates from word frequencies; returns created count."""

    if not learning_enabled() or not automine_enabled():
        return 0
    from src.state.runtime import persona_slang

    current = int(time.time()) if now is None else int(now)
    since = current - 86_400
    inputs = await _read_chat_inputs(since, 1_000)
    if len(inputs) < 12:
        return 0  # too small a sample to mean anything

    counter: Counter[str] = Counter()
    seen_lines: set[str] = set()
    for text in inputs:
        line = text.strip()
        if not line or line in seen_lines:
            continue
        seen_lines.add(line)
        for token in _TOKEN_RE.findall(line):
            if token not in _TOKEN_STOPWORDS:
                counter[token] += 1

    candidates = [
        token for token, count in counter.most_common(40) if count >= 3 and len(token) >= 2
    ]
    created = 0
    for term in candidates:
        if created >= max(1, int(limit)):
            break
        try:
            # The schema requires created_by > 0; automated proposals use the
            # reserved system author id 1 and say so in the meaning text.
            #
            # Sanitisation note (M-09): prompt-boundary escaping happens at the
            # **exit** (``persona._escape_untrusted`` inside ``build_persona_prompt``),
            # so every writer — QQ ``/学话``, desktop teach, and this miner — is
            # covered without repeating the escape here.  A future write path must
            # therefore still go through ``teach()`` (which enforces length/format);
            # a path that writes the table directly would need its own call into the
            # same exit function.  Deliberately **no test** for this path: it has no
            # production caller today (see the batch-2 prep note), and a test for a
            # path nothing runs would pin behaviour nobody exercises.
            _, is_new = await persona_slang.teach(
                0,
                created_by=1,
                term=term,
                meaning="自动发现的高频词，待确认含义",
            )
        except Exception as exc:  # noqa: BLE001 - mining must never raise
            logger.warning("Slang mining insert skipped error=%s", type(exc).__name__)
            continue
        if is_new:
            created += 1
    return created


__all__ = [
    "automine_enabled",
    "automine_once",
    "negative_feedback",
    "prompt_terms",
]

"""Offline short-term-memory compression (V0.4 P3).

When enabled, one LLM call compacts the older part of a chat session into a
durable summary so the persona prompt can carry ``[摘要层] + [近期原文]``
instead of losing everything beyond the verbatim window.  Every invocation
is gated, throttled, budget-capped and single-flight; any failure simply
means "no summary" and never blocks or degrades the chat reply.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import bounded_env_int, runtime_setting

logger = logging.getLogger("taffy.services.history_summary")

_MIN_SPAN = 8
_SUMMARY_MAX_TOKENS = 500
_TRANSCRIPT_LIMIT = 6_000
_HOURLY = 3_600.0

_SUMMARY_PROMPT = (
    "把下面这段群聊/私聊记录压缩成一段中文备忘（不超过 300 字），"
    "只保留稳定的事实与共识：用户的称呼与偏好、讨论过的话题与结论、"
    "答应过或请求过的事。禁止编造记录里没有的内容，禁止评价用户，"
    "禁止出现除原文数字以外的其他数字。直接输出备忘正文。"
)

_inflight: dict[str, asyncio.Task[None]] = {}
_inflight_lock = asyncio.Lock()
_last_attempt: dict[str, float] = {}


def _enabled_setting() -> bool:
    raw = runtime_setting("TAFFY_HISTORY_COMPRESSION_ENABLED")
    if raw is None:
        return False
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _pilot_users() -> set[int]:
    raw = runtime_setting("TAFFY_SUMMARY_PILOT_USERS")
    if not raw:
        return set()
    users: set[int] = set()
    for part in str(raw).split(","):
        token = part.strip()
        if token.isdigit():
            users.add(int(token))
    return users


def user_id_from_key(session_key: str) -> int | None:
    """Extract the QQ id from ``p_<uid>`` / ``g_<gid>_<uid>`` session keys."""

    text = session_key.strip()
    if text.startswith("p_"):
        tail = text[2:]
    elif text.startswith("g_"):
        tail = text.rsplit("_", 1)[-1]
    else:
        return None
    return int(tail) if tail.isdigit() else None


def compression_allows(session_key: str) -> bool:
    """Master gate: env switch plus the pilot whitelist."""

    if not _enabled_setting():
        return False
    users = _pilot_users()
    if not users:
        return True
    uid = user_id_from_key(session_key)
    return uid is not None and uid in users


def _daily_budget() -> int:
    return bounded_env_int("TAFFY_SUMMARY_DAILY_BUDGET", 50, 1, 1_000)


async def load_summary(session_key: str) -> str | None:
    """Return the latest persisted summary text, or None."""

    from src.state.runtime import session_summaries

    summary = await session_summaries.latest(session_key)
    return summary.summary if summary is not None else None


def schedule_refresh(session_key: str) -> None:
    """Fire-and-forget a summary refresh; never raises, never blocks."""

    if not compression_allows(session_key):
        return
    existing = _inflight.get(session_key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_refresh_quietly(session_key))
    _inflight[session_key] = task


async def _refresh_quietly(session_key: str) -> None:
    try:
        await refresh(session_key)
    except Exception as exc:
        logger.warning(
            "Summary refresh failed key_present=%s error=%s", bool(session_key), type(exc).__name__
        )
    finally:
        async with _inflight_lock:
            current = _inflight.get(session_key)
            if current is not None and current.done():
                _inflight.pop(session_key, None)


async def refresh(session_key: str) -> bool:
    """Summarize the uncovered older span of one session; True when stored."""

    from src.state.runtime import session_summaries, sessions

    if not compression_allows(session_key):
        return False
    async with _inflight_lock:
        existing = _inflight.get(session_key)
        if existing is not None and not existing.done():
            return False
        now = time.monotonic()
        last = _last_attempt.get(session_key)
        # ``monotonic()`` starts near zero at process start, so a missing
        # entry must not be treated as "attempted at t=0" — that would
        # throttle every first refresh in a freshly started process
        # (CI runs a brand-new pytest process every time).
        if last is not None and now - last < _HOURLY:
            return False
        _last_attempt[session_key] = now

    summary = await session_summaries.latest(session_key)
    oldest, newest = await sessions.id_boundary(session_key)
    if oldest is None or newest is None:
        print("DIAG refresh boundary refused", session_key)
        return False
    covers_from = (summary.covers_to_id + 1) if summary is not None else oldest
    # Leave the newest few messages verbatim; they are the live context.
    covers_to = newest - 4
    if covers_to - covers_from + 1 < _MIN_SPAN:
        logger.info(
            "Summary refresh refused span session=%s span=%d",
            session_key,
            covers_to - covers_from + 1,
        )
        return False

    budget_day = int(time.time()) // 86400 * 86400
    used = await session_summaries.count_since(budget_day)
    if used >= _daily_budget():
        logger.info("Summary budget exhausted used=%d", used)
        return False

    transcript_messages = await sessions.range_messages(
        session_key, from_id=covers_from, to_id=covers_to
    )
    if len(transcript_messages) < _MIN_SPAN:
        return False
    lines = [
        f"{'用户' if message['role'] == 'user' else '塔菲'}: {message['content']}"
        for message in transcript_messages
    ]
    transcript = "\n".join(lines)
    if len(transcript) > _TRANSCRIPT_LIMIT:
        transcript = transcript[-_TRANSCRIPT_LIMIT:]

    from .llm import try_anthropic, untrusted_context

    raw = await try_anthropic(
        _SUMMARY_PROMPT,
        [{"role": "user", "content": untrusted_context(transcript, limit=8000)}],
        _SUMMARY_MAX_TOKENS,
    )
    text = raw.strip()
    if not 2 <= len(text) <= 2000:
        logger.info("Summary rejected length=%d", len(text))
        return False
    await session_summaries.save(
        session_key,
        summary=text,
        covers_from_id=covers_from,
        covers_to_id=covers_to,
    )
    return True


async def wait_for_tests() -> None:
    """Test helper: await every in-flight refresh; production code never calls it."""

    async with _inflight_lock:
        tasks = [task for task in _inflight.values() if not task.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def clear_session(session_key: str) -> None:
    """Drop the durable summary plus any in-flight bookkeeping for the session."""

    from src.state.runtime import session_summaries

    await session_summaries.clear(session_key)
    _last_attempt.pop(session_key, None)
    async with _inflight_lock:
        _inflight.pop(session_key, None)


__all__ = [
    "clear_session",
    "compression_allows",
    "load_summary",
    "refresh",
    "schedule_refresh",
    "user_id_from_key",
    "wait_for_tests",
]

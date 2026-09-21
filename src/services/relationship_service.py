"""Familiarity gating for the persona prompt (V0.4 P3).

A thin, throttled read over :mod:`src.state.persona_relationship_repository`:
at most one recalculation per scope per hour, in-process memo, and a global
kill switch.  The underlying score only ever derives from confirmed
signals (usage counters, accepted impressions, style feedback).
"""

from __future__ import annotations

import logging
import time

from .config import runtime_setting

logger = logging.getLogger("taffy.services.relationship_service")

_HOURLY = 3_600.0
_LAST_REFRESH: dict[tuple[int, int], float] = {}
_LAST_BAND: dict[tuple[int, int], str] = {}


def relationship_enabled() -> bool:
    raw = runtime_setting("TAFFY_RELATIONSHIP_ENABLED")
    if raw is None:
        return True  # privacy-safe metadata; kill switch provided for pilots
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


async def familiarity_for_scope(scope: object) -> str | None:
    """Return the neutral familiarity band for one scope, or None."""

    if scope is None or not relationship_enabled():
        return None
    owner = getattr(scope, "owner_user_id", None)
    group = getattr(scope, "group_id", 0)
    if isinstance(owner, bool) or not isinstance(owner, int) or owner <= 0:
        return None
    key = (owner, int(group) if isinstance(group, int) and not isinstance(group, bool) else 0)
    now = time.monotonic()
    if now - _LAST_REFRESH.get(key, 0.0) < _HOURLY:
        return _LAST_BAND.get(key)
    _LAST_REFRESH[key] = now
    try:
        from src.state.runtime import persona_relationships

        relationship = await persona_relationships.refresh(key[0], key[1], now=int(time.time()))
    except Exception as exc:
        logger.warning("Relationship refresh failed error=%s", type(exc).__name__)
        return _LAST_BAND.get(key)
    _LAST_BAND[key] = relationship.band
    return relationship.band


#: Intimacy pays out in behaviour, not in announcements: how many taught
#: expressions the persona may draw from and which sticker vibes are in
#: taste.  Thresholds mirror the repository band cut-offs and stay neutral
#: (no ranking-style wording ever reaches the user).
_SLANG_LIMIT_BY_BAND = {"陌生": 5, "点头之交": 10, "熟络": 15, "老友": 20}
_DEFAULT_SLANG_LIMIT = 5

#: Old-topic recall (intimacy behaviour phase two): only 老友 scopes may
#: surface a past topic; the probability is bounded and the memory source is
#: the confirmed stores (never raw chat text).
_RECALL_BAND = "老友"
_RECALL_PROBABILITY = 0.35
_MAX_RECALL_CHARS = 120


def relationship_benefits(band: str | None) -> dict[str, int]:
    """Map a familiarity band to its concrete behavioural allowances."""

    if not band:
        return {"slang_limit": _DEFAULT_SLANG_LIMIT}
    return {"slang_limit": _SLANG_LIMIT_BY_BAND.get(band, _DEFAULT_SLANG_LIMIT)}


def recall_allowed(band: str | None, *, roll: float | None = None) -> bool:
    """Phase-two behaviour: may this scope hear an old-topic mention?

    Only the top band qualifies and the decision is probabilistic (a dice
    roll is injected for tests).  This stays inside the M4 anti-harassment
    discipline: at most one recall per conversation, no @-mention, and the
    source is always a confirmed memory.
    """

    if band != _RECALL_BAND:
        return False
    dice = float(roll) if roll is not None else _random()
    return dice < _RECALL_PROBABILITY


async def recall_old_topic(scope: object, message: str) -> str | None:
    """Return a one-line old-topic mention for a 老友 scope, or None.

    Retrieves a confirmed memory (persona memory first, then an accepted
    learning insight) that is not already echoed by the current message.
    The mention is capped in length and phrased as a soft callback —
    ``你还记得…吗`` — never an unsolicited deep dive.
    """

    if scope is None or not relationship_enabled():
        return None
    try:
        band = await familiarity_for_scope(scope)
    except Exception as exc:
        logger.warning("Familiarity refresh failed error=%s", type(exc).__name__)
        return None
    if not recall_allowed(band):
        return None
    try:
        from src.state.persona_memory_repository import PersonaScope
        from src.state.runtime import learning_insights, persona_memories

        pscope = PersonaScope(
            int(getattr(scope, "owner_user_id", 0)),
            int(getattr(scope, "group_id", 0) or 0),
        )
        rows = await persona_memories.list(pscope, limit=20)
        candidates = [row.content for row in rows if row.content not in message]
        if not candidates:
            try:
                insights = await learning_insights.list_scope(pscope.owner_user_id, pscope.group_id)
                candidates = [
                    insight.content
                    for insight in insights
                    if insight.status == "accepted" and insight.content not in message
                ]
            except Exception:
                candidates = []
        if not candidates:
            return None
        topic = candidates[0].strip()
        if len(topic) > _MAX_RECALL_CHARS:
            topic = topic[:_MAX_RECALL_CHARS] + "…"
        return f"对了，你还记得这个吗：{topic}"
    except Exception as exc:
        logger.warning("Old-topic recall failed error=%s", type(exc).__name__)
        return None


def _random() -> float:
    """Test seam: module-level cryptographically-strong random source."""

    import secrets

    return secrets.randbelow(1_000_000) / 1_000_000


def reset_throttle() -> None:
    """Test helper: clear the in-process throttle and memo."""

    _LAST_REFRESH.clear()
    _LAST_BAND.clear()


__all__ = [
    "familiarity_for_scope",
    "recall_allowed",
    "recall_old_topic",
    "relationship_benefits",
    "relationship_enabled",
    "reset_throttle",
]

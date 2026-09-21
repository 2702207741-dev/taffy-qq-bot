"""Session-level floating mood state (V0.5 night batch 3).

Three-layer emotion architecture (message-level intent → session-level
mood → user-level intimacy): this module implements the missing middle
layer.  A session (``g_<group>`` / ``p_<user>``) carries a small set of
floating mood floats that events push upward and idle time decays back to
neutral.

Discipline inherited from AstrBot/MaiBot: mood is *expression-layer only*.
It is injected as a transient "当前心境" block inside the persona prompt —
never appended to the constant persona section, never persisted to the
memory/learning stores, and never used to change what the model knows.

Privacy: mood state lives in memory only (no SQLite table, no chat text is
stored — just a numeric valence/energy pair).  Restart resets to neutral.
"""

from __future__ import annotations

import math
import re
import time

from .config import env_bool, runtime_setting

_VALENCE_CLAMP = 1.0
_ENERGY_CLAMP = 1.0
_DECAY_PER_MINUTE = 0.04
_POSITIVE_EVENT_PUSH = 0.25
_NEGATIVE_EVENT_PUSH = 0.25
_ENERGY_EVENT_PUSH = 0.2
_NEUTRAL = 0.0

#: Session key -> (valence, energy, updated_at)
_STATE: dict[str, tuple[float, float, float]] = {}


def mood_enabled() -> bool:
    """Master switch; factory off so the layer is inert unless opted in."""

    return env_bool("TAFFY_MOOD_ENABLED", False)


def _pilot_users() -> frozenset[int]:
    """Pilot whitelist: explicit users, falling back to SUPERUSERS."""

    explicit = runtime_setting("TAFFY_MOOD_PILOT_USERS") or ""
    values = re.findall(r"\d+", explicit)
    if not values:
        values = re.findall(r"\d+", runtime_setting("SUPERUSERS") or "")
    return frozenset(int(value) for value in values if int(value) > 0)


def mood_allowed_for_user(user_id: object) -> bool:
    """Pilot discipline: factory off → whitelist (falls back to SUPERUSERS)."""

    if not mood_enabled():
        return False
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        return False
    try:
        owner = int(user_id)
    except (TypeError, ValueError):
        return False
    return owner > 0 and owner in _pilot_users()


def _session_key(user_id: int, group_id: int) -> str:
    return f"g_{group_id}" if group_id else f"p_{user_id}"


def _decay(now: float, valence: float, energy: float, updated_at: float) -> tuple[float, float]:
    minutes = max(0.0, (now - updated_at) / 60.0)
    factor = math.exp(-_DECAY_PER_MINUTE * minutes)
    return valence * factor, energy * factor


def observe(
    user_id: int,
    group_id: int,
    *,
    sentiment: str = "neutral",
    energy: float | None = None,
    now: float | None = None,
) -> tuple[float, float]:
    """Push one message event onto the session mood.

    ``sentiment`` maps a lightweight rule/intent label to a valence push;
    positive events raise valence, negative lower it, neutral decays only.
    Returns the new ``(valence, energy)`` pair (clamped to [-1, 1]).
    """

    if not mood_allowed_for_user(user_id):
        return _NEUTRAL, _NEUTRAL
    clock = now if now is not None else time.time()
    key = _session_key(user_id, group_id)
    valence, energy, updated_at = _STATE.get(key, (_NEUTRAL, _NEUTRAL, clock))
    valence, energy = _decay(clock, valence, energy, updated_at)
    if sentiment in ("positive", "joy", "praise"):
        valence = valence + _POSITIVE_EVENT_PUSH
        energy = energy + _ENERGY_EVENT_PUSH
    elif sentiment in ("negative", "sad", "complaint"):
        valence = valence - _NEGATIVE_EVENT_PUSH
        energy = energy - _ENERGY_EVENT_PUSH * 0.5
    elif energy is not None:
        energy = energy + energy
    else:
        energy = energy + 0.0  # neutral message: no push, pure decay
    valence = max(-_VALENCE_CLAMP, min(_VALENCE_CLAMP, valence))
    energy = max(-_ENERGY_CLAMP, min(_ENERGY_CLAMP, energy))
    _STATE[key] = (valence, energy, clock)
    return valence, energy


def current(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> tuple[float, float]:
    """Current (valence, energy) after idle decay; neutral when disabled."""

    if not mood_allowed_for_user(user_id):
        return _NEUTRAL, _NEUTRAL
    clock = now if now is not None else time.time()
    key = _session_key(user_id, group_id)
    valence, energy, updated_at = _STATE.get(key, (_NEUTRAL, _NEUTRAL, clock))
    return _decay(clock, valence, energy, updated_at)


def mood_label(
    user_id: int,
    group_id: int,
    *,
    now: float | None = None,
) -> str | None:
    """Human-readable one-liner for the expression layer; None when off/neutral."""

    if not mood_allowed_for_user(user_id):
        return None
    valence, energy = current(user_id, group_id, now=now)
    if abs(valence) < 0.15 and abs(energy) < 0.15:
        return None
    if valence >= 0.15:
        if energy >= 0.15:
            return "心情不错，兴致挺高"
        return "心情还算不错"
    if valence <= -0.15:
        if energy <= -0.15:
            return "有点提不起劲"
        return "心情有点低落"
    if energy >= 0.3:
        return "今天很有精神"
    return None


def reset_runtime_state() -> None:
    """Test helper: clear all in-memory mood state."""

    _STATE.clear()


__all__ = [
    "current",
    "mood_allowed_for_user",
    "mood_enabled",
    "mood_label",
    "observe",
    "reset_runtime_state",
]

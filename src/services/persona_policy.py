"""Feature policy for the reversible persona V3 pilot.

Flags are read lazily at decision time so operators can flip the rollout
through the environment without restarting the process.
"""

from __future__ import annotations

import re

from src.state.persona_memory_repository import PersonaScope

from .config import runtime_setting

logger = __import__("logging").getLogger("taffy.services.persona_policy")


def _flag(name: str, default: bool = False) -> bool:
    """Boolean ``runtime_setting`` read resolved through this module's globals."""

    raw = runtime_setting(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning("Ignoring invalid boolean setting %s", name)
    return default


def _pilot_users() -> frozenset[int]:
    explicit = runtime_setting("TAFFY_PERSONA_V3_PILOT_USERS") or ""
    values = re.findall(r"\d+", explicit)
    if not values:
        values = re.findall(r"\d+", runtime_setting("SUPERUSERS") or "")
    return frozenset(int(value) for value in values if int(value) > 0)


def persona_v3_enabled_for_scope(scope: PersonaScope | None) -> bool:
    return bool(
        _flag("TAFFY_PERSONA_V3_ENABLED")
        and scope is not None
        and (_flag("TAFFY_PERSONA_V3_FULL_ROLLOUT") or scope.owner_user_id in _pilot_users())
    )


__all__ = [
    "persona_v3_enabled_for_scope",
]

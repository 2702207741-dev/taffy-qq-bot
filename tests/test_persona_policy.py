from __future__ import annotations

from unittest.mock import patch

from src.services import persona_policy
from src.state.persona_memory_repository import PersonaScope


def _patched(*, enabled: bool, rollout: bool = False) -> object:
    settings: dict[str, str] = {
        "TAFFY_PERSONA_V3_ENABLED": "true" if enabled else "false",
        "TAFFY_PERSONA_V3_FULL_ROLLOUT": "true" if rollout else "false",
        "TAFFY_PERSONA_V3_PILOT_USERS": "1001",
    }
    return patch.object(
        persona_policy,
        "runtime_setting",
        lambda name: settings.get(name),
    )


def test_v3_policy_is_off_by_default_and_then_owner_only_until_full_rollout() -> None:
    owner = PersonaScope(1001)
    other = PersonaScope(1002)
    with _patched(enabled=False):
        assert not persona_policy.persona_v3_enabled_for_scope(owner)

    with _patched(enabled=True):
        assert persona_policy.persona_v3_enabled_for_scope(owner)
        assert not persona_policy.persona_v3_enabled_for_scope(other)

    with _patched(enabled=True, rollout=True):
        assert persona_policy.persona_v3_enabled_for_scope(other)

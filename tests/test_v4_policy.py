from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.services import v4_policy


def _patched(*, enabled: bool, rollout: bool = False, until: str | None = None):
    """Patch the lazy V4 environment flags for one scenario."""

    settings: dict[str, str] = {
        "TAFFY_V4_ENABLED": "true" if enabled else "false",
        "TAFFY_V4_FULL_ROLLOUT": "true" if rollout else "false",
        "TAFFY_V4_PILOT_USERS": "1243607273",
    }
    if until is not None:
        settings["TAFFY_V4_PILOT_UNTIL"] = until

    def fake_runtime_setting(name: str) -> str | None:
        return settings.get(name)

    return patch.object(v4_policy, "runtime_setting", fake_runtime_setting)


def test_v4_policy_is_reversible_and_owner_scoped() -> None:
    with _patched(enabled=True):
        assert v4_policy.v4_enabled_for_user(1243607273)
        assert not v4_policy.v4_enabled_for_user(10001)
        assert not v4_policy.v4_enabled_for_user("bad")
    with _patched(enabled=True, rollout=True):
        assert v4_policy.v4_enabled_for_user(10001)
    with _patched(enabled=False):
        assert not v4_policy.v4_enabled_for_user(1243607273)


def test_v4_pilot_expires_at_the_configured_deadline() -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with _patched(enabled=True, until=past):
        assert not v4_policy.v4_enabled_for_user(1243607273)

    with _patched(enabled=True, until=future):
        assert v4_policy.v4_enabled_for_user(1243607273)

    # Full rollout is never blocked by the pilot deadline.
    with _patched(enabled=True, rollout=True, until=past):
        assert v4_policy.v4_enabled_for_user(10001)


def test_invalid_pilot_deadline_falls_back_to_unbounded() -> None:
    with patch.object(v4_policy, "runtime_setting", return_value=""):
        assert v4_policy._pilot_deadline() is None
    with patch.object(v4_policy, "runtime_setting", return_value="not-a-date"):
        assert v4_policy._pilot_deadline() is None
    with patch.object(
        v4_policy,
        "runtime_setting",
        return_value="2026-08-10T00:00:00Z",
    ):
        assert v4_policy._pilot_deadline() == datetime(2026, 8, 10, tzinfo=timezone.utc)


def test_only_new_v4_surfaces_require_the_pilot() -> None:
    for command in ("综合指数", "异动", "预测", "热度榜", "K线高清", "K线高清 红线 久经"):
        assert v4_policy.command_requires_v4_pilot(command)
    for command in ("大盘", "查询 红线 久经", "K线", "库存查询 7656119"):
        assert not v4_policy.command_requires_v4_pilot(command)

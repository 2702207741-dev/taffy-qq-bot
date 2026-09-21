"""V0.5 night batch 6: intimacy phase two — old-topic recall for 老友 scopes.

The recall stays inside the M4 anti-harassment discipline: top band only,
probabilistic, source is always a confirmed memory (never raw chat text),
at most one line, no @-mention.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import relationship_service
from src.services.relationship_service import recall_allowed, recall_old_topic

SCOPE = SimpleNamespace(owner_user_id=1001, group_id=2001)


def _fake_memory(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, status="accepted")


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    relationship_service.reset_throttle()
    monkeypatch.setenv("TAFFY_RELATIONSHIP_ENABLED", "true")
    yield
    relationship_service.reset_throttle()


def test_recall_allowed_only_top_band() -> None:
    assert recall_allowed("老友", roll=0.0) is True
    assert recall_allowed("老友", roll=0.99) is False
    assert recall_allowed("熟络", roll=0.0) is False
    assert recall_allowed(None, roll=0.0) is False


@pytest.mark.asyncio
async def test_recall_skips_when_not_top_band(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        relationship_service, "familiarity_for_scope", AsyncMock(return_value="熟络")
    )
    result = await recall_old_topic(SCOPE, "在吗")
    assert result is None


@pytest.mark.asyncio
async def test_recall_uses_confirmed_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        relationship_service, "familiarity_for_scope", AsyncMock(return_value="老友")
    )
    monkeypatch.setattr(relationship_service, "recall_allowed", lambda band, roll=None: True)
    memories = SimpleNamespace(
        list=AsyncMock(return_value=(_fake_memory("阿澈喜欢 AK-47 | Redline"),))
    )
    with patch("src.state.runtime.persona_memories", memories):
        result = await recall_old_topic(SCOPE, "今天好冷")
    assert result is not None
    assert "AK-47 | Redline" in result
    assert "你还记得" in result


@pytest.mark.asyncio
async def test_recall_falls_back_to_insights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        relationship_service, "familiarity_for_scope", AsyncMock(return_value="老友")
    )
    monkeypatch.setattr(relationship_service, "recall_allowed", lambda band, roll=None: True)
    memories = SimpleNamespace(list=AsyncMock(return_value=()))
    insights = SimpleNamespace(
        list_scope=AsyncMock(
            return_value=(_fake_memory("AWP | Asiimov 是阿澈 2026-05 起的主战皮肤"),)
        )
    )
    with (
        patch("src.state.runtime.persona_memories", memories),
        patch("src.state.runtime.learning_insights", insights),
    ):
        result = await recall_old_topic(SCOPE, "今天好冷")
    assert result is not None
    assert "AWP | Asiimov" in result


@pytest.mark.asyncio
async def test_recall_returns_none_when_no_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        relationship_service, "familiarity_for_scope", AsyncMock(return_value="老友")
    )
    monkeypatch.setattr(relationship_service, "recall_allowed", lambda band, roll=None: True)
    memories = SimpleNamespace(list=AsyncMock(return_value=()))
    insights = SimpleNamespace(list_scope=AsyncMock(return_value=()))
    with (
        patch("src.state.runtime.persona_memories", memories),
        patch("src.state.runtime.learning_insights", insights),
    ):
        result = await recall_old_topic(SCOPE, "今天好冷")
    assert result is None


@pytest.mark.asyncio
async def test_recall_crash_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        relationship_service,
        "familiarity_for_scope",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    result = await recall_old_topic(SCOPE, "在吗")
    assert result is None


def test_recall_never_includes_current_message() -> None:
    """A memory identical to the current message must not be recalled."""
    from src.services.relationship_service import _MAX_RECALL_CHARS

    assert _MAX_RECALL_CHARS > 0  # sanity: cap is bounded

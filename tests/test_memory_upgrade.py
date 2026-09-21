"""V0.4 P3: memory compression and relationship intimacy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.core.database import StateDatabase
from src.plugins import chat
from src.services.history_summary import (
    clear_session,
    compression_allows,
    load_summary,
    refresh,
    user_id_from_key,
)
from src.services.persona import build_persona_prompt
from src.state.persona_memory_repository import PersonaMemoryRepository, PersonaScope
from src.state.persona_relationship_repository import (
    PersonaRelationshipRepository,
    band_for,
    compute_intimacy,
)
from src.state.runtime import session_summaries as runtime_summaries
from src.state.runtime import sessions as runtime_sessions
from src.state.session_repository import SessionRepository
from src.state.session_summary_repository import SessionSummaryRepository

# ---------------------------------------------------------------------------
# Session summary repository
# ---------------------------------------------------------------------------


async def test_summary_repo_roundtrip_replace_clear_budget(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    summaries = SessionSummaryRepository(database)
    sessions = SessionRepository(database, legacy_path=tmp_path / "absent.json")
    await summaries.initialize()
    await sessions.initialize()

    assert await summaries.latest("p_1001") is None

    for index in range(3):
        await sessions.append_turn("p_1001", f"问{index}", f"答{index}")
    oldest, newest = await sessions.id_boundary("p_1001")
    assert oldest is not None and newest is not None

    saved = await summaries.save(
        "p_1001", summary="用户偏好短回答。", covers_from_id=oldest, covers_to_id=newest - 2
    )
    assert saved.id > 0
    latest = await summaries.latest("p_1001")
    assert latest is not None and latest.summary == "用户偏好短回答。"

    replaced = await summaries.save(
        "p_1001", summary="更新后的摘要。", covers_from_id=oldest, covers_to_id=newest
    )
    rows = await database.read("SELECT COUNT(*) AS c FROM session_summaries")
    assert rows == [{"c": 1}], "one summary per session"
    fetched = await summaries.latest("p_1001")
    assert fetched is not None and fetched.covers_to_id == newest

    since = replaced.created_at - 10
    assert await summaries.count_since(since) == 1
    assert await summaries.count_since(replaced.created_at + 10) == 0

    assert await summaries.clear("p_1001") == 1
    assert await summaries.latest("p_1001") is None


async def test_summary_purge_orphans_removes_only_dead_sessions(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    summaries = SessionSummaryRepository(database)
    sessions = SessionRepository(database, legacy_path=tmp_path / "absent.json")
    await summaries.initialize()
    await sessions.initialize()

    await sessions.append_turn("p_alive", "在吗", "在的")
    await summaries.save("p_alive", summary="活跃会话摘要", covers_from_id=1, covers_to_id=2)
    await summaries.save("p_dead", summary="已消亡会话摘要", covers_from_id=1, covers_to_id=2)

    purged = await summaries.purge_orphans()
    assert purged == 1
    assert await summaries.latest("p_alive") is not None
    assert await summaries.latest("p_dead") is None


# ---------------------------------------------------------------------------
# history_summary service
# ---------------------------------------------------------------------------


def test_user_id_from_key_formats() -> None:
    assert user_id_from_key("p_123") == 123
    assert user_id_from_key("g_456_123") == 123
    assert user_id_from_key("weird") is None
    assert user_id_from_key("p_abc") is None


def test_compression_gate_requires_env_and_pilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAFFY_HISTORY_COMPRESSION_ENABLED", raising=False)
    monkeypatch.delenv("TAFFY_SUMMARY_PILOT_USERS", raising=False)
    assert compression_allows("p_1001") is False

    monkeypatch.setenv("TAFFY_HISTORY_COMPRESSION_ENABLED", "true")
    assert compression_allows("p_1001") is True  # empty pilot list = full rollout

    monkeypatch.setenv("TAFFY_SUMMARY_PILOT_USERS", "1001,2002")
    assert compression_allows("p_1001") is True
    assert compression_allows("p_9999") is False


@pytest.mark.asyncio
async def test_refresh_summarizes_uncovered_span_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_HISTORY_COMPRESSION_ENABLED", "true")
    monkeypatch.delenv("TAFFY_SUMMARY_PILOT_USERS", raising=False)
    # Use a unique key so no other test's session/state can leak into this one
    # (CI collects files in a different order than local).
    key = "p_990011"
    await runtime_sessions.initialize()
    await runtime_summaries.initialize()
    await clear_session(key)
    await runtime_sessions.delete(key)
    # The daily budget is a global counter across all summaries; CI runs tests
    # in a different file order than local, so earlier tests can exhaust it.
    # Patch the meter to zero for this test so budget can never interfere
    # with this test's own throttle assertions.
    monkeypatch.setattr(
        "src.state.runtime.session_summaries.count_since",
        AsyncMock(return_value=0),
    )
    # Pin the master gate open and clear any single-flight bookkeeping another
    # test may have left, so the first refresh cannot fail on environment or
    # cross-test state.  Replace the whole dicts so no background task can
    # have left a stale throttle entry behind (CI collects files in a
    # different order than local).
    from src.services import history_summary as _history_summary_module

    monkeypatch.setattr(_history_summary_module, "compression_allows", lambda _k: True)
    # Drain any background refresh tasks another test may have scheduled.
    await _history_summary_module.wait_for_tests()
    monkeypatch.setattr(_history_summary_module, "_last_attempt", {})
    async with _history_summary_module._inflight_lock:
        _history_summary_module._inflight.clear()

    for index in range(8):  # 16 messages; the newest 4 stay verbatim
        await runtime_sessions.append_turn(key, f"问题{index}", f"回答{index}")

    llm_mock = AsyncMock(return_value="用户在连续追问测试话题；偏好短回答。")
    with patch("src.services.llm.try_anthropic", llm_mock):
        assert await refresh(key) is True
        assert await refresh(key) is False, "hourly throttle must stop a repeat"

    summary = await load_summary(key)
    assert summary is not None and "测试话题" in summary
    llm_mock.assert_awaited_once()
    sent = llm_mock.await_args.args[1][0]["content"]
    assert "用户: 问题0" in sent and "回答5" in sent
    assert "问题12" not in sent and "回答15" not in sent, "newest four stay verbatim"

    # Cleanup wipes the summary and the throttle so the session can re-summarize.
    await clear_session(key)
    assert await load_summary(key) is None


# ---------------------------------------------------------------------------
# Relationship intimacy
# ---------------------------------------------------------------------------


def test_intimacy_formula_caps_and_decay() -> None:
    assert (
        compute_intimacy(
            interaction_count=0,
            accepted_insight_count=0,
            style_feedback_count=0,
            last_interaction_at=None,
        )
        == 0.0
    )
    full = compute_intimacy(
        interaction_count=999,
        accepted_insight_count=999,
        style_feedback_count=999,
        last_interaction_at=None,
    )
    assert full == 100.0  # caps: 40 + 35 + 25
    decayed = compute_intimacy(
        interaction_count=999,
        accepted_insight_count=999,
        style_feedback_count=999,
        last_interaction_at=1_800_000_000,
        now=1_800_000_000 + 10 * 86400,
    )
    assert decayed == round(100 * 0.98**10, 1)


def test_band_boundaries_are_neutral() -> None:
    assert band_for(0) == "陌生"
    assert band_for(15) == "点头之交"
    assert band_for(40) == "熟络"
    assert band_for(70) == "老友"


@pytest.mark.asyncio
async def test_relationship_repo_refresh_and_clear(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    relationships = PersonaRelationshipRepository(database)
    await relationships.initialize()

    await database.write(
        "INSERT INTO usage_log (user_id, group_id, feature, command, input_text) VALUES (7, 42, 'chat', '大盘', '大盘')"
    )
    await database.write(
        "INSERT INTO usage_log (user_id, group_id, feature, command, input_text) VALUES (7, 42, 'chat', '查询', '查询')"
    )
    await database.write(
        """
        INSERT INTO learning_insights (user_id, scope_group_id, kind, content, evidence, status)
        VALUES (7, 42, 'preference', '喜欢简短回答', '用户明确说', 'accepted')
        """
    )

    relationship = await relationships.refresh(7, 42)
    assert relationship.intimacy == 11.0  # 2 interactions (4 pts) + 1 insight (7 pts)
    assert relationship.band == "陌生"  # 11 < 15: neutral bands, no flattery
    assert relationship.interaction_count == 2
    assert relationship.accepted_insight_count == 1

    again = await relationships.refresh(7, 42)
    assert again.intimacy == relationship.intimacy, "idempotent recompute"
    assert await relationships.get(7, 0) is None, "scopes stay isolated"

    assert await relationships.clear(7, 42) == 1
    assert await relationships.get(7, 42) is None


@pytest.mark.asyncio
async def test_prompt_renders_familiarity_and_summary_blocks() -> None:
    plain = build_persona_prompt("在吗")
    assert "熟悉程度" not in plain and "更早对话摘要" not in plain

    decorated = build_persona_prompt(
        "在吗",
        familiarity="熟络",
        history_summary="用户偏好短回答；聊过开箱。",
    )
    assert "熟悉程度：熟络" in decorated
    assert "更早对话摘要" in decorated
    assert "不得虚构其中没有的细节" in decorated
    assert "身份与关系" in decorated and "禁止项" in decorated


# ---------------------------------------------------------------------------
# Cleanup integration with the existing memory commands
# ---------------------------------------------------------------------------


def _private(message: str) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=2,
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=3,
        user_id=1001,
        message=message,
        raw_message=message,
        font=0,
        sender={"user_id": 1001, "nickname": "u", "sex": "unknown", "age": 0},
    )


@pytest.mark.asyncio
async def test_reset_and_clear_conversation_remove_summaries_and_relationship(
    tmp_path: Path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    summaries = SessionSummaryRepository(database)
    relationships = PersonaRelationshipRepository(database)
    memories = PersonaMemoryRepository(database)
    sessions = SessionRepository(database, legacy_path=tmp_path / "absent.json")
    for repo in (summaries, relationships, memories, sessions):
        await repo.initialize()

    scope = PersonaScope(owner_user_id=1001, group_id=0)
    await memories.remember_note(scope, "喜欢简短回答")
    await summaries.save("p_1001", summary="旧摘要", covers_from_id=1, covers_to_id=2)
    await relationships.refresh(1001, 0)
    assert await relationships.get(1001, 0) is not None

    event = _private("忘记我")
    with (
        patch.object(chat, "persona_memories", memories),
        patch.object(chat, "store", sessions),
        patch.object(chat, "session_summaries", summaries),
        patch.object(chat, "persona_relationships", relationships),
    ):
        reply = await chat._execute_persona_memory_command(event, "忘记我")
        assert reply is not None and "都已清空" in reply

    assert await summaries.latest("p_1001") is None
    assert await relationships.get(1001, 0) is None

    await summaries.save("p_1001", summary="新摘要", covers_from_id=1, covers_to_id=2)
    with (
        patch.object(chat, "persona_memories", memories),
        patch.object(chat, "store", sessions),
        patch.object(chat, "session_summaries", summaries),
        patch.object(chat, "persona_relationships", relationships),
    ):
        reply = await chat._execute_persona_memory_command(event, "清空对话")
        assert reply is not None and "长期记忆还在" in reply
    assert await summaries.latest("p_1001") is None


@pytest.mark.asyncio
async def test_familiarity_command_reports_composition(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    relationships = PersonaRelationshipRepository(database)
    memories = PersonaMemoryRepository(database)
    sessions = SessionRepository(database, legacy_path=tmp_path / "absent.json")
    for repo in (relationships, memories, sessions):
        await repo.initialize()
    await database.write(
        "INSERT INTO usage_log (user_id, group_id, feature, command, input_text) VALUES (1001, 0, 'chat', '大盘', '大盘')"
    )

    event = _private("塔菲我们熟吗")
    with (
        patch.object(chat, "persona_memories", memories),
        patch.object(chat, "store", sessions),
        patch.object(chat, "persona_relationships", relationships),
    ):
        reply = await chat._execute_persona_memory_command(event, "塔菲我们熟吗")
    assert reply is not None
    assert "熟悉度" in reply and "构成" in reply and "互动记录 1 次" in reply
    assert "不含聊天内容" in reply

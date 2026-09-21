from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.core.database import StateDatabase
from src.plugins import chat
from src.services.authority import DENIED_MESSAGE
from src.state.persona_memory_repository import PersonaMemoryRepository
from src.state.session_repository import SessionRepository


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


def _group(message: str) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=2,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=4,
        user_id=1001,
        group_id=2001,
        message=message,
        raw_message=message,
        font=0,
        sender={
            "user_id": 1001,
            "nickname": "u",
            "card": "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "level": "",
            "role": "member",
            "title": "",
        },
    )


async def _stores(tmp_path: Path) -> tuple[PersonaMemoryRepository, SessionRepository]:
    database = StateDatabase(tmp_path / "state.db")
    memories = PersonaMemoryRepository(database)
    await memories.initialize()
    return memories, SessionRepository(database, legacy_path=tmp_path / "absent.json")


@pytest.mark.asyncio
async def test_plain_text_memory_commands_work_and_isolate_private_from_group(
    tmp_path: Path,
) -> None:
    memories, sessions = await _stores(tmp_path)
    private = _private("记住 我喜欢短回答")
    group = _group("以后叫我队长")

    with patch.object(chat, "persona_memories", memories), patch.object(chat, "store", sessions):
        assert "记住了" in (
            await chat._execute_persona_memory_command(private, "记住 我喜欢短回答") or ""
        )
        assert "叫你“队长”" in (
            await chat._execute_persona_memory_command(group, "以后叫我队长") or ""
        )

        private_list = await chat._execute_persona_memory_command(private, "塔菲记忆")
        group_list = await chat._execute_persona_memory_command(group, "塔菲记忆")
        assert private_list is not None and "我喜欢短回答" in private_list
        assert private_list is not None and "队长" not in private_list
        assert group_list is not None and "队长" in group_list
        assert group_list is not None and "我喜欢短回答" not in group_list


@pytest.mark.asyncio
async def test_clear_conversation_and_reset_have_distinct_plain_text_semantics(
    tmp_path: Path,
) -> None:
    memories, sessions = await _stores(tmp_path)
    event = _private("记住 要保留的偏好")

    with patch.object(chat, "persona_memories", memories), patch.object(chat, "store", sessions):
        await chat._execute_persona_memory_command(event, "记住 要保留的偏好")
        await sessions.append_turn("p_1001", "上一问", "上一答")
        reply = await chat._execute_persona_memory_command(event, "清空对话")
        assert reply is not None and "长期记忆还在" in reply
        assert len(await memories.list(chat.persona_scope(event))) == 1
        assert await sessions.as_messages("p_1001") == []

        await sessions.append_turn("p_1001", "新一问", "新一答")
        reply = await chat._execute_persona_memory_command(event, "清空记忆")
        assert reply is not None and "都已清空" in reply
        assert await memories.list(chat.persona_scope(event)) == ()
        assert await sessions.as_messages("p_1001") == []


class _StubMatcher:
    """Capture matcher.finish payloads without a running NoneBot session."""

    def __init__(self) -> None:
        self.finished: list[str] = []

    async def finish(self, message: object) -> None:
        self.finished.append(str(message))
        raise FinishedException


@pytest.mark.asyncio
async def test_slash_memory_commands_respect_chat_authority_gate(tmp_path: Path) -> None:
    """A2 regression: /记住 must be gated like the plain-text chat path."""

    memories, sessions = await _stores(tmp_path)
    event = _private("记住 我喜欢短回答")
    stub = _StubMatcher()

    from src.plugins import feature_gate

    with (
        patch.object(chat, "persona_memories", memories),
        patch.object(chat, "store", sessions),
        patch.object(feature_gate.authority, "allowed", AsyncMock(return_value=False)),
    ):
        with pytest.raises(FinishedException):
            await chat._run_memory_command(stub, event, "fallback")
        assert stub.finished and DENIED_MESSAGE in stub.finished[0]
        # The denied command must not write anything into memory storage.
        assert await memories.list(chat.persona_scope(event)) == ()


@pytest.mark.asyncio
async def test_allowed_user_stores_memory_through_gated_command_path(
    tmp_path: Path,
) -> None:
    memories, sessions = await _stores(tmp_path)
    event = _private("记住 我喜欢短回答")
    stub = _StubMatcher()

    from src.plugins import feature_gate

    with (
        patch.object(chat, "persona_memories", memories),
        patch.object(chat, "store", sessions),
        patch.object(feature_gate.authority, "allowed", AsyncMock(return_value=True)),
    ):
        with pytest.raises(FinishedException):
            await chat._run_memory_command(stub, event, "fallback")
    assert stub.finished and "记住了" in stub.finished[0]
    assert len(await memories.list(chat.persona_scope(event))) == 1

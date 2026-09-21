from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.core.database import StateDatabase
from src.plugins import chat
from src.state.persona_style_repository import PersonaStyleRepository
from src.state.session_repository import SessionRepository

from .test_persona_memory_commands import _group, _private


@pytest.mark.asyncio
async def test_style_commands_and_feedback_are_opt_in_and_scope_isolated(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    styles = PersonaStyleRepository(database)
    sessions = SessionRepository(database, legacy_path=tmp_path / "absent.json")
    await styles.initialize()
    await sessions.initialize()
    private = _private("塔菲学习 开")
    group = _group("塔菲风格")

    with (
        patch.object(chat, "persona_styles", styles),
        patch.object(chat, "store", sessions),
        patch.object(chat, "persona_v3_enabled_for_scope", return_value=True),
    ):
        opened = await chat._execute_persona_style_command(private, "塔菲学习 开")
        assert opened is not None and "已开启" in opened
        await sessions.append_turn("p_1001", "在吗", "很高兴为您服务，请问有什么问题？")
        chat._record_feedback_reference(private, "很高兴为您服务，请问有什么问题？", "闲聊")
        feedback = await chat._execute_natural_persona_feedback(private, "太客服了，短一点")
        assert feedback is not None and "微调" in feedback

        private_profile = await styles.get(chat.persona_scope(private))
        group_profile = await styles.get(chat.persona_scope(group))
        assert private_profile.style.concise == 1
        assert private_profile.style.direct == 1
        assert group_profile.style.concise == 0

        duplicate = await chat._execute_natural_persona_feedback(private, "少点喵")
        assert duplicate is not None and "已经调过一次" in duplicate
        undone = await chat._execute_persona_style_command(private, "撤销塔菲调整")
        assert undone is not None and "撤销" in undone
        reset = await chat._execute_persona_style_command(private, "重置塔菲风格")
        assert reset is not None and "归零" in reset


@pytest.mark.asyncio
async def test_feedback_requires_last_reply_within_ten_minutes(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    styles = PersonaStyleRepository(database)
    sessions = SessionRepository(database, legacy_path=tmp_path / "absent.json")
    await styles.initialize()
    await sessions.initialize()
    event = _private("短一点")
    await styles.set_learning(chat.persona_scope(event), True)
    await sessions.append_turn("p_1001", "在吗", "在呢。")
    await database.write(
        "UPDATE session_messages SET created_at=? WHERE session_key=? AND role='assistant'",
        (int(time.time()) - 601, "p_1001"),
    )
    chat._record_feedback_reference(
        event,
        "在呢。",
        "闲聊",
        now=int(time.time()) - 601,
    )

    with (
        patch.object(chat, "persona_styles", styles),
        patch.object(chat, "store", sessions),
        patch.object(chat, "persona_v3_enabled_for_scope", return_value=True),
    ):
        reply = await chat._execute_natural_persona_feedback(event, "短一点")
    assert reply is not None and "太久" in reply
    assert (await styles.get(chat.persona_scope(event))).style.concise == 0

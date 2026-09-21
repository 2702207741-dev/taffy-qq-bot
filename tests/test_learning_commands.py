from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import feature_gate
from src.plugins.chat import learning_commands
from src.state.persona_memory_repository import PersonaScope
from src.state.runtime import learning_insights


@pytest.fixture(autouse=True)
async def prepare():
    await learning_insights.initialize()
    yield
    await learning_insights.database.write("DELETE FROM learning_insights")


def _event(user_id: int, text: str, group_id: int = 0) -> SimpleNamespace:
    if group_id:
        return SimpleNamespace(
            user_id=user_id,
            group_id=group_id,
            get_plaintext=lambda text=text: text,
        )
    return SimpleNamespace(user_id=user_id, get_plaintext=lambda text=text: text)


async def _run(text: str, *, user_id: int = 301, group_id: int = 0) -> str:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(learning_commands._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(learning_commands, "learning_enabled", return_value=True),
        patch.object(
            learning_commands,
            "persona_scope",
            return_value=PersonaScope(user_id, group_id),
        ),
        patch.object(learning_commands.learning_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await learning_commands.handle_learning(object(), _event(user_id, text, group_id))  # type: ignore[arg-type]
    return str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_confirm_then_list_accepted() -> None:
    insight = await learning_insights.create_pending(301, 0, "preference", "关注 AK 系列", "依据")
    pending = await _run("塔菲观察", user_id=301)
    assert "关注 AK 系列" in pending
    assert "依据" in pending
    confirmed = await _run(f"确认印象 {_position(pending)}", user_id=301)
    assert "记住啦" in confirmed
    accepted = await _run("塔菲印象", user_id=301)
    assert "关注 AK 系列" in accepted
    assert insight is not None


@pytest.mark.asyncio
async def test_reject_and_delete() -> None:
    await learning_insights.create_pending(302, 0, "style", "喜欢即时调整回答风格", "依据")
    pending = await _run("塔菲观察", user_id=302)
    rejected = await _run(f"拒绝印象 {_position(pending)}", user_id=302)
    assert "忘掉" in rejected
    accepted = await _run("塔菲印象", user_id=302)
    assert "还没有已接受的印象" in accepted
    await learning_insights.create_pending(302, 0, "preference", "关注手套", "依据")
    accepted_text = await _run("塔菲观察", user_id=302)
    await _run(f"确认印象 {_position(accepted_text)}", user_id=302)
    await _run(f"删除印象 {_position(await _run('塔菲印象', user_id=302))}", user_id=302)
    assert "还没有已接受的印象" in await _run("塔菲印象", user_id=302)


@pytest.mark.asyncio
async def test_scope_isolation_between_private_and_group() -> None:
    await learning_insights.create_pending(303, 100, "preference", "群内偏好", "依据")
    pending_group = await _run("塔菲观察", user_id=303, group_id=100)
    assert "群内偏好" in pending_group
    pending_private = await _run("塔菲观察", user_id=303)
    assert "暂时没有待确认的印象" in pending_private


@pytest.mark.asyncio
async def test_closed_message_when_disabled() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(learning_commands._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(learning_commands, "learning_enabled", return_value=False),
        patch.object(
            learning_commands,
            "persona_scope",
            return_value=PersonaScope(304, 0),
        ),
        patch.object(learning_commands.learning_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await learning_commands.handle_learning(object(), _event(304, "塔菲印象"))  # type: ignore[arg-type]
    assert "已关闭" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_rate_limited() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(learning_commands._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(
            learning_commands,
            "persona_scope",
            return_value=PersonaScope(305, 0),
        ),
        patch.object(learning_commands.learning_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await learning_commands.handle_learning(object(), _event(305, "塔菲印象"))  # type: ignore[arg-type]
    assert "太频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_blocked_when_chat_feature_denied() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(
            feature_gate,
            "authority",
            SimpleNamespace(allowed=AsyncMock(return_value=False)),
        ),
        patch.object(learning_commands.learning_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await learning_commands.handle_learning(object(), _event(306, "塔菲印象"))  # type: ignore[arg-type]
    assert "未对你开放" in str(finish.await_args.args[0])


def _position(text: str) -> int:
    """Extract the first item number from a listing line."""

    for line in text.splitlines():
        parts = line.strip().split(". ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            return int(parts[0])
    return 1

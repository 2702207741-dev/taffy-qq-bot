from __future__ import annotations

import time
from datetime import timedelta
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
from src.plugins.chat import knowledge_commands
from src.services import learning
from src.services.tz import market_now
from src.state.persona_memory_repository import PersonaScope
from src.state.runtime import database, learning_insights


def _yesterday_noon() -> int:
    """A wall clock guaranteed to sit inside yesterday's local window."""

    local_midnight = market_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int((local_midnight - timedelta(hours=12)).timestamp())


@pytest.fixture(autouse=True)
async def prepare():
    await learning_insights.initialize()
    yield
    await database.write("DELETE FROM learning_insights")
    await database.write("DELETE FROM session_messages")
    await database.write("DELETE FROM usage_log")


def _catalog_patch(name: str = "AK-47 | Redline (Field-Tested)"):
    entry = type("E", (), {"market_name": name, "display_name": "红线"})()
    return (
        patch.object(learning, "market_catalog_store"),
        patch.object(
            learning.market_catalog_store,
            "entries",
            AsyncMock(return_value=(entry,)),
        ),
    )


async def _seed_usage(rows: list[tuple[int, int, str, int]]) -> None:
    for user_id, group_id, text, created_at in rows:
        await database.write(
            "INSERT INTO usage_log (user_id, group_id, feature, command, input_text, created_at) "
            "VALUES (?, ?, 'market', '查询', ?, ?)",
            (user_id, group_id, text, created_at),
        )


# --- M3 rule-based knowledge generation -------------------------------------


@pytest.mark.asyncio
async def test_observe_creates_knowledge_for_repeated_queries() -> None:
    now = int(time.time())
    await _seed_usage(
        [(4001, 0, "查询 AK-47 | Redline (Field-Tested)", now - 3600) for _ in range(3)]
    )
    with _catalog_patch()[0], _catalog_patch()[1]:
        created = await learning.observe_once(now=now)
    assert created >= 1
    insights, _ = await learning_insights.admin_list(user_id=4001, status="pending")
    knowledge = [item for item in insights if item.kind == "knowledge"]
    assert knowledge and "AK-47" in knowledge[0].content
    assert any("3 次查询" in item.evidence for item in knowledge)


@pytest.mark.asyncio
async def test_observe_knowledge_below_threshold_creates_nothing() -> None:
    now = int(time.time())
    await _seed_usage([(4002, 0, "查询 AK-47 | Redline (Field-Tested)", now - 3600)] * 2)
    with _catalog_patch()[0], _catalog_patch()[1]:
        assert await learning.observe_once(now=now) == 0


@pytest.mark.asyncio
async def test_observe_knowledge_respects_shared_daily_limit() -> None:
    now = int(time.time())
    for index in range(3):
        await learning_insights.create_pending(
            4003, 0, "preference", f"已有偏好 {index}", "历史", now=now
        )
    await _seed_usage([(4003, 0, "查询 AK-47 | Redline (Field-Tested)", now - 3600)] * 4)
    with _catalog_patch()[0], _catalog_patch()[1]:
        assert await learning.observe_once(now=now) == 0


@pytest.mark.asyncio
async def test_observe_knowledge_deduplicates_across_runs() -> None:
    now = int(time.time())
    await _seed_usage([(4004, 0, "查询 AK-47 | Redline (Field-Tested)", now - 3600)] * 3)
    with _catalog_patch()[0], _catalog_patch()[1]:
        first = await learning.observe_once(now=now)
        second = await learning.observe_once(now=now)
    assert first >= 1
    assert second == 0


# --- reflection knowledge dimension (three guard paths) ----------------------


@pytest.mark.asyncio
async def test_reflect_writes_pending_knowledge_from_model_json() -> None:
    now = int(time.time())
    await database.write(
        "INSERT INTO session_messages (session_key, role, content, created_at) "
        "VALUES ('p_4005', 'user', '今天查了三次红线价格', ?)",
        (_yesterday_noon(),),
    )
    payload = (
        '[{"kind": "knowledge", "content": "常查价格：AK-47 | Redline (Field-Tested)", '
        '"confidence": 3, "evidence": "昨日多次查询红线行情"}]'
    )
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value=payload)),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        created = await learning.reflect_once(now=now)
    assert created == 1
    insights, _ = await learning_insights.admin_list(user_id=4005, status="pending")
    assert insights[0].kind == "knowledge"
    assert insights[0].confidence == 3


@pytest.mark.asyncio
async def test_reflect_drops_non_json_output_without_writing() -> None:
    now = int(time.time())
    await database.write(
        "INSERT INTO session_messages (session_key, role, content, created_at) "
        "VALUES ('p_4006', 'user', '随便聊聊', ?)",
        (_yesterday_noon(),),
    )
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value="不是 JSON")),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        assert await learning.reflect_once(now=now) == 0
    _, total = await learning_insights.admin_list(user_id=4006)
    assert total == 0


@pytest.mark.asyncio
async def test_reflect_drops_knowledge_quoting_raw_text() -> None:
    now = int(time.time())
    raw = "这是一条完整的原始聊天消息内容，用于检验知识防原文泄漏"
    await database.write(
        "INSERT INTO session_messages (session_key, role, content, created_at) "
        "VALUES ('p_4007', 'user', ?, ?)",
        (raw, now - 20000),
    )
    payload = '[{"kind": "knowledge", "content": "用户说：' + raw + '", "evidence": "原文"}]'
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value=payload)),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        assert await learning.reflect_once(now=now) == 0
    _, total = await learning_insights.admin_list(user_id=4007)
    assert total == 0


# --- reinforcement feedback loop ---------------------------------------------


@pytest.mark.asyncio
async def test_reinforce_bumps_confidence_on_repeat_mention() -> None:
    now = int(time.time())
    insight = await learning_insights.create_pending(
        4008,
        0,
        "knowledge",
        "常查价格：AK-47 | Redline (Field-Tested)",
        "历史查询",
        confidence=2,
        now=now - 86400,
    )
    assert insight is not None
    await learning_insights.confirm(insight.id, 4008, 0, now=now - 86400)
    await _seed_usage([(4008, 0, "查询 AK-47 redline", now - 3600)])
    with patch.object(learning, "learning_enabled", return_value=True):
        assert await learning.reinforce_once(now=now) == 1
    rows, _ = await learning_insights.admin_list(user_id=4008, status="accepted")
    assert rows[0].confidence == 3


@pytest.mark.asyncio
async def test_reinforce_caps_confidence_at_five() -> None:
    now = int(time.time())
    insight = await learning_insights.create_pending(
        4009,
        0,
        "knowledge",
        "常查价格：AK-47 | Redline (Field-Tested)",
        "历史查询",
        confidence=5,
        now=now - 86400,
    )
    assert insight is not None
    await learning_insights.confirm(insight.id, 4009, 0, now=now - 86400)
    await _seed_usage([(4009, 0, "查询 AK-47", now - 3600)])
    with patch.object(learning, "learning_enabled", return_value=True):
        assert await learning.reinforce_once(now=now) == 0
    rows, _ = await learning_insights.admin_list(user_id=4009, status="accepted")
    assert rows[0].confidence == 5


@pytest.mark.asyncio
async def test_reinforce_ignores_other_kinds_and_no_mentions() -> None:
    now = int(time.time())
    preference = await learning_insights.create_pending(
        4010, 0, "preference", "关注 AK-47", "历史", now=now - 86400
    )
    assert preference is not None
    await learning_insights.confirm(preference.id, 4010, 0, now=now - 86400)
    knowledge = await learning_insights.create_pending(
        4010,
        0,
        "knowledge",
        "常查价格：AK-47 | Redline (Field-Tested)",
        "历史",
        now=now - 86400,
    )
    assert knowledge is not None
    await learning_insights.confirm(knowledge.id, 4010, 0, now=now - 86400)
    with patch.object(learning, "learning_enabled", return_value=True):
        assert await learning.reinforce_once(now=now) == 0


# --- 塔菲知识 command ---------------------------------------------------------


def _event(user_id: int, text: str, group_id: int = 0) -> SimpleNamespace:
    if group_id:
        return SimpleNamespace(
            user_id=user_id,
            group_id=group_id,
            get_plaintext=lambda text=text: text,
        )
    return SimpleNamespace(user_id=user_id, get_plaintext=lambda text=text: text)


async def _run(text: str, *, user_id: int = 4020, group_id: int = 0) -> str:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(knowledge_commands._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(knowledge_commands, "learning_enabled", return_value=True),
        patch.object(
            knowledge_commands,
            "persona_scope",
            return_value=PersonaScope(user_id, group_id),
        ),
        patch.object(
            learning,
            "try_anthropic",
            AsyncMock(side_effect=AssertionError("knowledge lookup must not call the LLM")),
        ),
        patch.object(knowledge_commands.knowledge_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await knowledge_commands.handle_knowledge(object(), _event(user_id, text, group_id))  # type: ignore[arg-type]
    return str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_knowledge_command_hits_accepted_insight_without_llm() -> None:
    insight = await learning_insights.create_pending(
        4020,
        0,
        "knowledge",
        "常查价格：AK-47 | Redline (Field-Tested)",
        "近 24 小时私聊 3 次查询 'AK-47 | Redline (Field-Tested)'",
    )
    assert insight is not None
    await learning_insights.confirm(insight.id, 4020, 0)
    reply = await _run("塔菲知识 红线 Redline 多少钱")
    assert "常查价格" in reply
    assert "AK-47" in reply
    assert "依据" in reply


@pytest.mark.asyncio
async def test_knowledge_command_alias_and_miss() -> None:
    reply = await _run("塔菲知道 完全无关的问题")
    assert "还没记住" in reply


@pytest.mark.asyncio
async def test_knowledge_command_only_reads_accepted() -> None:
    await learning_insights.create_pending(
        4022, 0, "knowledge", "常查价格：AWP | Dragon Lore", "依据"
    )
    reply = await _run("塔菲知识 Dragon Lore 价格")
    assert "还没记住" in reply


@pytest.mark.asyncio
async def test_knowledge_command_requires_argument() -> None:
    reply = await _run("塔菲知识")
    assert "格式" in reply


@pytest.mark.asyncio
async def test_knowledge_command_rate_limited() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(knowledge_commands._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(
            knowledge_commands,
            "persona_scope",
            return_value=PersonaScope(4023, 0),
        ),
        patch.object(knowledge_commands.knowledge_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await knowledge_commands.handle_knowledge(object(), _event(4023, "塔菲知识 红线"))  # type: ignore[arg-type]
    assert "太频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_knowledge_command_blocked_when_chat_feature_denied() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(
            feature_gate,
            "authority",
            SimpleNamespace(allowed=AsyncMock(return_value=False)),
        ),
        patch.object(knowledge_commands.knowledge_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await knowledge_commands.handle_knowledge(object(), _event(4024, "塔菲知识 红线"))  # type: ignore[arg-type]
    assert "未对你开放" in str(finish.await_args.args[0])

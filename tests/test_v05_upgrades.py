"""V0.5 first-tier upgrades: tool loop, vision, stickers, memory decisions.

Covers the fail-closed surface of every new piece: factory-off switches,
pilot gating, bounded tool behaviour, vision budget/cache semantics, the
sleep-time consolidation pass and the mem0-style memory write decision.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.services import learning, stickers, tools, vision
from src.state.runtime import database, learning_insights


@pytest.fixture(autouse=True)
async def _v05_prepare():
    await learning_insights.initialize()
    stickers.reset_runtime_state()
    yield
    stickers.reset_runtime_state()
    await database.write("DELETE FROM learning_insights")
    await database.write("DELETE FROM memory_links")
    await database.write("DELETE FROM session_messages")
    await database.write("DELETE FROM app_meta")
    await database.write("DELETE FROM image_descriptions")


# ---------------------------------------------------------------------------
# Tool loop
# ---------------------------------------------------------------------------


def test_tools_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("TAFFY_TOOLS_PILOT_USERS", raising=False)
    assert tools.tools_enabled() is False
    assert tools.tools_allowed_for_user(10086) is False


def test_tools_pilot_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_TOOLS_ENABLED", "true")
    monkeypatch.setenv("TAFFY_TOOLS_PILOT_USERS", "10086")
    monkeypatch.delenv("TAFFY_TOOLS_FULL_ROLLOUT", raising=False)
    assert tools.tools_allowed_for_user(10086) is True
    assert tools.tools_allowed_for_user(20001) is False
    monkeypatch.setenv("TAFFY_TOOLS_FULL_ROLLOUT", "true")
    assert tools.tools_allowed_for_user(20001) is True
    assert tools.tools_allowed_for_user("not-a-number") is False


def test_tool_schemas_are_llm_ready() -> None:
    anthropic = tools.anthropic_tool_schemas()
    openai = tools.openai_tool_schemas()
    names = {spec["name"] for spec in anthropic}
    assert {"query_market_item", "search_chat_history", "search_memories", "send_sticker"} <= (
        names
    )
    assert all("input_schema" in spec for spec in anthropic)
    assert all(spec["type"] == "function" for spec in openai)


@pytest.mark.asyncio
async def test_execute_tool_unknown_and_crash_fail_closed() -> None:
    text, action = await tools.execute_tool(tools.ToolContext(user_id=1), "no_such_tool", {})
    assert "不存在" in text and action is None

    async def boom(ctx: tools.ToolContext, arguments: dict) -> tuple[str, None]:
        raise RuntimeError("boom")

    spec = tools.ToolSpec(
        name="boom",
        description="",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    with patch.dict(tools._TOOL_INDEX, {"boom": spec}):
        text, action = await tools.execute_tool(tools.ToolContext(user_id=1), "boom", {})
    assert "出了点问题" in text and action is None


@pytest.mark.asyncio
async def test_market_tool_fail_closed_without_directory_match() -> None:
    nonebot_init_quietly()
    with patch("src.plugins.chat.market.resolve_name", AsyncMock(return_value=None)):
        text, action = await tools.execute_tool(
            tools.ToolContext(user_id=1), "query_market_item", {"item": "不存在的饰品"}
        )
    assert "没有在目录里找到" in text and action is None


@pytest.mark.asyncio
async def test_market_tool_returns_verified_render_only() -> None:
    nonebot_init_quietly()
    with (
        patch("src.plugins.chat.market.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch(
            "src.plugins.chat.st_get",
            AsyncMock(side_effect=RuntimeError("upstream down")),
        ),
    ):
        text, action = await tools.execute_tool(
            tools.ToolContext(user_id=1), "query_market_item", {"item": "AK红线"}
        )
    assert "暂时不可用" in text and action is None, "a dead upstream never fabricates prices"


def nonebot_init_quietly() -> None:
    """The market tool lazily imports the chat plugin, which needs NoneBot."""

    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()


@pytest.mark.asyncio
async def test_history_tool_scopes_and_redacts_ids() -> None:
    now = int(time.time())
    for key, content in (
        ("g_777_1001", "群消息一：那个箱子叫什么来着"),
        ("g_777_1002", "群消息二：那个箱子是绯红战车"),
        ("g_888_1001", "别群的秘密暗号布莱克"),
        ("p_1001", "私聊里的秘密暗号布莱克"),
    ):
        await database.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (key, content, now),
        )
    try:
        group_text, _ = await tools.execute_tool(
            tools.ToolContext(user_id=1001, group_id=777),
            "search_chat_history",
            {"keyword": "那个箱子"},
        )
        assert "别群" not in group_text and "p_1001" not in group_text
        assert "绯红战车" in group_text, "only the same group is searched"

        private_text, _ = await tools.execute_tool(
            tools.ToolContext(user_id=1001, group_id=0),
            "search_chat_history",
            {"keyword": "秘密暗号"},
        )
        assert "私聊里的" in private_text
        assert "别群" not in private_text, "other people's private chat is hidden"
    finally:
        await database.write("DELETE FROM session_messages")


@pytest.mark.asyncio
async def test_memory_tool_hits_and_reinforces() -> None:
    await learning_insights.initialize()
    insight = await learning_insights.create_pending(
        3001, 0, "knowledge", "常查价格：AK-47 | Redline", "每日反思", now=int(time.time())
    )
    await learning_insights.confirm(insight.id, 3001, 0)
    try:
        text, action = await tools.execute_tool(
            tools.ToolContext(user_id=3001, group_id=0),
            "search_memories",
            {"keyword": "AK-47 Redline 常查价格"},
        )
        assert "常查价格" in text and action is None
        rows, _ = await learning_insights.admin_list(user_id=3001)
        assert rows[0].hit_count == 1, "a real retrieval hit reinforces the memory"
    finally:
        await database.write("DELETE FROM learning_insights")


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------


def test_vision_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_VISION_ENABLED", raising=False)
    monkeypatch.delenv("TAFFY_VISION_API_KEY", raising=False)
    assert vision.vision_enabled() is False


def test_vision_description_format_gate() -> None:
    assert vision._parse_description("一只开心的猫娘|开心,猫娘") is not None
    assert vision._parse_description("没有竖线的输出") is None
    assert vision._parse_description("短|") is None
    assert vision._parse_description(None) is None


@pytest.mark.asyncio
async def test_vision_describe_requires_enabled_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAFFY_VISION_ENABLED", raising=False)
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    assert await vision.describe_image(image) is None

    await database.write("DELETE FROM app_meta")
    await learning_insights.initialize()
    monkeypatch.setenv("TAFFY_VISION_ENABLED", "true")
    monkeypatch.setenv("TAFFY_VISION_API_KEY", "test-key-not-real-000000000000001")
    monkeypatch.setenv("TAFFY_VISION_DAILY_BUDGET", "1")

    payload = {
        "choices": [{"message": {"content": "一只戴耳机猫娘贴图，开心|开心,猫娘,耳机"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    mock_request = AsyncMock(return_value=payload)
    with patch.object(vision, "request_json", mock_request):
        first = await vision.describe_image(image)
        second = await vision.describe_image(tmp_path / "pic.png")
    assert first is not None and "猫娘" in first[0]
    assert second is not None, "cache hit costs no budget"
    assert mock_request.await_count == 1


# ---------------------------------------------------------------------------
# Stickers: four-axis selection and the pending review pool
# ---------------------------------------------------------------------------


def _write_library(directory: Path, entries: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps({"stickers": entries}, ensure_ascii=False), encoding="utf-8"
    )


def test_find_sticker_by_text_vibe_and_occasion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    library = tmp_path / "stickers"
    _write_library(
        library,
        [
            {"file": "polite.png", "tags": ["礼貌"], "vibe": ["礼貌"]},
            {"file": "tease.png", "tags": ["搞笑"], "vibe": ["搞笑"]},
            {"file": "sleepy.png", "tags": ["睡觉", "晚安"], "vibe": ["温柔"]},
        ],
    )
    for name in ("polite.png", "tease.png", "sleepy.png"):
        (library / name).write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)

    # A stranger never receives the over-familiar sticker...
    for _ in range(20):
        chosen = stickers.find_sticker_by_text("哈哈哈太搞笑了", intimacy_band="陌生")
        assert chosen is None or chosen.name != "tease.png"
    # ...but an old friend does.
    chosen = stickers.find_sticker_by_text("哈哈哈太搞笑了", intimacy_band="老友")
    assert chosen is not None and chosen.name == "tease.png"
    # Late-night occasion prefers calm tags.
    chosen = stickers.find_sticker_by_text("我要睡觉啦 晚安", intimacy_band="老友", hour=1)
    assert chosen is not None and chosen.name == "sleepy.png"


def test_pending_sticker_pool_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", tmp_path)
    source = tmp_path / "upload.png"
    source.write_bytes(b"png")

    pending = stickers.record_pending_sticker(source)
    assert pending is not None and pending.parent.name == "pending"
    assert len(stickers.pending_stickers()) == 1

    accepted = stickers.accept_pending_sticker(1, ["开心"])
    assert accepted is not None and accepted.parent == tmp_path
    assert stickers.pending_stickers() == []
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stickers"][0]["file"] == accepted.name

    (tmp_path / "pending").mkdir(exist_ok=True)
    (tmp_path / "pending" / "b.png").write_bytes(b"png")
    assert stickers.reject_pending_sticker(1) is not None
    assert stickers.pending_stickers() == []


# ---------------------------------------------------------------------------
# Memory write decision + consolidation (B4/B5)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _memory_prepare():
    await learning_insights.initialize()
    yield
    await database.write("DELETE FROM learning_insights")
    await database.write("DELETE FROM memory_links")
    await database.write("DELETE FROM app_meta")


@pytest.mark.asyncio
async def test_memory_write_decision_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAFFY_MEMORY_WRITE_DECISION_ENABLED", raising=False)
    decision, target, merged = await learning.memory_write_decision(
        4001, 0, "我住在杭州", now=int(time.time())
    )
    assert (decision, target, merged) == ("add", None, None)


@pytest.mark.asyncio
async def test_memory_write_decision_update_and_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_WRITE_DECISION_ENABLED", "true")
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "true")
    from src.state.persona_memory_repository import PersonaScope
    from src.state.runtime import persona_memories

    scope = PersonaScope(4002, 0)
    await persona_memories.remember_note(scope, "我住在杭州")

    async def fake_decision_model(prompt: str, messages: list, max_tokens: int) -> str:
        assert "已存在的相似记忆" in messages[0]["content"]
        candidates = messages[0]["content"]
        existing_id = int(candidates.split("已存在的相似记忆：\n")[1].split(". ")[0])
        return json.dumps(
            {"decision": "UPDATE", "target_id": existing_id, "content": "我住在杭州西湖区"}
        )

    with patch.object(learning, "try_anthropic", AsyncMock(side_effect=fake_decision_model)):
        decision, target, merged = await learning.memory_write_decision(
            4002, 0, "我住在杭州西湖区", now=int(time.time())
        )
    assert decision == "update" and target is not None and merged == "我住在杭州西湖区"
    # The chat layer applies the decision; verify update_note lands it.
    applied = await persona_memories.update_note(scope, target, merged)
    assert applied is not None and applied.content == "我住在杭州西湖区"
    memories = await persona_memories.list(scope)
    assert any(memory.content == "我住在杭州西湖区" for memory in memories)
    assert not any(memory.content == "我住在杭州" for memory in memories)

    async def fake_noop(prompt: str, messages: list, max_tokens: int) -> str:
        return json.dumps({"decision": "NOOP", "target_id": None, "content": ""})

    with patch.object(learning, "try_anthropic", AsyncMock(side_effect=fake_noop)):
        decision, target, merged = await learning.memory_write_decision(
            4002, 0, "我定居在杭州西湖区呀", now=int(time.time())
        )
    assert decision == "noop"


@pytest.mark.asyncio
async def test_memory_write_decision_falls_back_to_add_on_model_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_WRITE_DECISION_ENABLED", "true")
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "true")
    from src.state.persona_memory_repository import PersonaScope
    from src.state.runtime import persona_memories

    scope = PersonaScope(4003, 0)
    await persona_memories.remember_note(scope, "我喜欢简洁回复")
    with patch.object(learning, "try_anthropic", AsyncMock(side_effect=RuntimeError("down"))):
        decision, target, merged = await learning.memory_write_decision(
            4003, 0, "我喜欢简洁回复，真的", now=int(time.time())
        )
    assert (decision, target, merged) == ("add", None, None), (
        "an explicit user request is never lost to a model outage"
    )


@pytest.mark.asyncio
async def test_consolidation_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_MEMORY_CONSOLIDATION_ENABLED", raising=False)
    stats = await learning.consolidate_once(now=int(time.time()))
    assert stats == {"merged": 0, "protected": 0, "links": 0}


@pytest.mark.asyncio
async def test_consolidation_merges_protects_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_CONSOLIDATION_ENABLED", "true")
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "true")
    now = int(time.time())
    first = await learning_insights.create_pending(
        5001, 0, "knowledge", "常查价格：AK-47 | Redline（看跌可以关注）", "反思A", now=now
    )
    await learning_insights.confirm(first.id, 5001, 0, now=now)
    duplicate = await learning_insights.create_pending(
        5001, 0, "knowledge", "常查价格：AK-47 | Redline，看跌可关注", "反思B", now=now + 60
    )
    await learning_insights.confirm(duplicate.id, 5001, 0, now=now + 60)
    stable = await learning_insights.create_pending(
        5001, 0, "knowledge", "用户的常用基调是短线行情观察", "反思C", now=now
    )
    await learning_insights.confirm(stable.id, 5001, 0, now=now)
    for _ in range(4):
        await learning_insights.bump_confidence(stable.id, now=now)
    await learning_insights.merge_update(stable.id, confidence=5, now=now)

    async def fake_link_model(prompt: str, messages: list, max_tokens: int) -> str:
        listing = messages[0]["content"]
        first_id = int(listing.split("\n")[1].split(".")[0])
        second_id = int(listing.split("\n")[2].split(".")[0])
        return json.dumps(
            [
                {"from": first_id, "to": second_id, "relation": "同一饰品观察"},
                {"from": first_id, "to": 999999, "relation": "编造的链接"},
            ]
        )

    with patch.object(learning, "try_anthropic", AsyncMock(side_effect=fake_link_model)):
        stats = await learning.consolidate_once(now=now + 120)
    assert stats["merged"] == 1, "near-duplicate impressions collapse into the older row"
    assert stats["protected"] == 1, "max-confidence knowledge stops decaying"
    assert stats["links"] == 1, "fabricated ids are rejected by validation"
    rows, _ = await learning_insights.admin_list(user_id=5001, status="deprecated")
    assert len(rows) == 1 and rows[0].valid_until is not None
    links = await learning_insights.links_for_scope(5001, 0)
    assert len(links) == 1 and links[0][2] == "同一饰品观察"


@pytest.mark.asyncio
async def test_record_hit_reinforces_and_protects() -> None:
    now = int(time.time())
    insight = await learning_insights.create_pending(
        5002, 0, "knowledge", "常查价格：M4A1 消音", "反思", now=now
    )
    await learning_insights.confirm(insight.id, 5002, 0, now=now)
    for _ in range(3):
        assert await learning.record_insight_hit(insight.id, now=now) is True
    rows, _ = await learning_insights.admin_list(user_id=5002)
    assert rows[0].hit_count == 3
    assert rows[0].is_protected is True, "well-attested memories resist the forgetting curve"
    weight_after_hits = rows[0].weight
    archived = await learning_insights.decay(now=now + 86_400)
    assert archived == 0
    rows, _ = await learning_insights.admin_list(user_id=5002)
    assert rows[0].weight == weight_after_hits, "protected memories never decay"
    assert await learning.record_insight_hit(999999, now=now) is False

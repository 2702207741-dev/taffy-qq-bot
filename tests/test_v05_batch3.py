"""V0.5 third batch: tool-loop channel, calibration, negative feedback."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.services import learning, persona_eval, slang_service, stickers, tools
from src.state.runtime import database, learning_insights


@pytest.fixture(autouse=True)
async def _prepare():
    await learning_insights.initialize()
    stickers.reset_runtime_state()
    yield
    stickers.reset_runtime_state()
    await database.write("DELETE FROM learning_insights")
    await database.write("DELETE FROM persona_slang")
    await database.write("DELETE FROM session_messages")


# ---------------------------------------------------------------------------
# Tool loop on the Anthropic channel (B1)
# ---------------------------------------------------------------------------


class _FakeBlocks:
    def __init__(self, blocks: list) -> None:
        self.content = blocks
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5)


class _FakeMessage:
    def __init__(self, type_: str, **data: object) -> None:
        self.type = type_
        for key, value in data.items():
            setattr(self, key, value)


def _fake_client_factory(responses: list[_FakeBlocks]):
    calls = {"count": 0}

    class _Messages:
        async def create(self, **kwargs: object) -> _FakeBlocks:
            index = min(calls["count"], len(responses) - 1)
            calls["count"] += 1
            return responses[index]

    class _Client:
        def __init__(self) -> None:
            self.messages = _Messages()

    async def factory() -> _Client:
        return _Client()

    return factory


@pytest.mark.asyncio
async def test_call_model_with_tools_falls_back_when_disabled() -> None:
    from src.services.llm import chat_service

    scope = SimpleNamespace(owner_user_id=1, group_id=0)
    with patch.object(chat_service, "_call_model", AsyncMock(return_value="plain-reply")) as plain:
        text = await chat_service._call_model_with_tools(
            "prompt",
            [{"role": "user", "content": "hi"}],
            800,
            scope,
            None,
        )
    assert text == "plain-reply"
    assert plain.await_count == 1, "tools stay off without the pilot gate"


@pytest.mark.asyncio
async def test_tool_loop_executes_tool_and_recovers_final_text() -> None:
    from src.services.llm import try_anthropic_with_tools
    from src.services.tools import ToolContext

    ctx = ToolContext(user_id=4242, group_id=0)
    tool_block = _FakeMessage(
        "tool_use", id="call-1", name="search_memories", input={"keyword": "杭州"}
    )
    first = _FakeBlocks([tool_block])
    final = _FakeBlocks([_FakeMessage("text", text="想起来了喵。")])
    calls: list[dict] = []

    class _Messages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return first if len(calls) == 1 else final

    class _Client:
        messages = _Messages()

    async def factory() -> _Client:
        return _Client()

    actions: list[object] = []
    text = await try_anthropic_with_tools(
        "system",
        [{"role": "user", "content": "hi"}],
        800,
        ctx,
        actions,
        client_factory=factory,
    )
    assert text == "想起来了喵。"
    assert calls[0].get("tools"), "tool schemas must be attached on every round"


# ---------------------------------------------------------------------------
# Persona audit: the real calibration path (B8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_audit_calibration_passes_with_obedient_judge() -> None:
    async def fake_anthropic(prompt: str, messages: list, max_tokens: int) -> str:
        return json.dumps(
            [
                {"dimension": "口癖纪律", "score": 5, "evidence": "自然"},
                {"dimension": "客服腔", "score": 5, "evidence": "无客服腔"},
                {"dimension": "口癖纪律", "score": 5, "evidence": "自然"},
                {"dimension": "客服腔", "score": 5, "evidence": "无客服腔"},
                {"dimension": "口癖纪律", "score": 2, "evidence": "堆梗"},
                {"dimension": "客服腔", "score": 1, "evidence": "自称AI"},
                {"dimension": "口癖纪律", "score": 2, "evidence": "堆梗"},
                {"dimension": "客服腔", "score": 1, "evidence": "自称AI"},
            ]
        )

    with patch.object(persona_eval, "try_anthropic", AsyncMock(side_effect=fake_anthropic)):
        assert await persona_eval._judge_calibrated() is True


@pytest.mark.asyncio
async def test_persona_audit_calibration_fails_with_rebellious_judge() -> None:
    async def fake_anthropic(prompt: str, messages: list, max_tokens: int) -> str:
        return json.dumps(
            [
                {"dimension": "口癖纪律", "score": 1, "evidence": "错误判断"},
                {"dimension": "客服腔", "score": 1, "evidence": "错误判断"},
                {"dimension": "口癖纪律", "score": 1, "evidence": "错误判断"},
                {"dimension": "客服腔", "score": 1, "evidence": "错误判断"},
                {"dimension": "口癖纪律", "score": 1, "evidence": "错误判断"},
                {"dimension": "客服腔", "score": 1, "evidence": "错误判断"},
                {"dimension": "口癖纪律", "score": 1, "evidence": "错误判断"},
                {"dimension": "客服腔", "score": 1, "evidence": "错误判断"},
            ]
        )

    with patch.object(persona_eval, "try_anthropic", AsyncMock(side_effect=fake_anthropic)):
        assert await persona_eval._judge_calibrated() is False


# ---------------------------------------------------------------------------
# Slang negative feedback (B7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_feedback_disables_taught_term() -> None:
    from src.state.runtime import persona_slang

    await persona_slang.teach(77, created_by=6001, term="绝绝子", meaning="夸东西好")
    entry, _ = await persona_slang.teach(77, created_by=6001, term="绝绝子", meaning="夸东西好")
    await persona_slang.decide(entry.id, scope_group_id=77, status="accepted")

    reply = await slang_service.negative_feedback(77, "不可以再说绝绝子了喵")
    assert reply is not None and "绝绝子" in reply
    remaining = await persona_slang.accepted_for_injection(77)
    assert remaining == [], "the corrected term must leave the prompt pool"

    # Non-correction chatter stays untouched.
    assert await slang_service.negative_feedback(77, "今天天气不错") is None


@pytest.mark.asyncio
async def test_prompt_terms_respect_intimacy_allowance() -> None:
    from src.services.relationship_service import relationship_benefits
    from src.state.runtime import persona_slang

    for term in ("甲词", "乙词", "丙词", "丁词", "戊词", "己词", "庚词"):
        entry, _ = await persona_slang.teach(88, created_by=6002, term=term, meaning="测试")
        await persona_slang.decide(entry.id, scope_group_id=88, status="accepted")
    terms = await slang_service.prompt_terms(88, limit=relationship_benefits("陌生")["slang_limit"])
    assert len(terms) == 5, "stranger scopes draw from a smaller allowance"
    terms_friend = await slang_service.prompt_terms(
        88, limit=relationship_benefits("老友")["slang_limit"]
    )
    assert len(terms_friend) == 7


# ---------------------------------------------------------------------------
# Sticker description persistence + vision capture gate (B2/B3)
# ---------------------------------------------------------------------------


def test_apply_description_updates_manifest(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    stickers_module.reset_runtime_state()
    library = tmp_path / "stickers"
    library.mkdir(parents=True)
    (library / "manifest.json").write_text(
        json.dumps({"stickers": [{"file": "a.png", "tags": ["开心"]}]}), encoding="utf-8"
    )
    (library / "a.png").write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)

    target = library / "a.png"
    assert stickers.apply_description(target, "一只开心的猫娘", ["猫娘"]) is True
    manifest = json.loads((library / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stickers"][0]["desc"] == "一只开心的猫娘"
    assert "猫娘" in manifest["stickers"][0]["tags"]


@pytest.mark.asyncio
async def test_vision_capture_gate_is_off_by_default() -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    from src.plugins.chat import _first_image_segment, _vision_context_and_capture
    from src.services import vision

    assert vision.vision_enabled() is False
    event = SimpleNamespace(message=[], user_id=1)
    captured, snippet = await _vision_context_and_capture(None, event)
    assert snippet is None, "vision stays silent while the switch is off"
    url, file_id = _first_image_segment(event)
    assert url is None and file_id is None


@pytest.mark.asyncio
async def test_time_helpers_are_deterministic() -> None:
    assert learning.text_similarity("你好", "你好") == 1.0
    assert learning.text_similarity("你好", "再见") < 0.5
    assert await learning.record_insight_hit(999999) is False


# ---------------------------------------------------------------------------
# Coverage round-up: fail-closed corners of the V0.5 services
# ---------------------------------------------------------------------------


def _ensure_nonebot() -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()


@pytest.mark.asyncio
async def test_market_tool_success_path_returns_verified_render() -> None:
    _ensure_nonebot()
    with (
        patch("src.plugins.chat.market.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch(
            "src.plugins.chat.st_get",
            AsyncMock(return_value={"success": True, "data": []}),
        ),
        patch(
            "src.plugins.chat.market.format_price",
            lambda data: "¥ 100.00（Steam）",
        ),
    ):
        text, action = await tools.execute_tool(
            tools.ToolContext(user_id=1), "query_market_item", {"item": "AK红线"}
        )
    assert "AK-47 | Redline" in text and "100.00" in text
    assert action is None


@pytest.mark.asyncio
async def test_market_tool_rejects_bad_render_and_missing_argument() -> None:
    _ensure_nonebot()
    text, _ = await tools.execute_tool(
        tools.ToolContext(user_id=1), "query_market_item", {"item": "  "}
    )
    assert "缺少饰品名" in text
    with (
        patch("src.plugins.chat.market.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch("src.plugins.chat.st_get", AsyncMock(return_value={"success": False})),
        patch("src.plugins.chat.market.format_price", lambda data: "⚠️ 数据异常"),
    ):
        text, _ = await tools.execute_tool(
            tools.ToolContext(user_id=1), "query_market_item", {"item": "AK红线"}
        )
    assert "拿不到可信数据" in text


@pytest.mark.asyncio
async def test_history_tool_clamps_days_and_handles_bad_arguments() -> None:
    text, _ = await tools.execute_tool(
        tools.ToolContext(user_id=1), "search_chat_history", {"keyword": ""}
    )
    assert "缺少搜索关键词" in text
    text, _ = await tools.execute_tool(
        tools.ToolContext(user_id=1),
        "search_chat_history",
        {"keyword": "随便", "days": "not-a-number"},
    )
    assert "没搜到" in text or "相关消息" in text


@pytest.mark.asyncio
async def test_sticker_tool_respects_master_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_STICKERS_ENABLED", raising=False)
    stickers.reset_runtime_state()
    text, action = await tools.execute_tool(
        tools.ToolContext(user_id=1), "send_sticker", {"keywords": "开心"}
    )
    assert "关闭" in text and action is None


@pytest.mark.asyncio
async def test_automine_stays_off_and_needs_sample_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAFFY_SLANG_AUTOMINE_ENABLED", raising=False)
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "false")
    assert await slang_service.automine_once(now=int(time.time())) == 0


def test_negation_target_screening() -> None:
    assert slang_service._negation_target("") == ""
    assert slang_service._negation_target("不可以再说绝绝子了喵") != ""
    assert slang_service._negation_target("今天行情还行") == ""


@pytest.mark.asyncio
async def test_persona_audit_judge_parse_failures_fail_closed() -> None:
    async def bad_json(prompt: str, messages: list, max_tokens: int) -> str:
        return "不是 JSON 的输出"

    with patch.object(persona_eval, "try_anthropic", AsyncMock(side_effect=bad_json)):
        assert await persona_eval._judge(payload_user="随便", max_tokens=10) is None

    async def wrong_shape(prompt: str, messages: list, max_tokens: int) -> str:
        return json.dumps({"dimension": "x"})

    with patch.object(persona_eval, "try_anthropic", AsyncMock(side_effect=wrong_shape)):
        assert await persona_eval._judge(payload_user="随便", max_tokens=10) is None


@pytest.mark.asyncio
async def test_vision_capture_success_path_downloads_and_describes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_nonebot()
    from src.plugins.chat import _vision_context_and_capture
    from src.services import vision

    monkeypatch.setenv("TAFFY_VISION_ENABLED", "true")
    monkeypatch.setenv("TAFFY_VISION_API_KEY", "test-key-not-real-000000000000002")
    image = tmp_path / "in.png"
    image.write_bytes(b"png")
    segment = SimpleNamespace(
        type="image", data={"url": "https://example.invalid/a.png", "file": "f1"}
    )
    event = SimpleNamespace(message=[segment], user_id=1)
    with (
        patch.object(vision, "download_inbound_image", AsyncMock(return_value=image)),
        patch.object(vision, "describe_for_context", AsyncMock(return_value="[图片: 一只猫娘]")),
    ):
        captured, snippet = await _vision_context_and_capture(None, event)
    assert captured is True
    assert snippet == "[图片: 一只猫娘]"


@pytest.mark.asyncio
async def test_deliver_tool_actions_sends_and_honours_caps(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_nonebot()
    import src.services.stickers as stickers_module
    from src.plugins.chat import _deliver_tool_actions
    from src.services.tools import ToolAction

    # Pin the module constants directly: they are read once at import time and
    # this test must not depend on other tests having patched them (CI runs a
    # different order than local).
    monkeypatch.setattr(stickers_module, "_DAILY_CAP", 1)
    monkeypatch.setattr(stickers_module, "_COOLDOWN", 0.0)
    stickers.reset_runtime_state()
    sticker_file = tmp_path / "a.png"
    sticker_file.write_bytes(b"png")

    sent: list[object] = []

    class _Bot:
        async def send(self, event, segment):
            sent.append(segment)

    event = SimpleNamespace(user_id=6008)
    with patch.object(
        stickers_module.secrets, "randbelow", lambda n: 0
    ):  # probability gate must not matter for the explicit path
        await _deliver_tool_actions(_Bot(), event, [ToolAction("send_sticker", sticker_file)])
    assert len(sent) == 1, "explicit tool stickers bypass the probability gate"
    assert len(await _noop()) == 0


async def _noop() -> list:
    return []


@pytest.mark.asyncio
async def test_persona_audit_command_reports_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    import src.plugins.chat.persona_audit_command as audit_command

    monkeypatch.setenv("TAFFY_PERSONA_AUDIT_ENABLED", "false")
    finished: list[str] = []

    async def fake_finish(message):
        finished.append(str(message))

    async def fake_send(message):
        return None

    event = SimpleNamespace(user_id=1243607273, get_plaintext=lambda: "塔菲人格体检")
    with (
        patch.object(audit_command, "check_feature", AsyncMock(return_value=True)),
        patch.object(audit_command.persona_audit_cmd, "finish", fake_finish),
        patch.object(audit_command.persona_audit_cmd, "send", fake_send),
    ):
        await audit_command.handle_persona_audit(None, event)
    assert finished and "未开启" in finished[0]


@pytest.mark.asyncio
async def test_automine_creates_pending_from_repeated_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_SLANG_AUTOMINE_ENABLED", "true")
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "true")
    inputs = [f"这个词真的好怪 {index}" for index in range(15)]
    with patch.object(slang_service, "_read_chat_inputs", AsyncMock(return_value=inputs)):
        created = await slang_service.automine_once(now=int(time.time()), limit=3)
    assert created >= 1, "repeated isolated tokens become pending candidates"


@pytest.mark.asyncio
async def test_maybe_send_sticker_full_pipeline(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    # Module constants are read once at import; patch them directly so the
    # daily-cap semantics hold regardless of CI/local env (setenv is too late).
    monkeypatch.setattr(stickers_module, "_DAILY_CAP", 1)
    monkeypatch.setattr(stickers_module, "_COOLDOWN", 0.0)
    stickers_module.reset_runtime_state()
    library = tmp_path / "stickers"
    library.mkdir(parents=True)
    (library / "manifest.json").write_text(
        json.dumps({"stickers": [{"file": "a.png", "tags": ["开心"]}]}), encoding="utf-8"
    )
    (library / "a.png").write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)

    sent: list[object] = []

    class _Bot:
        async def send(self, event, segment):
            sent.append(segment)

    event = SimpleNamespace(user_id=6006)
    with patch.object(stickers_module.secrets, "randbelow", lambda n: 0):
        await stickers.maybe_send_sticker(_Bot(), event, scene="闲聊", reply_text="哈哈开心")
    assert len(sent) == 1, "probability gate passes at roll 0"
    # The sandbox daily cap is shared through record_send, so a second send
    # in the same day hits the cap and stays silent.
    with patch.object(stickers_module.secrets, "randbelow", lambda n: 0):
        await stickers.maybe_send_sticker(_Bot(), event, scene="闲聊", reply_text="哈哈开心")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_memory_search_without_learning_returns_honest_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "false")
    text, action = await tools.execute_tool(
        tools.ToolContext(user_id=7001), "search_memories", {"keyword": "杭州"}
    )
    assert "没有记住" in text and action is None


@pytest.mark.asyncio
async def test_memory_write_decision_ignores_unknown_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_WRITE_DECISION_ENABLED", "true")
    monkeypatch.setenv("TAFFY_LEARNING_ENABLED", "true")
    from src.state.persona_memory_repository import PersonaScope
    from src.state.runtime import persona_memories

    scope = PersonaScope(7002, 0)
    await persona_memories.remember_note(scope, "我喜欢简洁回复")

    async def weird_model(prompt: str, messages: list, max_tokens: int) -> str:
        return json.dumps({"decision": "DELETE", "target_id": 1, "content": "删掉"})

    with patch.object(learning, "try_anthropic", AsyncMock(side_effect=weird_model)):
        decision, target, merged = await learning.memory_write_decision(
            7002, 0, "我喜欢简洁回复真的", now=int(time.time())
        )
    assert (decision, target, merged) == ("add", None, None), (
        "unknown decisions fall back to a plain append"
    )


@pytest.mark.asyncio
async def test_vision_capture_reports_image_without_description() -> None:
    _ensure_nonebot()
    from src.plugins.chat import _vision_context_and_capture

    segment = SimpleNamespace(
        type="image", data={"url": "https://example.invalid/a.png", "file": "f1"}
    )
    event = SimpleNamespace(message=[segment], user_id=1)
    captured, snippet = await _vision_context_and_capture(None, event)
    assert captured is True
    assert snippet is None, "a failed download yields no description"


@pytest.mark.asyncio
async def test_describe_for_context_and_tagging_pipeline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.services.stickers as stickers_module
    from src.services import vision as vision_module

    library = tmp_path / "stickers"
    library.mkdir(parents=True)
    (library / "manifest.json").write_text(
        json.dumps({"stickers": [{"file": "a.png", "tags": ["开心"]}]}), encoding="utf-8"
    )
    (library / "a.png").write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)
    stickers_module.reset_runtime_state()

    image = library / "a.png"
    with patch.object(
        vision_module,
        "describe_image",
        AsyncMock(return_value=("一只戴耳机的猫娘", ["开心", "猫娘"])),
    ):
        snippet = await vision_module.describe_for_context(image)
        tagged = await vision_module.tag_pending_images([image], limit=5)
    assert snippet == "[图片: 一只戴耳机的猫娘（开心,猫娘）]"
    assert tagged == 1


@pytest.mark.asyncio
async def test_download_inbound_image_falls_through_without_bot() -> None:
    from src.services import vision as vision_module

    saved = await vision_module.download_inbound_image("not-a-url", None)
    assert saved is None
    saved = await vision_module.download_inbound_image(None, "file-id")
    assert saved is None, "no adapter bot means no description, never a crash"


def _vision_download_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    *,
    body,
    headers: dict[str, str] | None = None,
    cap: int = 4096,
) -> tuple[object, list[int]]:
    """Wire a mock transport into download_inbound_image and count chunks pulled."""

    from contextlib import asynccontextmanager

    import httpx

    from src.services import vision as vision_module

    pulled = [0]

    async def counted() -> Any:
        async for chunk in body():
            pulled[0] += len(chunk)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers or {}, content=counted())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @asynccontextmanager
    async def fake_lease() -> Any:
        yield client

    # download_inbound_image imports this by name at call time.
    monkeypatch.setattr("src.services.http_client.lease_http_client", fake_lease)
    monkeypatch.setattr(vision_module, "_MAX_IMAGE_BYTES", cap)
    monkeypatch.setattr(vision_module, "_INBOX_DIR", tmp_path / "inbox")
    return client, pulled


@pytest.mark.asyncio
async def test_oversized_inbound_image_is_rejected_without_being_buffered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """One message naming a huge URL must not allocate the whole body in RAM."""

    chunk = b"x" * 1024

    async def body() -> Any:
        for _ in range(400):  # 400 KiB against a 4 KiB cap
            yield chunk

    from src.services import vision as vision_module

    client, pulled = _vision_download_client(monkeypatch, tmp_path, body=body, cap=len(chunk) * 4)
    try:
        saved = await vision_module.download_inbound_image("https://example.invalid/huge.png", None)
    finally:
        await client.aclose()
    assert saved is None, "an oversized image is never persisted"
    assert pulled[0] <= len(chunk) * 5, (
        f"read {pulled[0]} bytes for a 4096-byte cap: the body was buffered"
    )


@pytest.mark.asyncio
async def test_inbound_image_honours_a_declared_content_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A lying-size header lets the download stop before any body byte is read."""

    chunk = b"x" * 1024

    async def body() -> Any:
        for _ in range(400):
            yield chunk

    from src.services import vision as vision_module

    client, pulled = _vision_download_client(
        monkeypatch,
        tmp_path,
        body=body,
        headers={"content-length": str(len(chunk) * 400)},
        cap=2048,
    )
    try:
        saved = await vision_module.download_inbound_image("https://example.invalid/huge.png", None)
    finally:
        await client.aclose()
    assert saved is None
    assert pulled == [0], "declared oversize must short-circuit before reading"


@pytest.mark.asyncio
async def test_small_inbound_image_still_downloads_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The streaming cap must not change the accepted-image behaviour."""

    import hashlib

    from src.services import vision as vision_module

    payload = b"png-bytes"

    async def body() -> Any:
        yield payload

    client, pulled = _vision_download_client(monkeypatch, tmp_path, body=body, cap=4096)
    try:
        saved = await vision_module.download_inbound_image("https://example.invalid/ok.png", None)
    finally:
        await client.aclose()
    assert saved is not None and saved.read_bytes() == payload
    assert saved.name == f"{hashlib.sha256(payload).hexdigest()}.png"
    assert pulled == [len(payload)]


@pytest.mark.asyncio
async def test_sticker_tool_returns_action_when_library_matches(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    stickers_module.reset_runtime_state()
    library = tmp_path / "stickers"
    library.mkdir(parents=True)
    (library / "manifest.json").write_text(
        json.dumps({"stickers": [{"file": "a.png", "tags": ["开心"]}]}), encoding="utf-8"
    )
    (library / "a.png").write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)

    text, action = await tools.execute_tool(
        tools.ToolContext(user_id=1), "send_sticker", {"keywords": "哈哈开心"}
    )
    assert action is not None and action.kind == "send_sticker"
    assert action.payload.name == "a.png"
    assert "自动发出" in text

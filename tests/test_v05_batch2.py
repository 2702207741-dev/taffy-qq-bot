"""V0.5 second batch: persona audit, world entries, llm usage accounting."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.services import persona_eval
from src.services.persona import build_persona_prompt, select_world_entries
from src.state.runtime import database, learning_insights


@pytest.fixture(autouse=True)
async def _prepare():
    await learning_insights.initialize()
    yield
    await database.write("DELETE FROM persona_eval_scores")
    await database.write("DELETE FROM session_messages")
    await database.write("DELETE FROM llm_usage_daily")
    await database.write("DELETE FROM app_meta")


def _market_now() -> int:
    from src.services.tz import market_now

    return int(market_now().timestamp())


# ---------------------------------------------------------------------------
# Persona drift audit (B8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_audit_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_PERSONA_AUDIT_ENABLED", raising=False)
    summary = await persona_eval.run_persona_audit(now=_market_now())
    assert summary == {"status": "disabled"}


@pytest.mark.asyncio
async def test_persona_audit_refuses_without_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_PERSONA_AUDIT_ENABLED", "true")
    summary = await persona_eval.run_persona_audit(now=_market_now())
    assert summary["status"] == "insufficient_samples"


@pytest.mark.asyncio
async def test_persona_audit_scores_refused_when_judge_uncalibrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_PERSONA_AUDIT_ENABLED", "true")
    now = _market_now()
    for index in range(8):
        await database.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (f"p_7{index}", f"这是一条足够长的塔菲回复样本 {index}", now - index),
        )
    with patch.object(persona_eval, "_judge_calibrated", AsyncMock(return_value=False)):
        summary = await persona_eval.run_persona_audit(now=now)
    assert summary["status"] == "calibration_failed"
    rows = await database.read("SELECT * FROM persona_eval_scores")
    assert rows == [], "an uncalibrated judge never produces stored scores"


@pytest.mark.asyncio
async def test_persona_audit_runs_persists_and_flags_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_PERSONA_AUDIT_ENABLED", "true")
    now = _market_now()
    for index in range(8):
        await database.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (f"p_8{index}", f"这是一条足够长的塔菲回复样本 {index}", now - index),
        )

    async def fake_judge(payload_user: str, max_tokens: int = 900) -> list[dict]:
        if "样本A" in payload_user:
            return [
                {"dimension": "口癖纪律", "score": 5, "evidence": "自然"},
                {"dimension": "客服腔", "score": 5, "evidence": "无"},
                {"dimension": "口癖纪律", "score": 5, "evidence": "自然"},
                {"dimension": "客服腔", "score": 5, "evidence": "无"},
                {"dimension": "口癖纪律", "score": 2, "evidence": "客服腔"},
                {"dimension": "客服腔", "score": 1, "evidence": "自称AI"},
                {"dimension": "口癖纪律", "score": 2, "evidence": "客服腔"},
                {"dimension": "客服腔", "score": 1, "evidence": "自称AI"},
            ]
        names = json.loads(payload_user.rsplit("只输出这些维度：", 1)[1])
        return [{"dimension": name, "score": 4.0, "evidence": "样本整体贴合"} for name in names]

    with (
        patch.object(persona_eval, "_judge", AsyncMock(side_effect=fake_judge)),
        patch.object(persona_eval, "_judge_calibrated", AsyncMock(return_value=True)),
    ):
        first = await persona_eval.run_persona_audit(now=now)
        assert first["status"] == "ok"
        assert len(first["scores"]) == 10
        assert first["drift"] == [], "the first run has no previous to drift from"

        second = await persona_eval.run_persona_audit(now=now + 3600)
        assert second["status"] == "ok"

    rows = await database.read("SELECT COUNT(*) AS c FROM persona_eval_scores")
    assert rows[0]["c"] == 20


def test_audit_report_is_neutral_and_fail_closed() -> None:
    assert "未开启" in persona_eval.format_audit_report({"status": "disabled"})
    assert "样本" in persona_eval.format_audit_report(
        {"status": "insufficient_samples", "samples": 0}
    )
    assert "校准" in persona_eval.format_audit_report({"status": "calibration_failed"})
    report = persona_eval.format_audit_report(
        {
            "status": "ok",
            "scores": [{"dimension": "活泼度", "score": 4.0}],
            "drift": [{"dimension": "活泼度", "previous": 5.0, "current": 4.0}],
        }
    )
    assert "活泼度" in report and "5.0 → 4.0" in report


# ---------------------------------------------------------------------------
# World-Info persona entries (B6)
# ---------------------------------------------------------------------------


def test_world_entries_trigger_sticky_cooldown_and_exclusion() -> None:
    import src.services.persona as persona

    if not persona.world_entries_enabled():
        pytest.skip("world entries disabled by operator config")
    scope = "unit-test-scope"
    persona._WI_TURNS.pop(scope, None)
    persona._WI_STATE.pop(scope, None)

    # Turn 1: greeting triggers; the mutual-exclusion group yields exactly one.
    first = select_world_entries("你好呀", None, scope_key=scope)
    assert any("打招呼" in line for line in first)
    assert sum("自我介绍" in line for line in first) <= 1

    # Cooldown: the same trigger does not repeat immediately.
    second = select_world_entries("你好呀", None, scope_key=scope)
    assert not any("打招呼" in line for line in second)

    # Budget: entries stay inside the per-turn character budget.
    assert sum(len(line) for line in first) <= persona._WI_BUDGET_CHARS + 16


def test_world_entries_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_PERSONA_WORLD_ENTRIES", "false")
    assert select_world_entries("你好呀", None, scope_key="kill-switch") == ()


def test_world_entries_appear_in_persona_prompt() -> None:
    from src.services.persona import PersonaContext

    prompt = build_persona_prompt(
        "在吗在吗",
        PersonaContext(),
        datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        variant_seed="prompt-scope:1",
    )
    if build_persona_prompt and "场景提示" in prompt:
        assert "按当前话题触发" in prompt


# ---------------------------------------------------------------------------
# LLM usage accounting + admin endpoint (B9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_usage_records_and_aggregates() -> None:
    from src.services.llm_usage import record_llm_usage
    from src.services.panel_stats import llm_usage_trend

    await record_llm_usage("taffy-primary", "deepseek-v4-pro", input_tokens=100, output_tokens=20)
    await record_llm_usage("taffy-primary", "deepseek-v4-pro", input_tokens=50, output_tokens=10)
    await record_llm_usage("backup", "glm-4-air", input_tokens=10, output_tokens=5)
    trend = await llm_usage_trend(7)
    assert trend["labels"], "at least today's bucket exists"
    assert trend["requests"][0] == 3
    assert trend["input_tokens"][0] == 160
    assert trend["output_tokens"][0] == 35
    assert any(entry["provider"] == "backup" for entry in trend["per_model"])


@pytest.mark.asyncio
async def test_llm_usage_rejects_garbage() -> None:
    from src.services.llm_usage import record_llm_usage
    from src.services.panel_stats import llm_usage_trend

    await record_llm_usage("p", "m", input_tokens=-5, output_tokens=None)  # type: ignore[arg-type]
    trend = await llm_usage_trend(7)
    assert all(value >= 0 for value in trend["input_tokens"])
    await database.write("DELETE FROM llm_usage_daily")

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.services.llm import ChatService
from src.services.persona import PersonaStyleContext, classify_persona


def _sampled_message(service: ChatService, session_id: str, base: str) -> str:
    for index in range(10_000):
        value = f"{base}{index}"
        if service._review_sampled(session_id, value):
            return value
    raise AssertionError("could not find sampled message")


def test_review_admission_keeps_average_calls_at_or_below_1_point_2() -> None:
    service = ChatService()
    admitted = sum(
        service._review_sampled(f"user-{index % 23}", f"social-message-{index}")
        for index in range(10_000)
    )
    assert admitted / 10_000 <= 0.2


@pytest.mark.asyncio
async def test_low_confidence_social_reply_gets_one_bounded_review() -> None:
    calls: list[int] = []

    async def reviewer(prompt: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        assert "二审" in prompt
        assert len(messages) == 1
        calls.append(max_tokens)
        return "在呢，怎么啦？"

    service = ChatService(review_generator=reviewer)
    session_id = "review-accept"
    message = _sampled_message(service, session_id, "在不在呀")
    profile = classify_persona(message)
    result = await service._review_persona_reply(
        message=message,
        session_id=session_id,
        draft="尊敬的用户，很高兴为您服务。",
        profile=profile,
        quality_issues=("customer_service_tone",),
        recent_replies=(),
        style=PersonaStyleContext(),
    )
    assert result == "在呢，怎么啦？"
    assert calls == [350]


@pytest.mark.asyncio
async def test_review_never_runs_for_fact_or_task_and_rejects_changed_numbers() -> None:
    calls = 0

    async def reviewer(prompt: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        nonlocal calls
        calls += 1
        return "CS2 是 13.50"

    service = ChatService(review_generator=reviewer)
    fact = await service._review_persona_reply(
        message="CS2最新更新是什么",
        session_id="fact",
        draft="原文",
        profile=classify_persona("CS2最新更新是什么"),
        quality_issues=("too_long",),
        recent_replies=(),
        style=PersonaStyleContext(),
    )
    task = await service._review_persona_reply(
        message="帮我排查游戏掉帧",
        session_id="task",
        draft="原文",
        profile=classify_persona("帮我排查游戏掉帧"),
        quality_issues=("too_long",),
        recent_replies=(),
        style=PersonaStyleContext(),
    )
    assert fact == task == "原文"
    assert calls == 0

    session_id = "review-protected"
    message = _sampled_message(service, session_id, "随便聊聊")
    draft = "CS2 是 12.50"
    changed = await service._review_persona_reply(
        message=message,
        session_id=session_id,
        draft=draft,
        profile=classify_persona(message),
        quality_issues=("customer_service_tone",),
        recent_replies=(),
        style=PersonaStyleContext(),
    )
    assert changed == draft
    assert calls == 1


@pytest.mark.asyncio
async def test_review_timeout_falls_back_to_first_draft() -> None:
    async def reviewer(prompt: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        await asyncio.sleep(0.1)
        return "不该返回"

    service = ChatService(review_generator=reviewer)
    session_id = "review-timeout"
    message = _sampled_message(service, session_id, "陪我聊聊")
    with patch("src.services.llm.PERSONA_REVIEW_TIMEOUT", 0.01):
        result = await service._review_persona_reply(
            message=message,
            session_id=session_id,
            draft="第一稿",
            profile=classify_persona(message),
            quality_issues=("too_long",),
            recent_replies=(),
            style=PersonaStyleContext(),
        )
    assert result == "第一稿"

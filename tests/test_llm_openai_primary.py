from unittest.mock import AsyncMock

import pytest

from src.services import llm
from src.services.tools import ToolContext


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_primary_openai_uses_own_endpoint_and_credentials(monkeypatch, with_tools):
    monkeypatch.setattr(llm, "TAFFY_OPENAI_MIN_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(llm, "TAFFY_KEY", "synthetic-primary")
    monkeypatch.setattr(llm, "TAFFY_URL", "https://primary.test/provider/v1/")
    monkeypatch.setattr(llm, "TAFFY_MODEL", "synthetic-model")
    usage = AsyncMock()
    monkeypatch.setattr(llm, "_record_openai_usage", usage)
    payload = {"choices": [{"message": {"content": "answer"}}]}
    requester = AsyncMock(return_value=payload)
    if with_tools:
        reply = await llm.fallback_openai_with_tools(
            "system", [], 1800, ToolContext(1), requester=requester, primary=True
        )
    else:
        reply = await llm.fallback_openai("system", [], 1800, requester=requester, primary=True)
    assert reply == "answer"
    args, kwargs = requester.call_args
    assert args == ("POST", "https://primary.test/provider/v1/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer synthetic-primary"
    assert kwargs["json"]["model"] == "synthetic-model"
    assert kwargs["json"]["max_tokens"] == 1800
    assert ("tools" in kwargs["json"]) is with_tools
    usage.assert_awaited_once_with("taffy-primary", "synthetic-model", payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_openai_dispatch_never_constructs_anthropic_client(monkeypatch, with_tools):
    monkeypatch.setattr(llm, "TAFFY_API_FORMAT", "openai")
    target = "fallback_openai_with_tools" if with_tools else "fallback_openai"
    request = AsyncMock(return_value="answer")
    monkeypatch.setattr(llm, target, request)
    factory = AsyncMock(side_effect=AssertionError("wrong protocol"))
    if with_tools:
        answer = await llm.try_anthropic_with_tools(
            "system", [], 1800, ToolContext(1), client_factory=factory
        )
    else:
        answer = await llm.try_anthropic("system", [], 1800, client_factory=factory)
    assert answer == "answer"
    factory.assert_not_awaited()
    assert request.call_args.kwargs["primary"] is True


@pytest.mark.asyncio
async def test_primary_failure_still_uses_independent_fallback(monkeypatch):
    monkeypatch.setattr(llm, "try_anthropic", AsyncMock(side_effect=RuntimeError("primary")))
    fallback = AsyncMock(return_value="fallback answer")
    monkeypatch.setattr(llm, "fallback_openai", fallback)
    assert await llm.chat_service._call_model("system", [], 800) == "fallback answer"
    fallback.assert_awaited_once_with("system", [], 800)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("primary", [False, True])
async def test_reasoning_budget_reserve_only_applies_to_primary(monkeypatch, with_tools, primary):
    monkeypatch.setattr(llm, "LLM_TOTAL_TIMEOUT", 180.0)
    monkeypatch.setattr(llm, "TAFFY_KEY", "synthetic-primary")
    monkeypatch.setattr(llm, "TAFFY_URL", "https://primary.test/v1")
    monkeypatch.setattr(llm, "DS_KEY", "synthetic-fallback")
    monkeypatch.setattr(llm, "DS_URL", "https://fallback.test/v1/chat/completions")
    monkeypatch.setattr(llm, "TAFFY_OPENAI_MIN_OUTPUT_TOKENS", 8192)
    monkeypatch.setattr(llm, "_record_openai_usage", AsyncMock())
    requester = AsyncMock(return_value={"choices": [{"message": {"content": "answer"}}]})
    if with_tools:
        await llm.fallback_openai_with_tools(
            "system", [], 1800, ToolContext(1), requester=requester, primary=primary
        )
    else:
        await llm.fallback_openai("system", [], 1800, requester=requester, primary=primary)
    assert requester.call_args.kwargs["json"]["max_tokens"] == (8192 if primary else 1800)
    assert requester.call_args.kwargs["timeout"].read == (120.0 if primary else 60.0)
    assert requester.call_args.kwargs["total_timeout"] == (120.0 if primary else 75.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_empty_length_primary_retries_once_without_lowering_first_effort(
    monkeypatch, with_tools
):
    monkeypatch.setattr(llm, "TAFFY_OPENAI_EMPTY_LENGTH_RETRY", True)
    monkeypatch.setattr(llm, "TAFFY_KEY", "synthetic-primary")
    monkeypatch.setattr(llm, "TAFFY_URL", "https://primary.test/v1")
    usage = AsyncMock()
    monkeypatch.setattr(llm, "_record_openai_usage", usage)
    empty = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    answer = {"choices": [{"finish_reason": "stop", "message": {"content": "three weighings"}}]}
    requester = AsyncMock(side_effect=[empty, answer])
    if with_tools:
        result = await llm.fallback_openai_with_tools(
            "system", [], 1800, ToolContext(1), requester=requester, primary=True
        )
    else:
        result = await llm.fallback_openai("system", [], 1800, requester=requester, primary=True)
    assert result == "three weighings"
    assert requester.await_count == 2
    first, second = requester.await_args_list
    assert "reasoning_effort" not in first.kwargs["json"]
    assert second.kwargs["json"] == {**first.kwargs["json"], "reasoning_effort": "low"}
    assert second.kwargs["attempts"] == 1
    assert usage.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary,enabled,finish,message",
    [
        (False, True, "length", {"content": ""}),
        (True, False, "length", {"content": ""}),
        (True, True, "stop", {"content": ""}),
        (True, True, "length", {"content": "partial answer"}),
        (True, True, "length", {"content": "", "tool_calls": [{"id": "synthetic"}]}),
    ],
)
async def test_retry_does_not_replay_tools_or_override_other_responses(
    monkeypatch, primary, enabled, finish, message
):
    monkeypatch.setattr(llm, "TAFFY_OPENAI_EMPTY_LENGTH_RETRY", enabled)
    payload = {"choices": [{"finish_reason": finish, "message": message}]}
    requester = AsyncMock(return_value=payload)
    result = await llm._request_openai_completion(
        requester, "https://primary.test/v1", primary=primary, json={"model": "synthetic"}
    )
    assert result is payload
    assert requester.await_count == 1


@pytest.mark.asyncio
async def test_repeated_empty_length_is_bounded_and_cancellation_propagates(monkeypatch):
    import asyncio

    monkeypatch.setattr(llm, "TAFFY_OPENAI_EMPTY_LENGTH_RETRY", True)
    monkeypatch.setattr(llm, "_record_openai_usage", AsyncMock())
    empty = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    requester = AsyncMock(return_value=empty)
    assert (
        await llm._request_openai_completion(
            requester, "https://primary.test/v1", primary=True, json={"model": "synthetic"}
        )
        is empty
    )
    assert requester.await_count == 2
    requester = AsyncMock(side_effect=[empty, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await llm._request_openai_completion(
            requester, "https://primary.test/v1", primary=True, json={"model": "synthetic"}
        )
    assert requester.await_count == 2


def _primary_env(monkeypatch: pytest.MonkeyPatch, *, retry: bool) -> AsyncMock:
    monkeypatch.setattr(llm, "TAFFY_OPENAI_EMPTY_LENGTH_RETRY", retry)
    monkeypatch.setattr(llm, "TAFFY_OPENAI_MIN_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(llm, "TAFFY_KEY", "synthetic-primary")
    monkeypatch.setattr(llm, "TAFFY_URL", "https://primary.test/v1")
    monkeypatch.setattr(llm, "TAFFY_MODEL", "synthetic-model")
    usage = AsyncMock()
    monkeypatch.setattr(llm, "_record_openai_usage", usage)
    return usage


@pytest.mark.asyncio
async def test_empty_body_recorded_then_raised_when_retry_disabled(monkeypatch):
    """An empty primary body must still be accounted even when the low-effort
    retry is off (the default): the usage row exists, then the call raises so
    the outer chain can fall back."""
    usage = _primary_env(monkeypatch, retry=False)
    empty = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    requester = AsyncMock(return_value=empty)
    with pytest.raises(RuntimeError, match="empty OpenAI-compatible response"):
        await llm.fallback_openai("system", [], 1800, requester=requester, primary=True)
    assert requester.await_count == 1, "retry disabled -> a single request"
    assert usage.await_count == 1
    usage.assert_awaited_once_with("taffy-primary", "synthetic-model", empty)


@pytest.mark.asyncio
async def test_stop_empty_body_recorded_without_retry(monkeypatch):
    """finish_reason=stop with an empty body is not a budget-exhaustion
    signature, so no retry happens — but the response is still accounted."""
    usage = _primary_env(monkeypatch, retry=True)
    stop_empty = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
    requester = AsyncMock(return_value=stop_empty)
    with pytest.raises(RuntimeError, match="empty OpenAI-compatible response"):
        await llm.fallback_openai("system", [], 1800, requester=requester, primary=True)
    assert requester.await_count == 1
    usage.assert_awaited_once_with("taffy-primary", "synthetic-model", stop_empty)


@pytest.mark.asyncio
async def test_retry_second_empty_is_bounded_and_both_responses_accounted(monkeypatch):
    """Retry fires once; when the second attempt is also empty the call fails
    (bounded, no third attempt) and BOTH empty responses are accounted."""
    usage = _primary_env(monkeypatch, retry=True)
    empty = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    requester = AsyncMock(return_value=empty)
    with pytest.raises(RuntimeError, match="empty OpenAI-compatible response"):
        await llm.fallback_openai("system", [], 1800, requester=requester, primary=True)
    assert requester.await_count == 2, "exactly one low-effort retry, no third attempt"
    assert usage.await_count == 2
    usage.assert_any_await("taffy-primary", "synthetic-model", empty)


@pytest.mark.asyncio
async def test_tool_round_empty_body_accounted_before_raise(monkeypatch):
    """The tool loop accounts every round payload; an empty no-tool response
    in the primary tool loop is recorded, then fails the round."""
    usage = _primary_env(monkeypatch, retry=True)
    empty = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    requester = AsyncMock(return_value=empty)
    with pytest.raises(RuntimeError, match="empty OpenAI-compatible response"):
        await llm.fallback_openai_with_tools(
            "system", [], 1800, ToolContext(1), requester=requester, primary=True
        )
    assert requester.await_count == 2
    assert usage.await_count == 2

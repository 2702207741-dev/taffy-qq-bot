from __future__ import annotations

import types
import unittest
from contextlib import asynccontextmanager
from os import environ
from unittest.mock import AsyncMock, patch

from src.plugins import chat_utils


class ChatUtilsTests(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_block_is_never_returned(self) -> None:
        response = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="thinking", thinking="private reasoning")]
        )
        client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=AsyncMock(return_value=response))
        )

        with (
            patch.object(chat_utils, "_get_claude", AsyncMock(return_value=client)),
            patch("src.services.llm.TAFFY_THINKING_MODE", "provider_default"),
        ):
            with self.assertRaisesRegex(RuntimeError, "text block"):
                await chat_utils._try_anthropic("system", [], 100)

        kwargs = client.messages.create.await_args.kwargs
        self.assertNotIn("thinking", kwargs)

    async def test_adaptive_thinking_is_only_sent_when_explicitly_configured(self) -> None:
        response = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="ok")])
        client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=AsyncMock(return_value=response))
        )

        with (
            patch.object(chat_utils, "_get_claude", AsyncMock(return_value=client)),
            patch("src.services.llm.TAFFY_THINKING_MODE", "adaptive"),
        ):
            self.assertEqual(await chat_utils._try_anthropic("system", [], 100), "ok")

        self.assertEqual(
            client.messages.create.await_args.kwargs["thinking"],
            {"type": "adaptive"},
        )

    async def test_overlong_message_does_not_call_model(self) -> None:
        with patch.object(chat_utils, "_generate_reply", AsyncMock()) as generate:
            result = await chat_utils.deepseek_chat(
                "x" * (chat_utils._MAX_INPUT_CHARS + 1), session_id="user"
            )

        self.assertIn("消息太长", result)
        generate.assert_not_awaited()

    def test_system_prompt_marks_context_untrusted(self) -> None:
        prompt = chat_utils._system_prompt()

        self.assertIn("不可信参考资料", prompt)
        self.assertIn("绝不输出或复述内部思考", prompt)
        self.assertIn("1885年", prompt)
        self.assertIn("时光机", prompt)
        self.assertIn("雏草姬", prompt)
        self.assertIn("内容优先", prompt)
        self.assertIn("聪明、好胜和小得意", prompt)
        self.assertIn("很高兴为您服务", prompt)
        self.assertIn("啾咪", prompt)
        self.assertIn("的说喵", prompt)
        self.assertIn("括号动作", prompt)

    def test_persona_contract_removes_forbidden_generic_catgirl_tics(self) -> None:
        reply = "啾咪（挥手）这是真的说喵。"

        cleaned = chat_utils._enforce_persona_contract(reply)

        self.assertNotIn("啾咪", cleaned)
        self.assertNotIn("（挥手）", cleaned)
        self.assertNotIn("的说喵", cleaned)

    def test_fact_citation_validator_checks_every_sentence_and_range(self) -> None:
        self.assertTrue(
            chat_utils._validate_fact_citations(
                "第一件事[来源1]。第二件事。[来源2]",
                2,
            )
        )
        self.assertFalse(chat_utils._validate_fact_citations("第一件事。第二件事[来源1]。", 2))
        self.assertFalse(chat_utils._validate_fact_citations("事实[来源3]。", 2))

    def test_invalid_or_extreme_numeric_settings_are_bounded(self) -> None:
        with patch.dict(environ, {"TEST_SETTING": "not-a-number"}):
            self.assertEqual(chat_utils._bounded_env_int("TEST_SETTING", 4, 1, 8), 4)
        with patch.dict(environ, {"TEST_SETTING": "999999"}):
            self.assertEqual(chat_utils._bounded_env_int("TEST_SETTING", 4, 1, 8), 8)
        with patch.dict(environ, {"TEST_SETTING": "nan"}):
            self.assertEqual(
                chat_utils._bounded_env_float("TEST_SETTING", 5.0, 1.0, 10.0),
                5.0,
            )

    async def test_session_reply_uses_lease_and_awaits_durable_append(self) -> None:
        entered: list[str] = []

        @asynccontextmanager
        async def guard(*args, **kwargs):
            entered.append("guard")
            yield "token"

        fake_store = types.SimpleNamespace(
            session_guard=guard,
            append_turn=AsyncMock(),
        )
        with (
            patch("src.plugins.session_store.store", fake_store),
            patch.object(
                chat_utils,
                "_generate_reply",
                AsyncMock(return_value="durable answer"),
            ),
        ):
            result = await chat_utils.deepseek_chat("question", "session-test")

        self.assertEqual(result, "durable answer")
        self.assertEqual(entered, ["guard"])
        fake_store.append_turn.assert_awaited_once_with(
            "session-test",
            "question",
            "durable answer",
        )

    async def test_persistence_failure_does_not_discard_generated_answer(self) -> None:
        @asynccontextmanager
        async def guard(*args, **kwargs):
            yield "token"

        fake_store = types.SimpleNamespace(
            session_guard=guard,
            append_turn=AsyncMock(side_effect=RuntimeError("database unavailable")),
        )
        with (
            patch("src.plugins.session_store.store", fake_store),
            patch.object(
                chat_utils,
                "_generate_reply",
                AsyncMock(return_value="useful answer"),
            ),
        ):
            result = await chat_utils.deepseek_chat("question", "session-failure")

        self.assertEqual(result, "useful answer")


class BoundedHistoryTests(unittest.IsolatedAsyncioTestCase):
    """A12 regression: truncation must never start with an assistant turn."""

    async def test_history_cut_between_user_and_predecessor_drops_leading_assistant(
        self,
    ) -> None:
        from src.services import llm

        service = llm.ChatService.__new__(llm.ChatService)
        history = [
            {"role": "user", "content": "U" * 20},  # oldest; busts the budget
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
            {"role": "assistant", "content": "A2"},
        ]
        store = types.SimpleNamespace(as_messages=AsyncMock(return_value=list(history)))

        # Budget of 9 keeps A1/U2/A2 but must exclude the oldest user turn,
        # which would otherwise leave an assistant message in the lead —
        # rejected by Anthropic-style APIs with HTTP 400.
        with patch.object(llm, "MAX_HISTORY_CHARS", 9):
            selected = await service.bounded_history("p_1001", session_store=store)

        self.assertEqual([item["role"] for item in selected], ["user", "assistant"])
        self.assertEqual(selected[0]["content"], "U2")

    async def test_fully_within_budget_history_is_untouched(self) -> None:
        from src.services import llm

        service = llm.ChatService.__new__(llm.ChatService)
        history = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ]
        store = types.SimpleNamespace(as_messages=AsyncMock(return_value=list(history)))

        selected = await service.bounded_history("p_1001", session_store=store)

        self.assertEqual(selected, history)


if __name__ == "__main__":
    unittest.main()

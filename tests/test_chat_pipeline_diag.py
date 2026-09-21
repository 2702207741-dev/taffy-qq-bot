"""Handler-level pipeline tests for the diagnosed fixed case set.

Drives the real chat handler (routing, session lease, context building and
post-processing stay real) with sandboxed repositories, a captured send
boundary and a stubbed model stage.  The real-model logic check for the
riddle is part of the diagnostic run, not these tests: a stub cannot prove
answer logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import chat
from src.services import llm
from src.services.fact_intent import detect_fact_intents

RIDDLE = (
    "任务：12个外观完全一样小球，其中1个重量异常，不知道是轻还是重。\n"
    "只回答两点：①天平最少需要称几次；②简短写出核心称量方案。\n"
    "禁止角色扮演，不要动作描写，只输出逻辑内容。"
)
SIMPLE_QA = "塔菲你好呀，今天过得怎么样？"
RIDDLE_ANSWER = (
    "①最少 3 次。②核心方案：先分三组各 4 球称第一次，再按结果换组称第二次，最后定位并判定轻重。"
)
FAILURE_TEXT = "塔菲暂时连不上大脑喵，请稍后再试。"


def _private(text: str, user_id: int) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=2,
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=3,
        user_id=user_id,
        message=text,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "u", "sex": "unknown", "age": 0},
    )


def _sent_texts(bot: SimpleNamespace) -> list[str]:
    texts: list[str] = []
    for call in bot.send.await_args_list:
        if len(call.args) >= 2:
            texts.append(str(call.args[1]))
        elif "message" in call.kwargs:
            texts.append(str(call.kwargs["message"]))
    return texts


async def _run(
    text: str,
    user_id: int,
    *,
    model_result: str | None = None,
    model_error: Exception | None = None,
) -> tuple[SimpleNamespace, AsyncMock]:
    from src.state.runtime import initialize_state

    await initialize_state()
    bot = SimpleNamespace(send=AsyncMock())
    event = _private(text, user_id)
    if model_error is not None:
        model = AsyncMock(side_effect=model_error)
    else:
        model = AsyncMock(return_value=model_result)
    with (
        patch.object(llm.chat_service, "_call_model_with_tools", model),
        patch.object(
            llm.chat_service,
            "_review_persona_reply",
            AsyncMock(side_effect=lambda **kwargs: kwargs["draft"]),
        ),
    ):
        await chat._handle_chat_message(bot, event)
    return bot, model


async def test_riddle_reaches_model_and_sends_a_body() -> None:
    bot, model = await _run(RIDDLE, 20001, model_result=RIDDLE_ANSWER)

    assert model.await_count == 1, "the riddle must reach the model exactly once"
    messages = model.await_args.args[1]
    assert any("12个外观完全一样小球" in item["content"] for item in messages)
    sent = _sent_texts(bot)
    assert sent and "①最少 3 次" in sent[0]
    assert "核心方案" in sent[0]


async def test_simple_chinese_qa_chain_sends_a_body() -> None:
    bot, model = await _run(SIMPLE_QA, 20002, model_result="挺好的喵，你呢？")

    assert model.await_count == 1
    assert any("挺好的" in text for text in _sent_texts(bot))


async def test_all_channels_failed_branch_still_sends_the_failure_text() -> None:
    bot, model = await _run(
        RIDDLE,
        20003,
        model_error=RuntimeError("synthetic all-channels failure"),
    )

    assert model.await_count == 1
    assert any(FAILURE_TEXT in text for text in _sent_texts(bot))


def test_riddle_is_not_intercepted_by_fact_routing() -> None:
    assert detect_fact_intents(RIDDLE) == ()

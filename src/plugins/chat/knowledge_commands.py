"""Local knowledge lookup over confirmed insights — zero LLM involvement.

``塔菲知识 <问题>`` searches the caller's accepted ``knowledge`` insights with
the shared dependency-free term matcher.  A hit answers instantly from stored
summaries; a miss states plainly that the fact is not memorised yet instead
of letting a model improvise one.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, MessageEvent
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.learning import learning_enabled

from ..feature_gate import check_feature, record_event_usage
from .routing import persona_scope

_log = logging.getLogger("taffy.knowledge_commands")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)
_CLOSED_MESSAGE = "塔菲的学习功能已关闭，管理员可在配置里打开。"
_MAX_ANSWERS = 3


def _is_knowledge_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return text.startswith(("塔菲知识", "塔菲知道"))


knowledge_cmd = on_message(rule=Rule(_is_knowledge_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await knowledge_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


async def _lookup_reply(event: MessageEvent, question: str) -> str:
    from src.services.memory_search import hybrid_search_with_reinforcement

    scope = persona_scope(event)
    result = await hybrid_search_with_reinforcement(
        scope.owner_user_id, scope.group_id, question, kind="knowledge", limit=_MAX_ANSWERS
    )
    if not result.hits:
        return "这个问题我还没记住，下次回答后我会尝试记下来。"
    lines = ["📚 【塔菲知识】"]
    for index, hit in enumerate(result.hits, 1):
        item = hit.insight
        lines.append(f"  {index}. {item.content}")
        lines.append(f"     依据：{item.evidence}")
    lines.append(f"检索通道：{result.channel_label}")
    return "\n".join(lines)


@knowledge_cmd.handle()
async def handle_knowledge(_bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(knowledge_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "塔菲知识查询太频繁了，请稍后再试。")
    if not learning_enabled():
        await _finish(event, _CLOSED_MESSAGE)
    question = ""
    for prefix in ("塔菲知识", "塔菲知道"):
        if text.startswith(prefix):
            question = text[len(prefix) :].strip()
            break
    if not question or len(question) > 200:
        await _finish(event, "格式：塔菲知识 <问题>（例如：塔菲知识 红线现在多少钱）")
    try:
        await _finish(event, await _lookup_reply(event, question))
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Knowledge command failed error=%s", type(exc).__name__)
        await _finish(event, "塔菲知识这次没查到，请稍后再试。")


__all__ = ["handle_knowledge", "knowledge_cmd"]

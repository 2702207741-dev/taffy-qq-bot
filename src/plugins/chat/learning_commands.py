"""Semi-automated learning commands: insights are confirmable, deletable, off-able."""

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

_log = logging.getLogger("taffy.learning_commands")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)
_CLOSED_MESSAGE = "塔菲的学习功能已关闭，管理员可在配置里打开。"

_EXACT_COMMANDS = {"塔菲印象", "塔菲观察"}


def _is_learning_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return text in _EXACT_COMMANDS or text.startswith(("确认印象 ", "拒绝印象 ", "删除印象 "))


learning_cmd = on_message(rule=Rule(_is_learning_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await learning_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


def _index(text: str, prefix: str) -> int | None:
    raw = text.removeprefix(prefix).strip()
    if raw.isdecimal():
        return int(raw)
    return None


async def _accepted_reply(event: MessageEvent) -> str:
    from src.state.runtime import learning_insights

    scope = persona_scope(event)
    insights = await learning_insights.list_scope(scope.owner_user_id, scope.group_id)
    if not insights:
        return "还没有已接受的印象。发送“塔菲观察”看看塔菲注意到了什么。"
    lines = ["🧠 【塔菲印象】"]
    for index, insight in enumerate(insights, 1):
        lines.append(f"  {index}. [{_kind_label(insight.kind)}] {insight.content}")
    lines.append(f"ℹ️ 只显示当前{scope.label}；印象可被遗忘归档，也可用“删除印象 序号”移除。")
    return "\n".join(lines)


async def _pending_reply(event: MessageEvent) -> str:
    from src.state.runtime import learning_insights

    scope = persona_scope(event)
    insights = await learning_insights.list_scope(
        scope.owner_user_id, scope.group_id, status="pending"
    )
    if not insights:
        return "暂时没有待确认的印象。"
    lines = ["👀 【塔菲观察】"]
    for index, insight in enumerate(insights, 1):
        lines.append(f"  {index}. {insight.content}")
        lines.append(f"     依据：{insight.evidence}")
        lines.append(f"     确认：确认印象 {index}｜拒绝：拒绝印象 {index}")
    return "\n".join(lines)


def _kind_label(kind: str) -> str:
    return {
        "preference": "偏好",
        "style": "风格",
        "knowledge": "知识",
        "cultural": "群文化",
    }.get(kind, kind)


async def _confirm(event: MessageEvent, raw_index: str) -> str:
    from src.state.runtime import learning_insights

    scope = persona_scope(event)
    index = _index(raw_index, "确认印象")
    if index is None or index < 1:
        return "格式：确认印象 1"
    insights = await learning_insights.list_scope(
        scope.owner_user_id, scope.group_id, status="pending"
    )
    if index > len(insights):
        return "没有这个编号，先发“塔菲观察”看看。"
    selected = insights[index - 1]
    if await learning_insights.confirm(selected.id, scope.owner_user_id, scope.group_id):
        return f"塔菲记住啦~ 印象已保存：{selected.content}"
    return "这条印象状态已变化，请重新查看。"


async def _reject(event: MessageEvent, raw_index: str) -> str:
    from src.state.runtime import learning_insights

    scope = persona_scope(event)
    index = _index(raw_index, "拒绝印象")
    if index is None or index < 1:
        return "格式：拒绝印象 1"
    insights = await learning_insights.list_scope(
        scope.owner_user_id, scope.group_id, status="pending"
    )
    if index > len(insights):
        return "没有这个编号，先发“塔菲观察”看看。"
    selected = insights[index - 1]
    if await learning_insights.reject(selected.id, scope.owner_user_id, scope.group_id):
        return "这条不算数，忘掉了。"
    return "这条印象状态已变化，请重新查看。"


async def _delete(event: MessageEvent, raw_index: str) -> str:
    from src.state.runtime import learning_insights

    scope = persona_scope(event)
    index = _index(raw_index, "删除印象")
    if index is None or index < 1:
        return "格式：删除印象 1"
    insights = await learning_insights.list_scope(scope.owner_user_id, scope.group_id)
    if index > len(insights):
        return "没有这个编号，先发“塔菲印象”看看。"
    selected = insights[index - 1]
    if await learning_insights.delete(selected.id, scope.owner_user_id, scope.group_id):
        return f"已删除印象：{selected.content}"
    return "这条印象状态已变化，请重新查看。"


@learning_cmd.handle()
async def handle_learning(_bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(learning_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "印象操作太频繁了，请稍后再试。")
    if not learning_enabled():
        await _finish(event, _CLOSED_MESSAGE)
    try:
        if text == "塔菲印象":
            await _finish(event, await _accepted_reply(event))
        if text == "塔菲观察":
            await _finish(event, await _pending_reply(event))
        if text.startswith("确认印象"):
            await _finish(event, await _confirm(event, text))
        if text.startswith("拒绝印象"):
            await _finish(event, await _reject(event, text))
        if text.startswith("删除印象"):
            await _finish(event, await _delete(event, text))
        await _finish(
            event, "格式：塔菲印象｜塔菲观察｜确认印象 序号｜拒绝印象 序号｜删除印象 序号"
        )
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Learning command failed error=%s", type(exc).__name__)
        await _finish(event, "印象这次没处理好，请稍后再试。")


__all__ = ["handle_learning", "learning_cmd"]

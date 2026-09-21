"""Group-taught expression dictionary commands (V0.4 P4).

Explicit teaching only: ``塔菲学话 <词> :: <含义>`` proposes, a scoped
``确认话术 序号`` accepts, and everything stays reviewable and deletable —
mirroring the M1 impression workflow.
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
from src.services.slang_service import prompt_terms

from ..feature_gate import check_feature, record_event_usage
from .routing import persona_scope

_log = logging.getLogger("taffy.slang_commands")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)
_CLOSED_MESSAGE = "塔菲的学习功能已关闭，管理员可在配置里打开。"

_SEPARATOR = "::"


def _is_slang_request(event: Event) -> bool:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return text in {"话术列表"} or text.startswith(
        ("塔菲学话 ", "确认话术 ", "拒绝话术 ", "删除话术 ", "启用话术 ")
    )


slang_cmd = on_message(rule=Rule(_is_slang_request), priority=7, block=True)


async def _finish(matcher: type, event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await slang_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@slang_cmd.handle()
async def handle_slang(bot: Bot, event: MessageEvent) -> None:
    if not await check_feature(slang_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(slang_cmd, event, "话术操作太频繁了，稍后再试喵。")
    if not learning_enabled():
        await _finish(slang_cmd, event, _CLOSED_MESSAGE)

    scope = persona_scope(event)
    from src.state.runtime import persona_slang

    text = event.get_plaintext().strip()
    try:
        if text == "话术列表":
            await _finish(slang_cmd, event, await _list_reply(scope.group_id, scope.label))
            return

        if text.startswith("塔菲学话 "):
            body = text[len("塔菲学话 ") :].strip()
            parts = [part.strip() for part in body.split(_SEPARATOR, 1)]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                await _finish(
                    slang_cmd,
                    event,
                    "格式是这样喵：塔菲学话 词 :: 含义或用法（例如 塔菲学话 6 :: 形容厉害）",
                )
                return
            entry, created = await persona_slang.teach(
                scope.group_id, created_by=scope.owner_user_id, term=parts[0], meaning=parts[1]
            )
            if not created:
                await _finish(
                    slang_cmd, event, f"“{entry.term}”已经教过啦，状态是 {entry.status}。"
                )
            await _finish(
                slang_cmd,
                event,
                f"记下了“{entry.term}”，等你或管理员发“确认话术”列表序号后才会使用喵。",
            )
            return

        for prefix, status, verb in (
            ("确认话术 ", "accepted", "接受"),
            ("拒绝话术 ", "rejected", "拒绝"),
        ):
            if not text.startswith(prefix):
                continue
            raw = text[len(prefix) :].strip()
            if not raw.isdecimal():
                await _finish(slang_cmd, event, "要给我一个序号，先发“话术列表”看看喵。")
                return
            pending = await persona_slang.pending(scope.group_id)
            index = int(raw)
            if index < 1 or index > len(pending):
                await _finish(slang_cmd, event, "列表里没有这个序号，发“话术列表”再确认一下吧。")
                return
            decided = await persona_slang.decide(
                pending[index - 1].id,
                scope_group_id=scope.group_id,
                created_by=scope.owner_user_id,
                status=status,
            )
            if decided is None:
                await _finish(slang_cmd, event, "这条刚刚被处理过了，晚点再看喵。")
                return
            await _finish(slang_cmd, event, f"{verb}了“{decided.term}”喵。")
            return

        if text.startswith("删除话术 "):
            raw = text[len("删除话术 ") :].strip()
            # 必须与「话术列表」打印的那一份同源（accepted_all，含已停用）。
            # 用 accepted_for_injection（仅启用）会让序号在有停用条目时整体错位，
            # 删掉的本是列表上另一行的词条。
            listing = await persona_slang.accepted_all(scope.group_id, limit=50)
            if not raw.isdecimal() or int(raw) < 1 or int(raw) > len(listing):
                await _finish(slang_cmd, event, "序号无效；删除按“话术列表”里已确认条目的顺序。")
                return
            removed = await persona_slang.delete(
                listing[int(raw) - 1].id,
                scope_group_id=scope.group_id,
                created_by=scope.owner_user_id,
            )
            if removed is None:
                await _finish(slang_cmd, event, "这条已经不在了喵。")
                return
            await _finish(slang_cmd, event, f"删掉了“{removed.term}”喵。")
            return

        if text.startswith("启用话术 "):
            raw = text[len("启用话术 ") :].strip()
            all_accepted = await persona_slang.accepted_all(scope.group_id, limit=50)
            if not raw.isdecimal() or int(raw) < 1 or int(raw) > len(all_accepted):
                await _finish(
                    slang_cmd, event, "序号无效；启用/停用按“话术列表”里已确认条目的顺序。"
                )
                return
            target = all_accepted[int(raw) - 1]
            enabled = await persona_slang.set_enabled(
                target.id,
                enabled=True,
                scope_group_id=scope.group_id,
                created_by=scope.owner_user_id,
            )
            if enabled is None:
                await _finish(slang_cmd, event, "这条不在了，晚点再看喵。")
                return
            await _finish(slang_cmd, event, f"好，“{enabled.term}”重新可以说了喵。")
            return
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Slang command failed error=%s", type(exc).__name__)
        await _finish(slang_cmd, event, "话术这次没处理好，晚点再试喵。")


async def _list_reply(group_id: int, label: str) -> str:
    from src.state.runtime import persona_slang

    pending = await persona_slang.pending(group_id)
    accepted = await persona_slang.accepted_all(group_id, limit=50)
    if not pending and not accepted:
        terms = await prompt_terms(group_id)
        if not terms:
            return (
                "这里还没有教过塔菲任何说法。格式：塔菲学话 词 :: 含义或用法，"
                "确认之后才会在聊天里自然使用。"
            )
    lines = [f"📖 【话术词典·{label}】"]
    if pending:
        lines.append("待确认：")
        for index, entry in enumerate(pending, start=1):
            lines.append(f"  {index}. {entry.term} —— {entry.meaning}")
        lines.append("发送“确认话术 序号”或“拒绝话术 序号”处理。")
    if accepted:
        lines.append("已生效（启用/停用/删除按此序号）：")
        for index, entry in enumerate(accepted, start=1):
            marker = "" if entry.enabled else "（已停用，说“启用话术 序号”恢复）"
            lines.append(f"  {index}. {entry.term} —— {entry.meaning}{marker}")
    lines.append("ℹ️ 只显示当前范围；私聊和每个群彼此隔离，确认后才会在聊天中使用。")
    lines.append("ℹ️ 对塔菲的发言说“不可以/别用XX”也能直接停用对应说法。")
    return "\n".join(lines)

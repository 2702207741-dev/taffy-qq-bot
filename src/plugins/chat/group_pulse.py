"""主动插话（群聊心流）matcher 层：旁听 + 每群 opt-in 命令 + 触发调度。

分层铁律：全部业务在 ``src/services/group_pulse.py``，本文件只做
NoneBot 适配（事件 → 服务调用）。

matcher 优先级链（绝不拦截既有链路）：
- gc@10 被@必回、gk@15 关键词兜底、pc@10 私聊；
- 本模块旁听 matcher **priority=20、block=False**——只在更高优先级
  matcher 放行（未消费）的消息上收集，绝不吞消息。

隐私红线（与 service 层同源，测试断言）：
- 旁听消息只进每群内存环形缓冲（最近 8 条），重启即失；
- 绝不落盘：不写 session_messages、不写 usage_log、不做学习观察素材；
- 磁盘上唯一新增持久化是审计台账 group_pulse_log（不含消息原文）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import NoReturn

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
)
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter

from ..feature_gate import check_feature, record_event_usage
from .routing import COMMAND_PREFIXES, EXACT_COMMANDS

_log = logging.getLogger("taffy.group_pulse_matcher")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)

_PULSE_COMMANDS = frozenset({"塔菲插话开", "塔菲插话关"})

#: 插话触发检查的最小间隔（秒）：旁听消息到达后不立即插话，等一小段
#: 时间窗口内最多检查一次，避免同一条消息触发多次判断。
_TRIGGER_CHECK_INTERVAL = 60.0


def _superusers() -> set[str]:
    configured: object = getattr(get_driver().config, "superusers", set())
    if not isinstance(configured, (set, frozenset, list, tuple)):
        return set()
    return {str(value) for value in configured}


def _is_group_pulse_request(event: Event) -> bool:
    """仅群消息且文本是插话开/关命令（管理员校验在 handler 内做）。"""

    if not isinstance(event, GroupMessageEvent):
        return False
    return event.get_plaintext().strip() in _PULSE_COMMANDS


pulse_cmd = on_message(rule=Rule(_is_group_pulse_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="chat",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await pulse_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


@pulse_cmd.handle()
async def handle_pulse_optin(bot: Bot, event: GroupMessageEvent) -> None:
    """塔菲插话开/关：仅群管理员（SUPERUSERS）可操作，默认全关。"""

    from src.services.group_pulse import pulse_enabled
    from src.state.runtime import group_pulse_log

    message = event.get_plaintext().strip()
    if str(event.user_id) not in _superusers():
        await _finish(event, "只有管理员才能开关插话喵。")
    if not await check_feature(pulse_cmd, event, "chat"):
        return
    if not await _RATE_LIMITER.allow(f"pulse:{event.group_id}"):
        await _finish(event, "操作太频繁了，请稍后再试。")
    try:
        if message == "塔菲插话开":
            await group_pulse_log.set_group_enabled(int(event.group_id), True)
            reply = (
                "已为本群开启塔菲插话（群聊心流）。开启后塔菲可能偶尔接话，仍受冷却与每日上限约束。"
            )
            if not pulse_enabled():
                reply += "\nℹ️ 管理员当前未启用全局开关，开启后会自动生效。"
            await _finish(event, reply)
        if message == "塔菲插话关":
            await group_pulse_log.set_group_enabled(int(event.group_id), False)
            await _finish(event, "已关闭本群塔菲插话，塔菲不会主动接话了。")
        await _finish(event, "格式：塔菲插话开｜塔菲插话关")
    except Exception as exc:
        _log.exception("Group pulse opt-in failed error=%s", type(exc).__name__)
        await _finish(event, "插话开关这次没设置好，请稍后再试。")


# ── 旁听 matcher：priority 20, block=False ─────────────────────


def _is_eavesdrop_candidate(event: Event) -> bool:
    """收集未被 @、非命令的群消息（仅当本群已开启插话时由 handler 判断）。"""

    return isinstance(event, GroupMessageEvent) and not event.is_tome()


def _is_looks_command(text: str) -> bool:
    message = text.strip()
    if not message:
        return False
    if message in EXACT_COMMANDS or message.startswith(tuple(COMMAND_PREFIXES)):
        return True
    if message.startswith(("塔菲插话", "/", "!")):
        return True
    return False


pulse_eavesdrop = on_message(rule=Rule(_is_eavesdrop_candidate), priority=20, block=False)


@pulse_eavesdrop.handle()
async def handle_eavesdrop(bot: Bot, event: GroupMessageEvent) -> None:
    """旁听一条群消息：只喂意愿系统，绝不拦截链路（block=False）。

    隐私红线：此处只更新每群内存环形缓冲与意愿值，任何持久化（写库、
    写 usage_log、写学习观察）都禁止。旁听原文永不落盘。
    """

    from src.services.group_pulse import group_pulse, pulse_enabled

    if not pulse_enabled():
        return
    try:
        from src.state.runtime import group_pulse_log

        if not await group_pulse_log.is_group_enabled(int(event.group_id)):
            return
    except Exception as exc:
        _log.warning(
            "Group pulse opt-in check failed group=%s error=%s", event.group_id, type(exc).__name__
        )
        return

    message = event.get_plaintext().strip()
    if _is_looks_command(message):
        return
    # 表情包/纯图片消息的明文为空：仍计入（按表情包意愿衰减处理）。
    pulse = group_pulse()
    pulse.observe(int(event.group_id), message)
    asyncio.get_running_loop().create_task(_maybe_interject(bot, int(event.group_id)))


_last_trigger_at: dict[int, float] = {}


async def _maybe_interject(bot: Bot, group_id: int) -> None:
    """冷却窗口内限频的插话触发检查（决策/生成/发送全在 service 层）。"""

    from src.services.group_pulse import run_interjection_once

    now = asyncio.get_running_loop().time()
    last = _last_trigger_at.get(group_id, 0.0)
    if now - last < _TRIGGER_CHECK_INTERVAL:
        return
    _last_trigger_at[group_id] = now
    await run_interjection_once(bot, group_id)


__all__ = [
    "handle_eavesdrop",
    "handle_pulse_optin",
    "pulse_cmd",
    "pulse_eavesdrop",
]

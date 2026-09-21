"""主动插话（群聊心流）——零 LLM 意愿公式 + 冷场触发，严格 opt-in。

隐私红线（写死，测试断言同源）：
- 旁听消息只进每群**内存环形缓冲**（最近 8 条），重启即失；
- **绝不落盘**：不写 session_messages、不写 usage_log、不做学习观察素材，
  磁盘上不新增任何聊天原文；
- 唯一持久化的是审计台账 ``group_pulse_log``（群号、触发原因、会话摘要
  哈希、时间），**不存消息原文**。

防骚扰四件套（对齐 M4 proactive 范式）：
1. 全局开关 ``TAFFY_GROUP_PULSE_ENABLED``（默认 false）；
2. 每群显式 opt-in（``塔菲插话开/关``，仅群管理员，默认全关）；
3. 每群冷却 ``TAFFY_GROUP_PULSE_COOLDOWN_SECONDS``（默认 1800 秒）；
4. 每群每日上限 ``TAFFY_GROUP_PULSE_DAILY_CAP``（默认 3 条，持久台账）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random  # noqa: S311 - 插话概率采样非加密用途（防骚扰门禁），可注入种子供测试
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from .config import bounded_env_int, env_bool

logger = logging.getLogger("taffy.group_pulse")

_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

TriggerReason = Literal["willing", "cold_start"]

#: 塔菲相关话题词：出现即大幅提升插话意愿（纯本地匹配，零 LLM）。
_TOPIC_MARKERS = (
    "塔菲",
    "taffy",
    "taffy猫",
    "雏草姬",
    "阿塔",
    "塔菲酱",
)

#: 表情包消息只按 x0.1 计入意愿，算完概率再 x10 回滚（防表情包打残对话意愿）。
_STICKER_WILLING_DECAY = 0.1

#: 每群内存环形缓冲容量（隐私红线：旁听原文只存在于内存）。
_PULSE_BUFFER_SIZE = 8

#: 插话概率 = max((willing − 0.45) × 2, 0)。
_WILLING_THRESHOLD = 0.45
_WILLING_PROBABILITY_GAIN = 2.0

#: 普通群消息对意愿的增量。
_WILLING_PER_MESSAGE = 0.02
#: 塔菲相关话题词增量。
_WILLING_PER_TOPIC = 0.1

#: 意愿上限；衰减按每分钟一档。
_WILLING_MAX = 1.0
#: 空闲线性衰减：每分钟下降这么多（回到 0 需 50 分钟）。
_WILLING_DECAY_PER_MINUTE = 0.02

#: 冷场门：沉默时长 > 群平均消息间隔 × 阈值 且 今日消息 > 30 且 最后一条非 bot。
_COLD_START_SILENCE_RATIO = 3.0
_COLD_START_MIN_DAILY_MESSAGES = 30
#: 平均间隔至少统计多少条最近消息。
_COLD_START_MIN_SAMPLES = 5
_COLD_START_SAMPLE_WINDOW = 20

#: 旁听消息缓存 key 前缀（群上下文用）。
_PULSE_CONTEXT_KEY_PREFIX = "pulse:"


def pulse_enabled() -> bool:
    """全局开关；出厂关闭，管理员显式开启后才可能插话。"""

    return env_bool("TAFFY_GROUP_PULSE_ENABLED", False)


def pulse_cooldown_seconds() -> int:
    return bounded_env_int("TAFFY_GROUP_PULSE_COOLDOWN_SECONDS", 1800, 0, 86_400)


def pulse_daily_cap() -> int:
    return bounded_env_int("TAFFY_GROUP_PULSE_DAILY_CAP", 3, 0, 50)


def _local_day_key(current: int | None = None) -> str:
    local = datetime.fromtimestamp(
        int(time.time() if current is None else current), _MARKET_TIMEZONE
    )
    return local.strftime("%Y%m%d")


@dataclass(slots=True)
class _GroupState:
    """每群纯内存状态；重启即失（旁听内容永不落盘）。"""

    group_id: int
    willing: float = 0.0
    buffer: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_PULSE_BUFFER_SIZE)
    )
    message_times: deque[int] = field(default_factory=lambda: deque(maxlen=200))
    last_message_at: int = 0
    last_bot_at: int = 0
    daily_messages: int = 0
    last_checked_day: str = ""
    last_interjection_at: int = 0


class GroupPulse:
    """每群意愿值、环形缓冲与触发决策（纯内存，零 LLM）。"""

    def __init__(self, *, random_source: random.Random | None = None) -> None:
        self._groups: dict[int, _GroupState] = {}
        # 插话概率采样非加密用途（防骚扰门禁），用可注入 Random 便于
        # 测试固定种子；生产默认实例由系统熵初始化。
        self._rng = random_source or random.Random()

    # ── 每群状态 ─────────────────────────────────────────

    def _state(self, group_id: int) -> _GroupState:
        state = self._groups.get(group_id)
        if state is None:
            state = _GroupState(group_id=group_id)
            self._groups[group_id] = state
        return state

    def _roll_day(self, state: _GroupState, now: int) -> None:
        day = _local_day_key(now)
        if state.last_checked_day != day:
            state.last_checked_day = day
            state.daily_messages = 0

    def _decay(self, state: _GroupState, now: int) -> None:
        """空闲线性衰减（每分钟一档）；若长时间无消息则衰减后再按上限保留。"""

        if state.last_message_at <= 0:
            return
        elapsed_minutes = max(0, (now - state.last_message_at)) / 60.0
        if elapsed_minutes <= 0:
            return
        # 关键：衰减钳到 [0, _WILLING_MAX]——不把意愿一次性打穿为 0，
        # 只做线性下降；连续活跃期间每 60s 的衰减被增量抵消。
        state.willing = min(
            _WILLING_MAX,
            max(0.0, state.willing - _WILLING_DECAY_PER_MINUTE * elapsed_minutes),
        )

    def observe(self, group_id: int, text: str, *, at: int | None = None) -> None:
        """旁听一条群消息，只更新内存（隐私红线：不落盘）。

        ``text`` 仅进入本群环形缓冲与意愿计算，调用方保证该消息
        未被 @、非命令（matcher 层过滤）。
        """

        now = int(time.time() if at is None else at)
        state = self._state(group_id)
        self._roll_day(state, now)
        state.daily_messages += 1
        self._decay(state, now)

        if text:
            state.buffer.append({"role": "user", "content": text[:400]})
        state.message_times.append(now)
        state.last_message_at = now

        if self._is_sticker_only(text):
            # 表情包消息按 ×0.1 权重计入（MaiBot"×0.1 再 ×10 回滚"的
            # 净效果 = 表情包只贡献普通消息十分之一的意愿），防表情包
            # 刷屏打残对话意愿。
            state.willing = min(
                _WILLING_MAX, state.willing + _WILLING_PER_MESSAGE * _STICKER_WILLING_DECAY
            )
            return
        lowered = text.casefold()
        if any(marker in lowered for marker in _TOPIC_MARKERS):
            state.willing = min(_WILLING_MAX, state.willing + _WILLING_PER_TOPIC)
        else:
            state.willing = min(_WILLING_MAX, state.willing + _WILLING_PER_MESSAGE)

    @staticmethod
    def _is_sticker_only(text: str) -> bool:
        """表情包消息 = 纯 image 段（matcher 层以空文本传入）或 [表情] 占位。"""

        return not text.strip() or text.strip().startswith("[表情]")

    def observe_bot_message(self, group_id: int, *, at: int | None = None) -> None:
        """记录 bot 自身发言时间（冷场门：最后一条非 bot 才允许）。"""

        state = self._state(group_id)
        state.last_bot_at = int(time.time() if at is None else at)

    def mark_interjection(self, group_id: int, *, at: int | None = None) -> None:
        state = self._state(group_id)
        state.last_interjection_at = int(time.time() if at is None else at)
        # 插话本身也是发言：重置意愿避免连珠炮。
        state.willing = 0.0
        state.last_bot_at = state.last_interjection_at

    # ── 决策 ─────────────────────────────────────────────

    def willing_for(self, group_id: int, *, at: int | None = None) -> float:
        state = self._groups.get(group_id)
        if state is None:
            return 0.0
        self._decay(state, int(time.time() if at is None else at))
        return state.willing

    def interjection_probability(self, willing: float) -> float:
        """意愿 → 概率：max((willing − 0.45) × 2, 0)，钳制到 [0, 1]。"""

        return min(max((willing - _WILLING_THRESHOLD) * _WILLING_PROBABILITY_GAIN, 0.0), 1.0)

    def average_message_interval(self, group_id: int) -> float | None:
        """最近消息的平均间隔（秒）；样本不足返回 None（冷场门不满足）。"""

        state = self._groups.get(group_id)
        if state is None or len(state.message_times) < _COLD_START_MIN_SAMPLES:
            return None
        times = list(state.message_times)[-_COLD_START_SAMPLE_WINDOW:]
        gaps = [b - a for a, b in zip(times, times[1:], strict=False) if b > a]
        if not gaps:
            return None
        return sum(gaps) / len(gaps)

    def cold_start_allows(self, group_id: int, *, at: int | None = None) -> bool:
        """冷场门四条件：沉默 > 平均间隔×阈值 且 今日消息 >30 且 最后一条非 bot。"""

        now = int(time.time() if at is None else at)
        state = self._groups.get(group_id)
        if state is None or state.last_message_at <= 0:
            return False
        interval = self.average_message_interval(group_id)
        if interval is None or interval <= 0:
            return False
        if now - state.last_message_at <= interval * _COLD_START_SILENCE_RATIO:
            return False
        self._roll_day(state, now)
        if state.daily_messages <= _COLD_START_MIN_DAILY_MESSAGES:
            return False
        if state.last_bot_at >= state.last_message_at:
            return False
        return True

    def recent_context(self, group_id: int) -> list[dict[str, str]]:
        """环形缓冲快照（旁听原文只在此；仅用于生成时注入上下文）。"""

        return [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in self._groups.get(group_id, _GroupState(group_id=group_id)).buffer
        ]

    def buffer_fingerprint(self, group_id: int) -> str:
        """会话摘要哈希：只哈希，不落原文（审计与去重用）。"""

        payload = "|".join(
            str(item.get("content", ""))
            for item in self._groups.get(group_id, _GroupState(group_id=group_id)).buffer
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_pulse = GroupPulse()


def group_pulse() -> GroupPulse:
    return _pulse


# ── 触发决策（含防骚扰四件套） ─────────────────────────────


async def _today_interjections(group_id: int, day_key: str, now: int) -> int:
    """查询本群今日已插话条数（持久台账，供每日上限判定）。"""

    try:
        from src.state.runtime import group_pulse_log

        return await group_pulse_log.count_for_group(group_id, day_key, now)
    except Exception as exc:
        logger.warning(
            "Group pulse ledger read failed group=%s error=%s", group_id, type(exc).__name__
        )
        return 0


async def should_interject(group_id: int, *, at: int | None = None) -> TriggerReason | None:
    """防骚扰四件套全通过才返回触发原因；否则 None。"""

    if not pulse_enabled():
        return None
    try:
        from src.state.runtime import group_pulse_log

        if not await group_pulse_log.is_group_enabled(group_id):
            return None
    except Exception as exc:
        logger.warning(
            "Group pulse opt-in check failed group=%s error=%s", group_id, type(exc).__name__
        )
        return None

    now = int(time.time() if at is None else at)
    pulse = group_pulse()
    state = pulse._state(group_id)  # noqa: SLF001 - same-module state helper
    cooldown = pulse_cooldown_seconds()
    if cooldown > 0 and now - state.last_interjection_at < cooldown:
        return None
    day_key = _local_day_key(now)
    cap = pulse_daily_cap()
    if cap > 0 and await _today_interjections(group_id, day_key, now) >= cap:
        return None

    willing = pulse.willing_for(group_id, at=now)
    probability = pulse.interjection_probability(willing)
    if pulse._rng.random() < probability:  # noqa: SLF001 - same-module
        return "willing"
    if pulse.cold_start_allows(group_id, at=now):
        return "cold_start"
    return None


async def record_interjection(group_id: int, reason: TriggerReason, fingerprint: str) -> None:
    """插话成功后写审计台账（不存原文）；失败只记日志，不影响主流程。"""

    try:
        from src.state.runtime import group_pulse_log

        await group_pulse_log.record(group_id, reason, fingerprint)
    except Exception as exc:
        logger.warning(
            "Group pulse ledger write failed group=%s error=%s", group_id, type(exc).__name__
        )


async def generate_interjection(group_id: int) -> str | None:
    """走现有人格管线生成插话；失败静默放弃（不计入上限）。

    使用独立空 session key（``pulse:{group_id}``）——chat() 对非空 key
    会 append_turn 落盘，主动插话绝不能把旁听原文写进 session_messages；
    以空 key 调用只做一次无状态生成，不产生任何持久化。
    """

    try:
        # chat_utils 在插件根（src/plugins/chat_utils.py），不在 chat 包内；
        # 错误路径会让每次生成都 ImportError → 静默放弃（实测踩过的坑）。
        from src.plugins.chat_utils import deepseek_chat as _request

        context = _pulse.recent_context(group_id)
        if not context:
            return None
        scene = "\n".join(f"{item['role']}: {item['content']}" for item in context)
        message = (
            "（群聊安静了一会儿，你自然地接一句话，简短、贴合上面的话题，"
            "不要 @ 任何人，不要用任何命令格式，也不要开启新话题）\n\n" + scene
        )
        reply = await _request(
            message,
            session_id="",
            persona_scope=None,
            # 插话是社交行为：必须跳过事实意图路由，否则上下文里的
            # "行情/入手"等词会被 detect_fact_intents 截胡成行情引导文案
            # （实测踩过的坑）。
            preverified_context=True,
        )
        return reply.strip() or None
    except Exception as exc:
        logger.info("Group pulse generation dropped error=%s", type(exc).__name__)
        return None


async def run_interjection_once(bot: object, group_id: int, *, at: int | None = None) -> bool:
    """一次完整插话流程：决策 → 生成 → 发送 → 台账。返回是否发出。"""

    reason = await should_interject(group_id, at=at)
    if reason is None:
        return False
    reply = await generate_interjection(group_id)
    if not reply:
        return False
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    try:
        async with asyncio.timeout(20):
            await bot.send_group_msg(  # type: ignore[attr-defined]
                group_id=group_id,
                message=Message(MessageSegment.text(reply)),
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Group pulse send failed group=%s error=%s", group_id, type(exc).__name__)
        return False
    now = int(time.time() if at is None else at)
    _pulse.mark_interjection(group_id, at=now)
    await record_interjection(group_id, reason, _pulse.buffer_fingerprint(group_id))
    logger.info("Group pulse interjected group=%s reason=%s", group_id, reason)
    return True


__all__ = [
    "TriggerReason",
    "cold_start_allows",
    "generate_interjection",
    "group_pulse",
    "interjection_probability",
    "observe_bot_message",
    "observe_group_message",
    "pulse_cooldown_seconds",
    "pulse_daily_cap",
    "pulse_enabled",
    "recent_context",
    "record_interjection",
    "run_interjection_once",
    "should_interject",
    "willing_for",
]


def observe_group_message(group_id: int, text: str, *, at: int | None = None) -> None:
    """公开旁听入口（matcher 层调用）；同 ``GroupPulse.observe``。"""

    _pulse.observe(group_id, text, at=at)


def willing_for(group_id: int) -> float:
    return _pulse.willing_for(group_id)


def recent_context(group_id: int) -> list[dict[str, str]]:
    return _pulse.recent_context(group_id)


def interjection_probability(willing: float) -> float:
    return _pulse.interjection_probability(willing)


def cold_start_allows(group_id: int, *, at: int | None = None) -> bool:
    return _pulse.cold_start_allows(group_id, at=at)


def observe_bot_message(group_id: int, *, at: int | None = None) -> None:
    _pulse.observe_bot_message(group_id, at=at)

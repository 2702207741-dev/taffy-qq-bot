"""主动插话（群聊心流）测试：隐私红线 / 意愿公式 / 冷场门 / 防骚扰四件套。

隐私红线断言（最重要）：旁听后 SQLite 零新增原文行——session_messages、
usage_log、learning_insights 等表中不出现任何旁听文本；唯一新增持久化是
group_pulse_log 审计台账，且其中只有群号/原因/指纹哈希，绝无消息原文。
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.services import group_pulse as gp
from src.state.group_pulse_repository import GroupPulseLogRepository, row_kind


def _repo(tmp_path: Path, name: str = "pulse.db") -> GroupPulseLogRepository:
    repository = GroupPulseLogRepository(StateDatabase(tmp_path / name))
    return repository


# ─── 意愿公式边界 ─────────────────────────────────────────────


def test_interjection_probability_threshold_boundary() -> None:
    pulse = gp.GroupPulse()
    assert pulse.interjection_probability(0.45) == 0.0
    assert pulse.interjection_probability(0.44) == 0.0
    assert pulse.interjection_probability(0.46) == pytest.approx(0.02)
    assert pulse.interjection_probability(0.95) == pytest.approx(1.0)
    assert pulse.interjection_probability(1.5) == pytest.approx(1.0)


def test_willing_increments_and_clamps() -> None:
    pulse = gp.GroupPulse()
    at = 1_700_000_000
    for _ in range(60):
        pulse.observe(1001, "普通消息", at=at)  # 同一时刻，避免衰减干扰
    assert pulse.willing_for(1001, at=at) == pytest.approx(1.0)  # 封顶 1.0


def test_topic_marker_boosts_willingness() -> None:
    pulse = gp.GroupPulse()
    at = 1_700_000_000
    pulse.observe(1001, "今天大盘怎么样", at=at)
    pulse.observe(1001, "塔菲你知道这个箱子吗", at=at)
    # 话题词 +0.1 vs 普通消息 +0.02
    assert pulse.willing_for(1001, at=at) == pytest.approx(0.02 + 0.1)


def test_sticker_message_uses_rollback_trick() -> None:
    """表情包 ×0.1 后再 ×10 回滚：不因表情包打残对话意愿。"""

    pulse = gp.GroupPulse()
    at = 1_700_000_000
    pulse.observe(1001, "你好", at=at)
    pulse.observe(1001, "", at=at)
    pulse.observe(1001, "", at=at)
    # 0.02 + 0.002 + 0.002 = 0.024（表情包只贡献十分之一意愿）
    assert pulse.willing_for(1001, at=at) == pytest.approx(0.02 + 0.002 + 0.002)


def test_idle_linear_decay() -> None:
    pulse = gp.GroupPulse()
    at = 1_700_000_000
    pulse.observe(1001, "聊起来", at=at)
    assert pulse.willing_for(1001, at=at) == pytest.approx(0.02)
    # 5 分钟衰减 0.1 → 归零
    assert pulse.willing_for(1001, at=at + 5 * 60) == 0.0


def test_decay_reaches_zero_after_long_idle() -> None:
    pulse = gp.GroupPulse()
    at = 1_700_000_000
    pulse.observe(1001, "hi", at=at)
    pulse._state(1001).willing = 0.5
    # 30 分钟 × 0.02/分钟 = 0.6 > 0.5 → 归零
    assert pulse.willing_for(1001, at=at + 30 * 60) == 0.0


# ─── 冷场门四条件 ──────────────────────────────────────────────


def _cold_group(
    pulse: gp.GroupPulse,
    group_id: int,
    *,
    daily_messages: int = 40,
    gap: int = 60,
    silence: int = 600,
    last_bot: bool = False,
    at: int = 1_700_000_000,
) -> None:
    state = pulse._state(group_id)
    times = [at - silence - gap * (daily_messages - 1 - i) for i in range(daily_messages)]
    for t in times:
        state.message_times.append(t)
    state.daily_messages = daily_messages
    state.last_checked_day = gp._local_day_key(at)  # noqa: SLF001 - 直接对齐当天，防 _roll_day 归零
    state.last_message_at = at - silence
    state.last_bot_at = at - silence + 1 if last_bot else 0


def _seed_cold_group(pulse: gp.GroupPulse, group_id: int) -> None:
    """把某群冷场条件全部满足（含消息间隔样本），供 cold_start 断言。"""

    _cold_group(pulse, group_id, daily_messages=40, gap=60, silence=600)


def test_cold_start_all_four_conditions_met() -> None:
    pulse = gp.GroupPulse()
    group_id = 2001
    _cold_group(pulse, group_id, daily_messages=40, gap=60, silence=600)
    assert pulse.average_message_interval(group_id) == pytest.approx(60.0)
    assert pulse.cold_start_allows(group_id, at=1_700_000_000) is True


def test_cold_start_requires_silence_above_ratio() -> None:
    pulse = gp.GroupPulse()
    group_id = 2001
    # 沉默 120s < 60×3=180s → 不允许
    _cold_group(pulse, group_id, daily_messages=40, gap=60, silence=120)
    assert pulse.cold_start_allows(group_id, at=1_700_000_000) is False


def test_cold_start_requires_daily_messages_over_thirty() -> None:
    pulse = gp.GroupPulse()
    group_id = 2001
    _cold_group(pulse, group_id, daily_messages=30, gap=60, silence=600)
    assert pulse.cold_start_allows(group_id, at=1_700_000_000) is False


def test_cold_start_rejects_when_last_message_is_bot() -> None:
    pulse = gp.GroupPulse()
    group_id = 2001
    _cold_group(pulse, group_id, daily_messages=40, gap=60, silence=600, last_bot=True)
    assert pulse.cold_start_allows(group_id, at=1_700_000_000) is False


def test_cold_start_rejects_too_few_samples() -> None:
    pulse = gp.GroupPulse()
    group_id = 2001
    _cold_group(pulse, group_id, daily_messages=3, gap=60, silence=600)
    assert pulse.cold_start_allows(group_id, at=1_700_000_000) is False


# ─── 隐私红线：旁听零落盘 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_eavesdrop_writes_nothing_to_disk(tmp_path: Path) -> None:
    """旁听后 SQLite 零新增原文行（隐私红线核心断言）。

    - 台账中不得出现旁听文本（唯一持久化是 group_pulse_log）；
    - 任何表都不应包含旁听原文。
    """

    from src.state.group_pulse_repository import GroupPulseLogRepository

    repository = GroupPulseLogRepository(StateDatabase(tmp_path / "privacy.db"))
    await repository.initialize()

    pulse = gp.GroupPulse()
    group_id = 3001
    at = 1_700_000_000
    secret_text = "旁听这条绝对不能落盘喵喵喵ABC123"
    for i in range(12):
        pulse.observe(group_id, f"{secret_text}-{i}", at=at + i * 5)

    # 唯一持久化：opt-in + 审计，且不含原文
    await repository.set_group_enabled(group_id, True)
    await repository.record(group_id, "willing", pulse.buffer_fingerprint(group_id))
    rows = await repository.recent(group_id)
    assert len(rows) == 2  # opt-in 行 + 审计行
    for row in rows:
        assert secret_text not in (row.fingerprint or "")

    # 全库扫描：任何表都没有原文
    tables = [
        row["name"]
        for row in await repository.database.read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            (),
        )
    ]
    for table in tables:
        info = await repository.database.read(f"PRAGMA table_info({table})", ())
        text_columns = [str(c["name"]) for c in info if "TEXT" in str(c["type"]).upper()]
        for column in text_columns:
            rows_found = await repository.database.read(
                f"SELECT {column} AS v FROM {table} LIMIT 500", ()
            )
            for row in rows_found:
                assert secret_text not in str(row["v"])


@pytest.mark.asyncio
async def test_ledger_never_stores_message_text(tmp_path: Path) -> None:
    """台账约束：fingerprint 定长 64 且来自哈希，reason 枚举受限。"""

    repository = GroupPulseLogRepository(StateDatabase(tmp_path / "ledger.db"))
    await repository.initialize()
    await repository.record(3001, "cold_start", "a" * 64)
    rows = await repository.recent(3001)
    assert rows[0].reason == "cold_start"
    assert rows[0].fingerprint == "a" * 64


# ─── 防骚扰四件套 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_switch_off_blocks_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "pulse_enabled", lambda: False)
    reason = await gp.should_interject(4001, at=1_700_000_000)
    assert reason is None


@pytest.mark.asyncio
async def test_group_optin_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)
    monkeypatch.setattr(gp, "pulse_enabled", lambda: True)

    pulse = gp.group_pulse()
    pulse._state(4002).willing = 0.95
    reason = await gp.should_interject(4002, at=1_700_000_000)
    assert reason is None

    # 已开启 + 意愿 0.95 + 固定随机种子 → 意愿路径应放行
    pulse._state(4001).willing = 0.95
    pulse._state(4001).last_interjection_at = 0
    pulse._rng = random.Random(0)
    reason = await gp.should_interject(4001, at=1_700_000_000)
    assert reason == "willing"
    # 但随机种子取大数时意愿路径可能不过；冷场门四条件齐备时必然触发 cold_start
    pulse._state(4001).willing = 0.0
    pulse._state(4001).last_interjection_at = 0
    _seed_cold_group(pulse, 4001)
    reason = await gp.should_interject(4001, at=1_700_000_000)
    assert reason == "cold_start"


# ─── opt-in 开关与审计行共表：最新一行只对「开关行」成立 ──────────────


@pytest.mark.asyncio
async def test_optin_survives_interjection_audit_rows(tmp_path: Path) -> None:
    """一次插话之后 opt-in 必须仍然是开着的。

    ``group_pulse_log`` 同时承载开关行（``reason IS NULL``，``enabled`` 0/1）
    与审计行（``reason NOT NULL``，``enabled`` 恒 0）。「每组最新一行胜出」
    只对开关行成立：否则插话一次之后最新行变成审计行，群被永久静默退订。
    """

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)
    assert await repository.is_group_enabled(4001) is True

    await repository.record(4001, "willing", "a" * 64)

    assert await repository.is_group_enabled(4001) is True
    assert await repository.enabled_groups() == [4001]


@pytest.mark.asyncio
async def test_audit_rows_never_opt_a_group_in(tmp_path: Path) -> None:
    """反向对照：审计行不得把从未订阅的群变成已订阅（防过度修正）。"""

    repository = _repo(tmp_path)
    await repository.initialize()

    await repository.record(4002, "cold_start", "b" * 64)

    assert await repository.is_group_enabled(4002) is False
    assert await repository.enabled_groups() == []


@pytest.mark.asyncio
async def test_service_ledger_write_keeps_group_subscribed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实调用链：走 service 的 record_interjection（group_pulse.py:335）后 opt-in 仍在。"""

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4004, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)

    await gp.record_interjection(4004, "willing", "d" * 64)

    assert await repository.is_group_enabled(4004) is True
    assert await repository.enabled_groups() == [4004]


@pytest.mark.asyncio
async def test_optout_still_wins_over_older_audit_rows(tmp_path: Path) -> None:
    """反向对照：显式退订必须压过更早的审计行。"""

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4003, True)
    await repository.record(4003, "willing", "c" * 64)
    await repository.set_group_enabled(4003, False)

    assert await repository.is_group_enabled(4003) is False
    assert await repository.enabled_groups() == []


@pytest.mark.asyncio
async def test_cooldown_blocks_second_interjection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)
    monkeypatch.setattr(gp, "pulse_enabled", lambda: True)
    monkeypatch.setattr(gp, "pulse_cooldown_seconds", lambda: 1800)

    pulse = gp.group_pulse()
    pulse._state(4001).willing = 0.95
    pulse._state(4001).last_interjection_at = 0
    pulse._rng = random.Random(0)
    at = 1_700_000_000
    reason = await gp.should_interject(4001, at=at)
    # 全局单例可能残留冷场样本 → 触发原因可能是 willing 或 cold_start，二者都算通过
    assert reason in ("willing", "cold_start")
    pulse.mark_interjection(4001, at=at)
    # 冷却内第二次：拒绝
    reason2 = await gp.should_interject(4001, at=at + 60)
    assert reason2 is None


@pytest.mark.asyncio
async def test_daily_cap_blocks_after_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)
    monkeypatch.setattr(gp, "pulse_enabled", lambda: True)
    monkeypatch.setattr(gp, "pulse_daily_cap", lambda: 3)

    pulse = gp.group_pulse()
    pulse._state(4001).willing = 0.95
    pulse._state(4001).last_interjection_at = 0
    pulse._rng = random.Random(0)
    at = 1_700_000_000
    # 已写 3 条审计 → 上限满
    for i in range(3):
        await repository.record(4001, "willing", "a" * 64, at=at + i)
    reason = await gp.should_interject(4001, at=at + 100)
    assert reason is None


@pytest.mark.asyncio
async def test_generation_failure_silently_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成失败静默放弃：不计入上限、不写台账、不抛异常。"""

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)
    monkeypatch.setattr(gp, "pulse_enabled", lambda: True)
    monkeypatch.setattr(gp, "pulse_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(gp, "generate_interjection", lambda group_id: _failed_generation(group_id))

    class FakeBot:
        async def send_group_msg(self, **kwargs) -> None:  # pragma: no cover
            raise AssertionError("不应发送")

    pulse = gp.group_pulse()
    pulse._state(4001).willing = 0.95
    pulse._state(4001).last_interjection_at = 0
    pulse._rng = random.Random(0)
    sent = await gp.run_interjection_once(FakeBot(), 4001, at=1_700_000_000)
    assert sent is False
    # 台账仍是 1 行（只有 opt-in），无审计行
    rows = await repository.recent(4001)
    assert len(rows) == 1


async def _failed_generation(group_id: int) -> str | None:
    return None


async def _async_text(value: str) -> str:
    return value


# ─── module 级包装函数与完整插话链路 ─────────────────────────


def test_module_level_wrappers_delegate_to_singleton() -> None:
    """observe/willing/recent_context/probability/cold_start 模块级包装。"""

    import time as time_mod

    now = int(time_mod.time())
    gp.observe_group_message(5001, "塔菲在吗", at=now)
    gp.observe_bot_message(5001, at=now)
    assert gp.willing_for(5001) > 0
    assert len(gp.recent_context(5001)) == 1
    assert gp.recent_context(5001)[0]["content"] == "塔菲在吗"
    assert gp.interjection_probability(0.95) == pytest.approx(1.0)
    assert gp.cold_start_allows(9999, at=now) is False  # 无状态群


@pytest.mark.asyncio
async def test_run_interjection_once_full_success_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整链路：决策 → 生成 → 发送 → 台账 → 标记冷却。"""

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(6001, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)
    monkeypatch.setattr(gp, "pulse_enabled", lambda: True)
    monkeypatch.setattr(gp, "pulse_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(gp, "generate_interjection", lambda group_id: _async_text("自然的接话"))

    sent_calls: list[int] = []

    class FakeBot:
        async def send_group_msg(self, *, group_id: int, message: object) -> None:
            sent_calls.append(group_id)

    pulse = gp.group_pulse()
    pulse.observe(6001, "今天好安静", at=1_700_000_000)
    pulse._state(6001).willing = 0.95
    pulse._state(6001).last_interjection_at = 0
    pulse._rng = random.Random(0)
    sent = await gp.run_interjection_once(FakeBot(), 6001, at=1_700_000_000)
    assert sent is True
    assert sent_calls == [6001]
    # 台账：opt-in 1 行 + 审计 1 行
    rows = await repository.recent(6001)
    assert len(rows) == 2
    assert rows[0].reason in ("willing", "cold_start")
    # 插话后冷却已标记
    assert pulse._state(6001).last_interjection_at > 0


@pytest.mark.asyncio
async def test_run_interjection_once_send_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发送失败：返回 False 且不写审计（不占用每日上限）。"""

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(6002, True)
    monkeypatch.setattr("src.state.runtime.group_pulse_log", repository)
    monkeypatch.setattr(gp, "pulse_enabled", lambda: True)
    monkeypatch.setattr(gp, "pulse_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(gp, "generate_interjection", lambda group_id: _async_text("接话"))

    class FailingBot:
        async def send_group_msg(self, **kwargs) -> None:
            raise RuntimeError("network down")

    pulse = gp.group_pulse()
    pulse.observe(6002, "今天好安静", at=1_700_000_000)
    pulse._state(6002).willing = 0.95
    pulse._state(6002).last_interjection_at = 0
    pulse._rng = random.Random(0)
    sent = await gp.run_interjection_once(FailingBot(), 6002, at=1_700_000_000)
    assert sent is False
    rows = await repository.recent(6002)
    assert len(rows) == 1  # 只有 opt-in 行


# ─── matcher 层：block=False 不影响既有链路 ─────────────────────


def test_eavesdrop_matcher_priority_and_block() -> None:
    """旁听 matcher priority=20、block=False：绝不拦截 gc@10/gk@15。"""

    try:
        import nonebot

        nonebot.get_driver()
    except ValueError:
        import nonebot

        nonebot.init()

    from src.plugins.chat import gc, gk, group_pulse, pc

    matcher = group_pulse.pulse_eavesdrop
    assert matcher.priority == 20
    assert matcher.block is False
    # 既有关键 matcher 优先级低于旁听，且会先消费（block=True）
    assert gc.priority == 10 and gc.block is True
    assert gk.priority == 15 and gk.block is True
    assert pc.priority == 10 and pc.block is True


def test_eavesdrop_candidate_excludes_tome_and_commands() -> None:
    from src.plugins.chat.group_pulse import _is_looks_command

    assert _is_looks_command("大盘") is True
    assert _is_looks_command("开箱 AK") is True
    assert _is_looks_command("塔菲插话开") is True
    assert _is_looks_command("/help") is True
    assert _is_looks_command("今天天气不错") is False
    assert _is_looks_command("") is False


# ─── v18→v19 迁移往返 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrate_18_to_19_idempotent(tmp_path: Path) -> None:
    """v18→v19 迁移幂等可重跑；表结构/索引一次到位。"""

    from src.core.database import SCHEMA_VERSION

    database = StateDatabase(tmp_path / "v19.db", allow_schema_migrations=True)
    await database.initialize()
    await database.initialize()  # 重跑幂等

    rows = await database.read(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='group_pulse_log'",
        (),
    )
    assert rows, "group_pulse_log 表必须存在"
    index_rows = await database.read(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_group_pulse_log_group_day'",
        (),
    )
    assert index_rows
    version = await database.read("PRAGMA user_version", ())
    assert int(version[0]["user_version"]) == SCHEMA_VERSION == 19


async def _wiring_probe_db() -> StateDatabase:
    import tempfile

    db = StateDatabase(Path(tempfile.mkdtemp(prefix="pulse-wiring-")) / "state.db")
    await db.initialize()
    return db


async def test_generate_interjection_reaches_real_chat_layer(monkeypatch):
    """接线契约：generate_interjection 必须调用真实的 chat 层。

    回归背景：曾把 chat_utils 导入路径写错（src.plugins.chat.chat_utils
    不存在），每次生成 ImportError 被静默吞掉，插话功能在生产从未发过话
    ——而管线测试全程 mock 掉 generate_interjection，没能暴露。本测试
    mock 到真实 chat 层边界，钉住导入路径与调用方式。
    """

    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()

    db = await _wiring_probe_db()
    repository = GroupPulseLogRepository(db)
    await repository.initialize()

    pulse = gp.group_pulse()
    pulse.observe(777001, "周末有人一起打 CS 吗", at=1_700_000_000)

    captured: dict = {}

    async def fake_deepseek_chat(message, session_id="", *, preverified_context=False, **kwargs):
        captured["message"] = message
        captured["session_id"] = session_id
        captured["preverified_context"] = preverified_context
        return "自然接话喵。"

    monkeypatch.setattr("src.plugins.chat_utils.deepseek_chat", fake_deepseek_chat)
    reply = await gp.generate_interjection(777001)

    assert reply == "自然接话喵。", (
        "generate_interjection 必须能到达真实 chat 层（导入路径回归护栏）"
    )
    assert captured["session_id"] == "", "空 session key：旁听原文不得落盘"
    assert captured["preverified_context"] is True, (
        "插话必须跳过事实意图路由，否则上下文里的行情词会被截胡成引导文案"
    )


# ─── M-109：活动流按行-kind 标注，计数按类分开 ────────────────────────


@pytest.mark.asyncio
async def test_recent_labels_each_row_kind(tmp_path: Path) -> None:
    """每行必须带 ``kind``，且**来自共享的 ``row_kind``** 而非调用方再判一次。

    这一条针对 M-107/M-109 的共同根因：行-kind 的判据散落在各调用方。
    """

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)  # switch 行
    await repository.record(4001, "willing", "a" * 64)  # interjection 行

    rows = await repository.recent(4001)

    assert [row.kind for row in rows] == ["interjection", "switch"], "新→旧"
    # 钉住来源：DTO 的 kind 必须等于共享函数的判定
    assert all(row.kind == row_kind(row.reason) for row in rows)


@pytest.mark.asyncio
async def test_counts_split_interjections_from_switches(tmp_path: Path) -> None:
    """面板条数不得把每次 opt-in 开关操作算成插话（M-109 的直接症状）。"""

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(4001, True)
    await repository.record(4001, "willing", "a" * 64)
    await repository.record(4001, "cold_start", "b" * 64)
    await repository.set_group_enabled(4002, True)
    await repository.set_group_enabled(4002, False)  # 一次开、一次关

    counts = await repository.counts()

    assert counts["interjection_count"] == 2
    assert counts["switch_count"] == 3
    # 旧口径 `len(recent())` 会把这 5 行全部算成「插话条数」
    assert len(await repository.recent()) == 5
    assert counts["interjection_count"] != len(await repository.recent())

    # 分群口径
    assert await repository.counts(group_id=4001) == {
        "interjection_count": 2,
        "switch_count": 1,
    }
    assert await repository.counts(group_id=4002) == {
        "interjection_count": 0,
        "switch_count": 2,
    }
    # 与既有语义口径一致：日上限只数审计行（窗口按 now 的本地日起算，
    # 故必须传真实当前时间；传一个远期时间戳会让窗口落空、结果恒为 0）
    now = int(time.time())
    assert await repository.count_for_group(4001, "day", now) == 2
    assert await repository.count_for_group(4002, "day", now) == 0


@pytest.mark.asyncio
async def test_row_kind_matches_an_independent_literal_sql_query(tmp_path: Path) -> None:
    """M-109 的**独立判据**：SQL 谓词与 Python 判据不得各写一遍而静默失配。

    为什么需要它：``assert row.kind == row_kind(row.reason)`` 是**同义反复** ——
    ``kind`` 本来就是那个函数产出的，它只挡得住「有人改成硬编码 kind」。而真正的
    风险是两条规则各写一遍（``SWITCH_ROW_PREDICATE = "reason IS NULL"`` 与
    ``row_kind(): reason is None``）将来单侧漂移。

    因此本用例**不引用**这两个常量，而是自己写死字面量 SQL 求出「应被标为
    switch / interjection 的 id 集合」，再与 DTO 的 ``kind`` 逐一对表 ——
    任一侧单独改动都会红。
    """

    repository = _repo(tmp_path)
    await repository.initialize()
    await repository.set_group_enabled(5001, True)
    await repository.record(5001, "willing", "a" * 64)
    await repository.set_group_enabled(5001, False)
    await repository.record(5002, "cold_start", "b" * 64)

    literal_switch_ids = {
        int(row["id"])
        for row in await repository.database.read(
            "SELECT id FROM group_pulse_log WHERE reason IS NULL"
        )
    }
    literal_audit_ids = {
        int(row["id"])
        for row in await repository.database.read(
            "SELECT id FROM group_pulse_log WHERE reason IS NOT NULL"
        )
    }
    assert literal_switch_ids and literal_audit_ids, "两类行都必须有样本"

    by_kind: dict[str, set[int]] = {"switch": set(), "interjection": set()}
    for row in await repository.recent():
        by_kind[row.kind].add(row.id)

    assert by_kind["switch"] == literal_switch_ids
    assert by_kind["interjection"] == literal_audit_ids

    # counts() 的两列 SUM(CASE WHEN <谓词常量> …) 引用的是**同一组常量**，
    # 漂移风险一模一样 —— 这里用上面的字面量集合把它一并闭合。
    counts = await repository.counts()
    assert counts["switch_count"] == len(literal_switch_ids)
    assert counts["interjection_count"] == len(literal_audit_ids)

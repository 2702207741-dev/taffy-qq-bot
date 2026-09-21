"""手动广播的投递计数（M-24）：``total`` 必须是「应投递数」而非「已尝试数」。

越过总时限后，未被尝试的群原本被静默排除在 ``results`` 之外，而
``total = len(results)`` —— 一次跳过若干群的运行因此显示成一个**更小但看起来
完整**的成功数，面板还会把它算成绿灯。

本文件用 ``tmp_path`` 上的真实 ``BroadcastRepository``，只 patch Bot 与
「总时限」这一个注入点。

语义精确性说明（重要，别把两者混为一谈）：``unconfirmed`` 的准确定义是
**「没有记录到终态结果」**，而不是「从未联系过」。当并发槽位被占满时，
拿住槽位的那几个群的 ``send_group_msg`` 已经被调用，但任务在写入结果前被
取消 —— 它们同样计入 ``unconfirmed``。运维上两者都需要人工重投，所以这个
计数是对的；但它不等于「发送 spy 计数为 0」。
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from unittest.mock import patch

import nonebot
import pytest

from src.core.database import StateDatabase
from src.state.broadcast_repository import BroadcastRepository


def _admin_api():
    """Import ``src.plugins.admin_api``, ensuring NoneBot is initialised first.

    Done **lazily, not at module import**, on purpose: a module-level
    ``nonebot.init`` re-configures global NoneBot state during *collection*, which
    changed behaviour for every other test module depending on import order — in
    this batch it turned a ~6-minute full-suite run into a 15-minute one that had
    to be killed.  Confining the side effect to this module's own tests also
    matches the existing ``tests/test_admin_api.py`` pattern (init in
    ``setUpClass``, not at import).
    """

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=(), driver="~fastapi")
    return importlib.import_module("src.plugins.admin_api")


GROUPS = [1000, 1001, 1002, 1003, 1004, 1005, 1006]
#: 唯一一个在时限内完成投递的群（保证至少有一条成功记录）。
FAST_GROUP = 1000
#: 注入的总时限：足够让 FAST_GROUP 走完「发送 + 落账」，其余群必然被取消。
INJECTED_DEADLINE = 0.5


class _Bot:
    """发送 spy：除 ``fast_group`` 外一律阻塞，直到测试结束都不放行。"""

    def __init__(self, fast_group: int) -> None:
        self.fast_group = fast_group
        self.sent: list[int] = []
        self._blocker = asyncio.Event()

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.sent.append(group_id)
        if group_id != self.fast_group:
            await self._blocker.wait()


async def _repository(tmp_path: Path) -> BroadcastRepository:
    repository = BroadcastRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    return repository


@pytest.mark.asyncio
async def test_unconfirmed_groups_are_reported_and_stay_claimable(tmp_path: Path) -> None:
    """未尝试的群必须被计数、不出现在 results、且仍可被后续领取。"""

    admin_api = _admin_api()
    repository = await _repository(tmp_path)
    bot = _Bot(FAST_GROUP)

    with (
        patch.object(admin_api, "broadcasts", repository),
        patch.object(admin_api, "_current_bot", lambda: bot),
        patch.object(admin_api, "_broadcast_total_deadline_seconds", lambda: INJECTED_DEADLINE),
        # 并发度降到 1：这样「拿住槽位被取消」与「根本没轮上」两类才是可区分的
        patch.object(admin_api, "_BROADCAST_SEND_CONCURRENCY", 1),
    ):
        # 注入点若被改成「导入期早绑定」，这里会在 10s 内失败，而不是挂 300s
        async with asyncio.timeout(10):
            body = await admin_api.admin_broadcast_send(
                group_ids=list(GROUPS), payload_text="hello"
            )

    # ① total 是应投递数，不是已尝试数。
    #    注意：`total == len(results) + unconfirmed` 按构造恒真（同义反复），
    #    它只作**回归护栏**防止有人把 total 改回 len(results)；真正承重的是 ②③。
    assert body["total"] == len(GROUPS)
    assert body["total"] == len(body["results"]) + body["unconfirmed"]
    assert body["unconfirmed"] == len(GROUPS) - 1
    assert body["succeeded"] == 1

    attempted_ids = {row["group_id"] for row in body["results"]}
    unconfirmed_ids = set(GROUPS) - attempted_ids
    assert attempted_ids == {FAST_GROUP}
    assert len(unconfirmed_ids) == body["unconfirmed"]

    # ② 没有轮上槽位的群，发送 spy 必须为 0（真正「未被尝试」的那一批）
    never_sent = {gid for gid in GROUPS if bot.sent.count(gid) == 0}
    assert never_sent, "并发度为 1 时必须存在根本没轮上的群"
    assert never_sent <= unconfirmed_ids
    # 拿住槽位后被取消的那个群：已联系过，但仍计入 unconfirmed（语义见模块 docstring）
    contacted_but_cancelled = unconfirmed_ids - never_sent
    assert contacted_but_cancelled == {1001}

    # ③ 运维后果：未尝试的群没有终态，租约过期后仍可被后续领取
    rows = {
        row.group_id: row.status for row in await repository.list(schedule_key=body["schedule_key"])
    }
    for gid in unconfirmed_ids:
        assert rows[gid] not in {"delivered", "failed"}, f"group {gid} 不应有终态"
    assert rows[FAST_GROUP] == "delivered"

    later = await repository.claim_due(lease_token="later-token", lease_seconds=600, now=10**10)
    reclaimed = {delivery.group_id for delivery in later}
    assert unconfirmed_ids <= reclaimed
    assert FAST_GROUP not in reclaimed, "已投递的群不得被重新领取"


@pytest.mark.asyncio
async def test_default_deadline_is_unchanged() -> None:
    """注入点未生效时，默认值与今天逐位一致（B 要求的第 1 条断言）。"""

    admin_api = _admin_api()

    assert admin_api._BROADCAST_TOTAL_DEADLINE_SECONDS == 300.0
    assert admin_api._broadcast_total_deadline_seconds() == 300.0


@pytest.mark.asyncio
async def test_deadline_is_read_at_call_time_not_bound_early(tmp_path: Path) -> None:
    """注入点必须在**执行期**被读取（B 要求的第 2 条断言）。

    若有人把 ``deadline = _broadcast_total_deadline_seconds()`` 提前绑定到模块
    导入路径或函数外的局部变量，上一条用例仍会绿，而注入永久失效 —— 这条专门
    封死那个改法。
    """

    admin_api = _admin_api()
    observed: list[float] = []

    def spy() -> float:
        observed.append(INJECTED_DEADLINE)
        return INJECTED_DEADLINE

    repository = await _repository(tmp_path)
    bot = _Bot(FAST_GROUP)
    with (
        patch.object(admin_api, "broadcasts", repository),
        patch.object(admin_api, "_current_bot", lambda: bot),
        patch.object(admin_api, "_broadcast_total_deadline_seconds", spy),
        patch.object(admin_api, "_BROADCAST_SEND_CONCURRENCY", 1),
    ):
        async with asyncio.timeout(10):
            await admin_api.admin_broadcast_send(group_ids=list(GROUPS), payload_text="hello")

    assert observed, "总时限必须在执行期读取（否则无法注入）"

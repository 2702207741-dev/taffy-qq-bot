"""Slang dictionary command-layer tests (V0.4 P4 matchers).

Covers every branch of ``slang_commands.handle_slang`` — teach/confirm/
reject/delete/re-enable/list plus fail-closed paths — so the command layer
stays covered without hitting the network or the real database.
"""

from __future__ import annotations

import gc
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

import src.plugins.chat.slang_commands as slang_cmd_module

SLANG = slang_cmd_module
MODULE = "src.plugins.chat.slang_commands"


class _AllowAllLimiter:
    """Async stub matching the real ``SlidingWindowLimiter.allow`` contract.

    The fixture used to be ``SimpleNamespace(allow=lambda key: True)`` — a
    *synchronous* callable.  That only worked while production code forgot the
    ``await``: any test using it passed no matter what, and the moment the
    ``await`` was added it raised ``TypeError: object bool can't be used in
    'await' expression``.  Stubbing the real (async) interface is what makes
    the throttle branch testable at all.
    """

    async def allow(self, key: str) -> bool:
        return True


class _FakeEntry:
    """Minimal stand-in for a slang repository row."""

    def __init__(
        self, id: int, term: str, meaning: str = "", status: str = "pending", enabled: bool = True
    ) -> None:
        self.id = id
        self.term = term
        self.meaning = meaning
        self.status = status
        self.enabled = enabled


def _event(text: str, user_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        group_id=2001,
        get_plaintext=lambda: text,
    )


async def _run(text: str, *, user_id: int = 1001) -> list[str]:
    """Drive handle_slang with fakes; returns the finished messages."""
    finished: list[str] = []

    async def fake_finish(message: object) -> None:
        finished.append(str(message))

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    async def fake_record(event: object, **kwargs: object) -> None:
        return None

    fake_repo = SimpleNamespace(
        teach=AsyncMock(return_value=(_FakeEntry(1, "6", "形容厉害"), True)),
        pending=AsyncMock(return_value=[_FakeEntry(1, "6", "形容厉害")]),
        accepted_for_injection=AsyncMock(
            return_value=[_FakeEntry(1, "6", "形容厉害", status="accepted")]
        ),
        accepted_all=AsyncMock(return_value=[_FakeEntry(1, "6", "形容厉害", status="accepted")]),
        decide=AsyncMock(return_value=_FakeEntry(1, "6", status="accepted")),
        delete=AsyncMock(return_value=_FakeEntry(1, "6", status="accepted")),
        set_enabled=AsyncMock(return_value=_FakeEntry(1, "6", status="accepted", enabled=True)),
    )

    with (
        patch.object(SLANG, "check_feature", fake_check),
        patch.object(SLANG, "record_event_usage", fake_record),
        patch.object(SLANG, "learning_enabled", lambda: True),
        patch.object(SLANG, "_RATE_LIMITER", _AllowAllLimiter()),
        patch.object(
            SLANG,
            "persona_scope",
            lambda event: SimpleNamespace(group_id=2001, owner_user_id=user_id, label="群聊"),
        ),
        patch.object(SLANG, "slang_cmd", SimpleNamespace(finish=fake_finish)),
        patch("src.state.runtime.persona_slang", fake_repo),
    ):
        with pytest.raises(RuntimeError):
            await slang_cmd_module.handle_slang(None, _event(text, user_id))
    return finished


@pytest.mark.asyncio
async def test_teach_new_term() -> None:
    finished = await _run("塔菲学话 6 :: 形容厉害")
    assert finished and "记下了" in finished[0] and "6" in finished[0]


@pytest.mark.asyncio
async def test_teach_duplicate_reports_existing() -> None:
    fake_repo = SimpleNamespace(
        teach=AsyncMock(return_value=(_FakeEntry(1, "6", status="pending"), False)),
    )
    finished: list[str] = []

    async def fake_finish(message: object) -> None:
        finished.append(str(message))

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    async def fake_record(event: object, **kwargs: object) -> None:
        return None

    with (
        patch.object(SLANG, "check_feature", fake_check),
        patch.object(SLANG, "record_event_usage", fake_record),
        patch.object(SLANG, "learning_enabled", lambda: True),
        patch.object(SLANG, "_RATE_LIMITER", _AllowAllLimiter()),
        patch.object(
            SLANG,
            "persona_scope",
            lambda event: SimpleNamespace(group_id=2001, owner_user_id=1001, label="群聊"),
        ),
        patch.object(SLANG, "slang_cmd", SimpleNamespace(finish=fake_finish)),
        patch("src.state.runtime.persona_slang", fake_repo),
    ):
        with pytest.raises(RuntimeError):
            await slang_cmd_module.handle_slang(None, _event("塔菲学话 6 :: 形容厉害"))
    assert finished and "已经教过啦" in finished[0]


@pytest.mark.asyncio
async def test_teach_bad_format() -> None:
    finished = await _run("塔菲学话 只有一个词")
    assert finished and "格式是这样" in finished[0]


@pytest.mark.asyncio
async def test_confirm_pending() -> None:
    finished = await _run("确认话术 1")
    assert finished and "接受了" in finished[0]


@pytest.mark.asyncio
async def test_confirm_bad_index() -> None:
    finished = await _run("确认话术 99")
    assert finished and "没有这个序号" in finished[0]


@pytest.mark.asyncio
async def test_confirm_non_numeric() -> None:
    finished = await _run("确认话术 abc")
    assert finished and "要给我一个序号" in finished[0]


@pytest.mark.asyncio
async def test_reject_pending() -> None:
    finished = await _run("拒绝话术 1")
    assert finished and "拒绝了" in finished[0]


@pytest.mark.asyncio
async def test_delete_accepted() -> None:
    finished = await _run("删除话术 1")
    assert finished and "删掉了" in finished[0]


@pytest.mark.asyncio
async def test_delete_bad_index() -> None:
    finished = await _run("删除话术 99")
    assert finished and "序号无效" in finished[0]


@pytest.mark.asyncio
async def test_enable_accepted() -> None:
    finished = await _run("启用话术 1")
    assert finished and "重新可以说了" in finished[0]


@pytest.mark.asyncio
async def test_list_empty_reply() -> None:
    fake_repo = SimpleNamespace(
        pending=AsyncMock(return_value=[]),
        accepted_all=AsyncMock(return_value=[]),
    )
    finished: list[str] = []

    async def fake_finish(message: object) -> None:
        finished.append(str(message))

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    async def fake_record(event: object, **kwargs: object) -> None:
        return None

    with (
        patch.object(SLANG, "check_feature", fake_check),
        patch.object(SLANG, "record_event_usage", fake_record),
        patch.object(SLANG, "learning_enabled", lambda: True),
        patch.object(SLANG, "_RATE_LIMITER", _AllowAllLimiter()),
        patch.object(
            SLANG,
            "persona_scope",
            lambda event: SimpleNamespace(group_id=2001, owner_user_id=1001, label="群聊"),
        ),
        patch.object(SLANG, "slang_cmd", SimpleNamespace(finish=fake_finish)),
        patch("src.state.runtime.persona_slang", fake_repo),
        patch.object(SLANG, "prompt_terms", AsyncMock(return_value=[])),
    ):
        with pytest.raises(RuntimeError):
            await slang_cmd_module.handle_slang(None, _event("话术列表"))
    assert finished and "还没有教过" in finished[0]


@pytest.mark.asyncio
async def test_learning_disabled_short_circuits() -> None:
    finished: list[str] = []

    async def fake_finish(message: object) -> None:
        finished.append(str(message))

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    async def fake_record(event: object, **kwargs: object) -> None:
        return None

    with (
        patch.object(SLANG, "check_feature", fake_check),
        patch.object(SLANG, "record_event_usage", fake_record),
        patch.object(SLANG, "learning_enabled", lambda: False),
        patch.object(SLANG, "_RATE_LIMITER", _AllowAllLimiter()),
        patch.object(SLANG, "slang_cmd", SimpleNamespace(finish=fake_finish)),
    ):
        with pytest.raises(RuntimeError):
            await slang_cmd_module.handle_slang(None, _event("话术列表"))
    assert finished and "学习功能已关闭" in finished[0]


@pytest.mark.asyncio
async def test_feature_gate_blocks() -> None:
    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return False

    with patch.object(SLANG, "check_feature", fake_check):
        result = await slang_cmd_module.handle_slang(None, _event("话术列表"))
    assert result is None


class _RecordingLimiter:
    """Async limiter stub that records whether ``allow`` was ever awaited.

    ``_run`` patches ``_RATE_LIMITER`` with ``SimpleNamespace(allow=lambda ...)``
    — a *sync* callable — which is exactly why the missing ``await`` at
    ``slang_commands.py:62`` stayed invisible: with the real
    ``SlidingWindowLimiter``, ``allow`` is a coroutine, so ``not <coroutine>``
    is always ``False`` and the throttle branch never runs.
    """

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.awaited = False

    async def allow(self, key: str) -> bool:
        self.awaited = True
        return self.allowed


async def _run_with_limiter(text: str, limiter: object, *, user_id: int = 1001) -> list[str]:
    """``_run`` 的变体：注入真实（异步）限流器，而不是同步桩。"""

    finished: list[str] = []

    async def fake_finish(message: object) -> None:
        finished.append(str(message))

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    async def fake_record(event: object, **kwargs: object) -> None:
        return None

    fake_repo = SimpleNamespace(
        pending=AsyncMock(return_value=[_FakeEntry(1, "6", "形容厉害")]),
        accepted_for_injection=AsyncMock(
            return_value=[_FakeEntry(1, "6", "形容厉害", status="accepted")]
        ),
        accepted_all=AsyncMock(return_value=[_FakeEntry(1, "6", "形容厉害", status="accepted")]),
        teach=AsyncMock(return_value=(_FakeEntry(1, "6"), True)),
        decide=AsyncMock(return_value=_FakeEntry(1, "6", status="accepted")),
        delete=AsyncMock(return_value=_FakeEntry(1, "6", status="accepted")),
        set_enabled=AsyncMock(return_value=_FakeEntry(1, "6", status="accepted")),
    )

    with (
        patch.object(SLANG, "check_feature", fake_check),
        patch.object(SLANG, "record_event_usage", fake_record),
        patch.object(SLANG, "learning_enabled", lambda: True),
        patch.object(SLANG, "_RATE_LIMITER", limiter),
        patch.object(
            SLANG,
            "persona_scope",
            lambda event: SimpleNamespace(group_id=2001, owner_user_id=user_id, label="群聊"),
        ),
        patch.object(SLANG, "slang_cmd", SimpleNamespace(finish=fake_finish)),
        patch("src.state.runtime.persona_slang", fake_repo),
        patch.object(SLANG, "prompt_terms", AsyncMock(return_value=[])),
    ):
        with pytest.raises(RuntimeError):
            await slang_cmd_module.handle_slang(None, _event(text, user_id))
    return finished


async def _run_with_repo(text: str, repo: object, *, user_id: int = 1001) -> list[str]:
    """``_run`` 的变体：注入指定的仓储假件（用于校验「命中了哪一条」）。"""

    finished: list[str] = []

    async def fake_finish(message: object) -> None:
        finished.append(str(message))

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    async def fake_record(event: object, **kwargs: object) -> None:
        return None

    with (
        patch.object(SLANG, "check_feature", fake_check),
        patch.object(SLANG, "record_event_usage", fake_record),
        patch.object(SLANG, "learning_enabled", lambda: True),
        patch.object(SLANG, "_RATE_LIMITER", _AllowAllLimiter()),
        patch.object(
            SLANG,
            "persona_scope",
            lambda event: SimpleNamespace(group_id=2001, owner_user_id=user_id, label="群聊"),
        ),
        patch.object(SLANG, "slang_cmd", SimpleNamespace(finish=fake_finish)),
        patch("src.state.runtime.persona_slang", repo),
        patch.object(SLANG, "prompt_terms", AsyncMock(return_value=[])),
    ):
        with pytest.raises(RuntimeError):
            await slang_cmd_module.handle_slang(None, _event(text, user_id))
    return finished


def _listing_repo() -> tuple[SimpleNamespace, AsyncMock, AsyncMock]:
    """构造「打印列表 ≠ 注入列表」的仓储假件。

    三条已确认词条，``confidence`` 互不相同，且把**已停用**的那条放在**中间**排名：

    * ``accepted_all``（打印列表，含停用）→ ``[11 高优, 22 中停用, 33 低优]``
    * ``accepted_for_injection``（仅启用）→ ``[11 高优, 33 低优]``

    两条 confidence 相同的话两份列表会偶然同序，「序号错位」就测不出来了。
    """

    listed = [
        _FakeEntry(11, "高优", status="accepted", enabled=True),
        _FakeEntry(22, "中停用", status="accepted", enabled=False),
        _FakeEntry(33, "低优", status="accepted", enabled=True),
    ]
    enabled_only = [listed[0], listed[2]]
    delete_mock = AsyncMock(return_value=listed[1])
    set_enabled_mock = AsyncMock(return_value=listed[1])
    repo = SimpleNamespace(
        pending=AsyncMock(return_value=[]),
        accepted_all=AsyncMock(return_value=list(listed)),
        accepted_for_injection=AsyncMock(return_value=list(enabled_only)),
        delete=delete_mock,
        set_enabled=set_enabled_mock,
        decide=AsyncMock(return_value=listed[0]),
        teach=AsyncMock(return_value=(listed[0], True)),
    )
    return repo, delete_mock, set_enabled_mock


@pytest.mark.asyncio
async def test_delete_targets_the_printed_listing_row() -> None:
    """M-19：``删除话术 N`` 必须命中「话术列表」第 N 行对应的那条。

    修复前删除走 ``accepted_for_injection``（仅启用），而打印走 ``accepted_all``
    （含停用）→ 只要存在停用条目，序号就整体错位，删掉的是**别人那条**。
    """

    repo, delete_mock, _ = _listing_repo()
    # 「话术列表」的已生效段按 accepted_all 顺序渲染：
    #   1. 11 高优  2. 22 中停用（已停用）  3. 33 低优
    printed_row_2_id = 22

    finished = await _run_with_repo("删除话术 2", repo)

    assert delete_mock.await_count == 1, "删除分支必须恰好调用一次 delete"
    assert delete_mock.await_args.args[0] == printed_row_2_id, (
        f"删除第 2 行应命中 id={printed_row_2_id}，"
        f"实际命中 {delete_mock.await_args.args[0]}（说明用的是另一份列表）"
    )
    assert finished and "删掉了" in finished[0]


@pytest.mark.asyncio
async def test_enable_targets_the_same_printed_listing_row() -> None:
    """反向对照：``启用话术 N`` 与打印列表同源（修复前后都应成立，用于锁住一致性）。"""

    repo, _, set_enabled_mock = _listing_repo()

    await _run_with_repo("启用话术 2", repo)

    assert set_enabled_mock.await_count == 1
    assert set_enabled_mock.await_args.args[0] == 22


@pytest.mark.asyncio
async def test_throttle_branch_rejects_with_exact_text() -> None:
    """M-04：配额用尽必须落到限流分支（断言精确文案 + 分支副作用）。

    只断言「收到某条拒绝回复」不够 —— ``check_feature`` 与
    ``learning_enabled`` 也会走 ``_finish`` 回话，那样测的是别的门，不是限流。
    """

    limiter = _RecordingLimiter(allowed=False)

    finished = await _run_with_limiter("话术列表", limiter)

    assert limiter.awaited is True, "限流检查必须被 await（漏 await 时协程永不被执行）"
    assert finished and finished[0] == "话术操作太频繁了，稍后再试喵。"


@pytest.mark.asyncio
async def test_throttle_check_leaves_no_unawaited_coroutine() -> None:
    """M-04 第二道封死：把「未 await 协程」本身变成失败。

    即使将来有人把 ``_RATE_LIMITER`` 换成别的开关、或改动 ``not`` 的写法，
    只要限流检查没有被 await，这里就会留下 ``never awaited`` 告警。
    """

    limiter = _RecordingLimiter(allowed=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await _run_with_limiter("话术列表", limiter)
        gc.collect()

    unawaited = [item for item in caught if "never awaited" in str(item.message)]
    assert not unawaited, f"检测到未 await 的协程：{[str(i.message) for i in unawaited]}"


@pytest.mark.asyncio
async def test_is_slang_request_matcher_rule() -> None:
    import nonebot.adapters.onebot.v11 as onebot_v11

    class _FakeGroup:
        def get_plaintext(self) -> str:
            return self.text

        def __init__(self, text: str) -> None:
            self.text = text

    class _NotGroup:
        pass

    with patch.object(onebot_v11, "GroupMessageEvent", _FakeGroup):
        assert slang_cmd_module._is_slang_request(_FakeGroup("塔菲学话 6 :: 形容厉害"))
        assert slang_cmd_module._is_slang_request(_FakeGroup("话术列表"))
        assert not slang_cmd_module._is_slang_request(_FakeGroup("在吗"))
        assert not slang_cmd_module._is_slang_request(_NotGroup())

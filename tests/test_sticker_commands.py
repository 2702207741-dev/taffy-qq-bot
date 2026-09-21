"""Sticker admin command-layer tests (V0.5 phase two matchers).

Exercises every branch of ``sticker_commands.handle_sticker_admin`` —
tag/review/accept/reject plus fail-closed paths — with fakes only.

``matcher.finish`` raises ``FinishedException`` in production, so every
fake ``finish`` here raises a marker exception as well; that is what makes
the ``return`` statements after ``finish`` in the production code reachable
and observable.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

import src.plugins.chat.sticker_commands as sticker_cmd_module

STICKER = sticker_cmd_module


class _Finished(Exception):
    pass


def _event(text: str, user_id: int = 1243607273) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, get_plaintext=lambda: text)


def _raising_finish(finished: list[str]):
    async def fake_finish(message: object) -> None:
        finished.append(str(message))
        raise _Finished

    return fake_finish


def _patched_context(
    *,
    finished: list[str],
    stickers: object,
    vision: object,
    superusers: object = None,
    feature_allowed: bool = True,
):
    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return feature_allowed

    stack = ExitStack()
    stack.enter_context(patch.object(STICKER, "check_feature", fake_check))
    stack.enter_context(
        patch.object(
            STICKER, "sticker_admin_cmd", SimpleNamespace(finish=_raising_finish(finished))
        )
    )
    stack.enter_context(
        patch.object(
            STICKER,
            "_superusers",
            superusers if superusers is not None else lambda: {"1243607273"},
        )
    )
    stack.enter_context(patch("src.services.stickers", stickers, create=True))
    stack.enter_context(patch("src.services.vision", vision, create=True))
    return stack


def _base_stickers(**overrides: object) -> SimpleNamespace:
    base = SimpleNamespace(
        stickers_enabled=lambda: True,
        entries_missing_description=lambda limit=10: [],
        pending_stickers=lambda: [],
        load_pending_metadata=lambda path: None,
        accept_pending_sticker=lambda index, tags: None,
        reject_pending_sticker=lambda index: None,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


async def _run(text: str, *, user_id: int = 1243607273) -> list[str]:
    """Drive handle_sticker_admin with fakes; returns finished messages."""
    finished: list[str] = []
    context = _patched_context(
        finished=finished,
        stickers=_base_stickers(),
        vision=SimpleNamespace(
            tag_pending_images=AsyncMock(return_value=0),
            describe_image=AsyncMock(return_value=None),
        ),
    )
    with context:
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event(text, user_id))
        except _Finished:
            pass
    return finished


# ---------------------------------------------------------------------------
# 塔菲贴图打标
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_all_described() -> None:
    finished = await _run("塔菲贴图打标")
    assert finished and "都有描述了" in finished[0]


@pytest.mark.asyncio
async def test_tag_run_reports_progress() -> None:
    finished: list[str] = []
    stickers = _base_stickers(
        entries_missing_description=lambda limit=10: [SimpleNamespace(path=Path("a.png"))],
    )
    vision = SimpleNamespace(
        tag_pending_images=AsyncMock(return_value=1),
        describe_image=AsyncMock(return_value=None),
    )
    with _patched_context(finished=finished, stickers=stickers, vision=vision):
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event("塔菲贴图打标"))
        except _Finished:
            pass
    assert finished and "本轮打标完成 1/1" in finished[0]


# ---------------------------------------------------------------------------
# 塔菲贴图审核
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_empty_pool() -> None:
    finished = await _run("塔菲贴图审核")
    assert finished and "待审贴图池是空的" in finished[0]


@pytest.mark.asyncio
async def test_review_lists_pending() -> None:
    finished: list[str] = []
    stickers = _base_stickers(pending_stickers=lambda: [Path("meme1.png"), Path("meme2.gif")])
    with _patched_context(finished=finished, stickers=stickers, vision=SimpleNamespace()):
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event("塔菲贴图审核"))
        except _Finished:
            pass
    assert finished and "meme1.png" in finished[0] and "确认贴图 序号" in finished[0]


# ---------------------------------------------------------------------------
# 确认贴图 / 拒绝贴图
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_bad_format() -> None:
    # "确认贴图 1a" matches the matcher regex but int("1a") raises -> format reply
    finished = await _run("确认贴图 1a")
    assert finished and "格式：确认贴图 序号 标签1 标签2" in finished[0]


@pytest.mark.asyncio
async def test_accept_missing_index() -> None:
    finished = await _run("确认贴图 99 可爱")
    assert finished and "没有这个序号" in finished[0]


@pytest.mark.asyncio
async def test_accept_with_description() -> None:
    accepted = SimpleNamespace(name="meme1.png")
    finished: list[str] = []
    stickers = _base_stickers(accept_pending_sticker=lambda index, tags: accepted)
    vision = SimpleNamespace(
        tag_pending_images=AsyncMock(return_value=0),
        describe_image=AsyncMock(return_value=("一只猫猫在卖萌", ["可爱"])),
    )
    with _patched_context(finished=finished, stickers=stickers, vision=vision):
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event("确认贴图 1 可爱"))
        except _Finished:
            pass
    assert finished and "已收入 meme1.png" in finished[0] and "一只猫猫" in finished[0]


@pytest.mark.asyncio
async def test_accept_describe_failure_still_reports() -> None:
    accepted = SimpleNamespace(name="meme1.png")
    finished: list[str] = []
    stickers = _base_stickers(accept_pending_sticker=lambda index, tags: accepted)
    vision = SimpleNamespace(
        tag_pending_images=AsyncMock(return_value=0),
        describe_image=AsyncMock(return_value=None),
    )
    with _patched_context(finished=finished, stickers=stickers, vision=vision):
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event("确认贴图 1 可爱"))
        except _Finished:
            pass
    assert finished and "描述打标失败" in finished[0]


@pytest.mark.asyncio
async def test_reject_bad_format() -> None:
    finished = await _run("拒绝贴图 1a")
    assert finished and "格式：拒绝贴图 序号" in finished[0]


@pytest.mark.asyncio
async def test_reject_missing_index() -> None:
    finished = await _run("拒绝贴图 99")
    assert finished and "没有这个序号" in finished[0]


@pytest.mark.asyncio
async def test_reject_ok() -> None:
    finished: list[str] = []
    stickers = _base_stickers(reject_pending_sticker=lambda index: "meme2.gif")
    with _patched_context(finished=finished, stickers=stickers, vision=SimpleNamespace()):
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event("拒绝贴图 1"))
        except _Finished:
            pass
    assert finished and "已丢弃：meme2.gif" in finished[0]


# ---------------------------------------------------------------------------
# fail-closed 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stickers_disabled_fail_closed() -> None:
    finished: list[str] = []
    stickers = SimpleNamespace(stickers_enabled=lambda: False)
    with _patched_context(finished=finished, stickers=stickers, vision=SimpleNamespace()):
        try:
            await sticker_cmd_module.handle_sticker_admin(None, _event("塔菲贴图打标"))
        except _Finished:
            pass
    assert finished and "未开启" in finished[0]


@pytest.mark.asyncio
async def test_feature_gate_blocks() -> None:
    finished: list[str] = []
    with _patched_context(
        finished=finished,
        stickers=_base_stickers(),
        vision=SimpleNamespace(),
        feature_allowed=False,
    ):
        result = await sticker_cmd_module.handle_sticker_admin(None, _event("塔菲贴图打标"))
    assert result is None
    assert finished == []


@pytest.mark.asyncio
async def test_matcher_rule_superuser_only() -> None:
    import re
    from unittest.mock import MagicMock

    import nonebot.adapters.onebot.v11 as onebot_v11

    from src.plugins.chat.sticker_commands import _is_sticker_admin_request

    pattern = re.compile(r"(塔菲贴图打标|塔菲贴图审核|确认贴图 \d+.*|拒绝贴图 \d+)")
    assert pattern.fullmatch("塔菲贴图打标")
    assert pattern.fullmatch("塔菲贴图审核")
    assert pattern.fullmatch("确认贴图 1 可爱")
    assert pattern.fullmatch("拒绝贴图 3")
    assert not pattern.fullmatch("在吗")

    with patch.object(STICKER, "_superusers", lambda: {"1243607273"}):
        admin_event = MagicMock(spec=onebot_v11.GroupMessageEvent)
        admin_event.user_id = 1243607273
        admin_event.get_plaintext.return_value = "塔菲贴图打标"
        stranger_event = MagicMock(spec=onebot_v11.GroupMessageEvent)
        stranger_event.user_id = 9999
        stranger_event.get_plaintext.return_value = "塔菲贴图打标"
        assert _is_sticker_admin_request(admin_event)
        assert not _is_sticker_admin_request(stranger_event)

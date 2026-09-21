"""开箱EV 缓存契约（M-03）：两条写入路径必须产出同一个 payload 形状。

背景：文本路径（``build_case_ev_reply``，QQ 命令）与桌面路径
（``build_case_ev_payload``）共用同一个 EV 缓存键，但只有文本路径往
payload 里写 ``reply_text``。桌面路径先落缓存时，文本路径读到
``cached.get("reply_text") or ""`` → **返回空消息**（无日志、无异常）。

本文件按四条封死来锁：
1. 写入侧不变量：两条路径经 ``store_ev`` 落盘的 payload **键集合一致**；
2. 双向顺序各测一次：桌面→文本、文本→桌面；
3. 「计算只发生一次」的计数断言 —— 这条能区分「命中缓存」与「重算后覆写
   缓存」（后者也会让非空断言变绿）；
4. 修复前遗留的旧行（缺 ``reply_text``）不得再产出空消息。

全部使用 ``tmp_path`` 上的真实 ``CaseMetaStore``（真缓存、真 SQLite），
只 patch 网络/同步边界与昂贵的 ``compute_case_ev``。
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.services import case_ev as case_ev_module
from src.services.case_ev import (
    CaseEvResult,
    build_case_ev_payload,
    build_case_ev_reply,
    compute_case_ev,
)
from src.services.case_meta import CaseMetaStore, CrateInfo, CrateItem

CRATE_NAME = "测试箱"
CASE_PRICE = 60.0


def _crate() -> CrateInfo:
    return CrateInfo(
        crate_id="crate-test",
        name_en="Test Case",
        name_zh=CRATE_NAME,
        market_hash_name="Test Case",
        crate_type="Case",
        first_sale_date="2026-01-01",
        rare_count=0,
        unknown_count=0,
        fetched_at=int(time.time()),
    )


def _items() -> tuple[CrateItem, ...]:
    return (CrateItem("Test Skin", None, "milspec"),)


async def _real_result() -> CaseEvResult:
    """跑一次**真实** compute（价格走桩），用来给计数桩当返回值。"""

    async def fake_price(name: str) -> dict | None:
        return {"min_sell": CASE_PRICE}

    with patch.object(case_ev_module, "_price_one", side_effect=fake_price):
        return await compute_case_ev(_crate(), _items(), ())


class _Harness:
    """tmp_path 上的真实 store + 计数的 compute 桩 + 落盘记录。"""

    def __init__(self, store: CaseMetaStore, result: CaseEvResult) -> None:
        self.store = store
        self.result = result
        self.compute_calls = 0
        self.written: list[dict] = []

    async def compute(self, *args: object, **kwargs: object) -> CaseEvResult:
        self.compute_calls += 1
        return self.result

    def record_writes(self) -> None:
        """包一层 ``store_ev``，把两条路径实际落盘的 payload 记下来。"""

        real_store_ev = self.store.store_ev

        async def recording_store_ev(market_hash_name: str, payload: dict) -> None:
            self.written.append(dict(payload))
            await real_store_ev(market_hash_name, payload)

        self.store.store_ev = recording_store_ev  # type: ignore[method-assign]

    def patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(case_ev_module, "case_meta_store", self.store))
        stack.enter_context(patch.object(case_ev_module, "case_ev_enabled", lambda: True))
        stack.enter_context(
            patch.object(case_ev_module, "_catalog_market_names", AsyncMock(return_value=()))
        )
        stack.enter_context(patch.object(case_ev_module, "compute_case_ev", self.compute))
        return stack


async def _harness(tmp_path: Path) -> _Harness:
    store = CaseMetaStore(tmp_path / "case_meta.db")
    await store.initialize()
    store.sync_if_due = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.find_crate = AsyncMock(return_value=_crate())  # type: ignore[method-assign]
    store.crate_items = AsyncMock(return_value=_items())  # type: ignore[method-assign]
    harness = _Harness(store, await _real_result())
    harness.record_writes()
    return harness


@pytest.mark.asyncio
async def test_desktop_then_text_never_returns_an_empty_reply(tmp_path: Path) -> None:
    """顺序 A（桌面先写）：文本路径必须仍能渲染出非空回复，且不重算。"""

    harness = await _harness(tmp_path)
    with harness.patches():
        payload = await build_case_ev_payload(CRATE_NAME)
        reply = await build_case_ev_reply(CRATE_NAME)

    assert payload is not None
    assert reply.strip() != "", "文本路径不得返回空消息（M-03 的直接症状）"
    assert "开箱EV" in reply
    assert harness.compute_calls == 1, (
        f"桌面路径算过之后文本路径应命中缓存；实际 compute 调用 {harness.compute_calls} 次"
        "（说明被重算了）"
    )


@pytest.mark.asyncio
async def test_text_then_desktop_keeps_payload_renderable(tmp_path: Path) -> None:
    """顺序 B（文本先写）：桌面路径读回同一份缓存，仍然只算一次。"""

    harness = await _harness(tmp_path)
    with harness.patches():
        reply = await build_case_ev_reply(CRATE_NAME)
        payload = await build_case_ev_payload(CRATE_NAME)

    assert reply.strip() != ""
    assert payload is not None
    # 桌面契约里不含 QQ 渲染文本（形状由 test_desktop_response_shape_* 钉住）
    assert "reply_text" not in payload
    assert harness.compute_calls == 1


@pytest.mark.asyncio
async def test_both_paths_persist_the_same_payload_shape(tmp_path: Path) -> None:
    """写入侧不变量：两条路径落盘的 payload 键集合必须一致。"""

    desktop = await _harness(tmp_path / "desktop")
    with desktop.patches():
        await build_case_ev_payload(CRATE_NAME)

    text = await _harness(tmp_path / "text")
    with text.patches():
        await build_case_ev_reply(CRATE_NAME)

    assert desktop.written and text.written, "两条路径都必须落盘一次"
    desktop_keys = set(desktop.written[0])
    text_keys = set(text.written[0])
    assert desktop_keys == text_keys, (
        f"两条路径的 payload 键集合不一致：桌面 {sorted(desktop_keys)} vs 文本 {sorted(text_keys)}"
    )
    assert "reply_text" in desktop_keys, "reply_text 必须由写入侧统一补齐"


@pytest.mark.asyncio
async def test_legacy_payload_without_reply_text_is_repaired(tmp_path: Path) -> None:
    """修复前遗留的旧行（缺 ``reply_text``）不得再产出空消息。

    生产库里已经存在桌面路径写下的旧 payload；只修写入侧不够，读侧必须把
    这类行当作「缓存内容不完整」处理并补齐，否则旧行会继续吐空消息。
    """

    harness = await _harness(tmp_path)
    legacy = dict(case_ev_module.case_ev_payload(harness.result))
    legacy.pop("reply_text", None)
    await harness.store.store_ev(_crate().market_hash_name, legacy)
    harness.written.clear()

    with harness.patches():
        reply = await build_case_ev_reply(CRATE_NAME)
        again = await build_case_ev_reply(CRATE_NAME)

    assert reply.strip() != "", "遗留旧行被读到时不得返回空消息"
    assert "开箱EV" in reply
    assert again.strip() != ""
    # 读侧自愈后第二次必须命中缓存，不得重复计算
    assert harness.compute_calls == 1, (
        f"遗留旧行应只补齐一次；实际 compute 调用 {harness.compute_calls} 次"
    )


@pytest.mark.asyncio
async def test_desktop_response_shape_stays_free_of_reply_text(tmp_path: Path) -> None:
    """桌面 HTTP 契约：``reply_text`` 只属于缓存 payload，不得进响应体。

    ``desktop_api.desktop_market_case_ev``（``/api/desktop/market/case-ev``）返回
    ``{"computed": True, **payload}`` 且**没有** ``response_model`` → payload 的
    每一个键都会成为响应字段。修复前它在「QQ 路径先写」时才多出这个字段，
    属于**概率性**的公开契约变更；本用例把形状钉成确定值。
    """

    canonical = set(case_ev_module.case_ev_payload(await _real_result()))

    for order in ("desktop_first", "text_first"):
        harness = await _harness(tmp_path / order)
        with harness.patches():
            if order == "desktop_first":
                payload = await build_case_ev_payload(CRATE_NAME)
                await build_case_ev_reply(CRATE_NAME)
            else:
                await build_case_ev_reply(CRATE_NAME)
                payload = await build_case_ev_payload(CRATE_NAME)

        assert payload is not None
        assert "reply_text" not in payload, f"{order}: reply_text 泄漏进桌面响应体"
        assert set(payload) == canonical, (
            f"{order}: 桌面响应字段集与规范形状不一致：{sorted(set(payload) ^ canonical)}"
        )
        # 缓存里仍带 reply_text：写入侧契约不受影响
        cached = await harness.store.cached_ev(_crate().market_hash_name, ttl_seconds=9999)
        assert cached is not None and cached.get("reply_text")

"""V0.4 P4: slang dictionary (explicit teaching) and sticker phase one."""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.core.database import StateDatabase
from src.services import slang_service
from src.services.persona import build_persona_prompt
from src.services.slang_service import automine_once, prompt_terms
from src.services.stickers import (
    reset_runtime_state,
    select_sticker,
    stickers_enabled,
)
from src.state.persona_slang_repository import PersonaSlangRepository


def _patch(module: object, name: str, value: object):
    return unittest.mock.patch.object(module, name, value)


# ---------------------------------------------------------------------------
# Slang repository
# ---------------------------------------------------------------------------


async def _repo(tmp_path: Path) -> PersonaSlangRepository:
    repo = PersonaSlangRepository(StateDatabase(tmp_path / "state.db"))
    await repo.initialize()
    return repo


@pytest.mark.asyncio
async def test_slang_teach_dedupe_decide_and_injection_order(tmp_path: Path) -> None:
    repo = await _repo(tmp_path)

    entry, created = await repo.teach(2001, created_by=1001, term="6", meaning="形容厉害")
    assert created is True and entry.status == "pending"

    duplicate, created_again = await repo.teach(
        2001, created_by=1002, term="6", meaning="另一种解释"
    )
    assert created_again is False and duplicate.id == entry.id

    other_scope, _ = await repo.teach(0, created_by=1001, term="6", meaning="私聊的6")
    assert other_scope.scope_group_id == 0

    rejected, _ = await repo.teach(2001, created_by=1001, term="过时词", meaning="曾经流行")
    await repo.decide(rejected.id, scope_group_id=2001, status="rejected")

    pending = await repo.pending(2001)
    assert [item.term for item in pending] == ["6"]

    decided = await repo.decide(entry.id, scope_group_id=2001, status="accepted")
    assert decided is not None and decided.status == "accepted" and decided.accepted_at
    assert await repo.decide(entry.id, scope_group_id=2001, status="accepted") is None, (
        "decide is one-shot"
    )

    high, _ = await repo.teach(2001, created_by=1001, term="彳亍", meaning="行，可以")
    await repo.decide(high.id, scope_group_id=2001, status="accepted")
    injected = await repo.accepted_for_injection(2001)
    assert [item.term for item in injected] == ["彳亍", "6"]

    removed = await repo.delete(injected[0].id, scope_group_id=2001)
    assert removed is not None and removed.term == "彳亍"
    assert [item.term for item in await repo.accepted_for_injection(2001)] == ["6"]
    # Scope isolation: the group's decisions never touch the private entry.
    private_pending = await repo.pending(0)
    assert [item.term for item in private_pending] == ["6"]


@pytest.mark.asyncio
async def test_slang_prompt_terms_respect_learning_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(tmp_path)
    entry, _ = await repo.teach(2001, created_by=1001, term="6", meaning="形容厉害")
    await repo.decide(entry.id, scope_group_id=2001, status="accepted")

    import src.state.runtime as runtime_module

    monkeypatch.setattr(slang_service, "learning_enabled", lambda: True)
    with _patch(runtime_module, "persona_slang", repo):
        assert await prompt_terms(2001) == ("6（形容厉害）",)

    monkeypatch.setattr(slang_service, "learning_enabled", lambda: False)
    with _patch(runtime_module, "persona_slang", repo):
        assert await prompt_terms(2001) == ()


@pytest.mark.asyncio
async def test_prompt_renders_slang_block() -> None:
    plain = build_persona_prompt("在吗")
    assert "表达素材" not in plain

    decorated = build_persona_prompt("在吗", slang_terms=("6（形容厉害）", "彳亍（行）"))
    assert "表达素材" in decorated
    assert "6（形容厉害）" in decorated
    assert "每条回复最多一次" in decorated
    assert "禁止项" in decorated


# ---------------------------------------------------------------------------
# Automine (factory-off; even when on, everything stays pending)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_automine_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_SLANG_AUTOMINE_ENABLED", raising=False)
    assert await automine_once() == 0


@pytest.mark.asyncio
async def test_automine_proposes_pending_terms_without_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAFFY_SLANG_AUTOMINE_ENABLED", "true")
    repo = await _repo(tmp_path)

    lines = [f"太抽象了 {index}" for index in range(14)]

    async def fake_inputs(since: int, limit: int) -> list[str]:
        return lines[:limit]

    monkeypatch.setattr(slang_service, "learning_enabled", lambda: True)
    monkeypatch.setattr(slang_service, "automine_enabled", lambda: True)
    monkeypatch.setattr(slang_service, "_read_chat_inputs", fake_inputs)

    import src.state.runtime as runtime_module

    with _patch(runtime_module, "persona_slang", repo):
        created = await automine_once()

    assert created >= 1
    pending = await repo.pending(0)
    assert any(item.term == "太抽象了" for item in pending)
    assert all(item.created_by == 1 for item in pending)
    assert all("待确认含义" in item.meaning for item in pending)
    # The stopword filter must keep command vocabulary out of the dictionary.
    assert all(item.term not in {"大盘", "查询"} for item in pending)


# ---------------------------------------------------------------------------
# Private-scope ownership on the read side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_listings_can_be_filtered_to_one_speaker(
    tmp_path: Path,
) -> None:
    """``group_id == 0`` is shared across users, so listings must be filterable.

    The QQ commands resolve ``确认/删除/启用话术 N`` by index into these very
    listings, so an unfiltered read leaks another user's terms and shifts the
    numbering that then drives an ownership-fenced mutation.
    """

    repo = await _repo(tmp_path)
    await repo.teach(0, created_by=1001, term="我的词", meaning="甲")
    await repo.teach(0, created_by=1002, term="别人的词", meaning="乙")

    mine = await repo.pending(0, created_by=1001)
    assert {item.term for item in mine} == {"我的词"}
    assert all(item.created_by == 1001 for item in mine)

    theirs = await repo.pending(0, created_by=1002)
    assert {item.term for item in theirs} == {"别人的词"}

    # The operator console intentionally reads the whole bucket.
    assert {item.term for item in await repo.pending(0)} == {"我的词", "别人的词"}


@pytest.mark.asyncio
async def test_accepted_read_side_respects_speaker_and_group_scope(
    tmp_path: Path,
) -> None:
    repo = await _repo(tmp_path)
    for speaker, term in ((1001, "甲语"), (1002, "乙语")):
        entry, _ = await repo.teach(0, created_by=speaker, term=term, meaning="义")
        await repo.decide(entry.id, scope_group_id=0, created_by=speaker, status="accepted")

    assert {i.term for i in await repo.accepted_all(0, created_by=1001)} == {"甲语"}
    assert {i.term for i in await repo.accepted_for_injection(0, created_by=1002)} == {"乙语"}

    group_a, _ = await repo.teach(2001, created_by=1001, term="群词", meaning="义")
    await repo.decide(group_a.id, scope_group_id=2001, status="accepted")
    # A group's vocabulary belongs to the group: a member sees every row.
    assert {i.term for i in await repo.accepted_for_injection(2001)} == {"群词"}


@pytest.mark.asyncio
async def test_prompt_terms_threads_the_speaker_into_private_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(tmp_path)
    for speaker, term in ((1001, "甲语"), (1002, "乙语")):
        entry, _ = await repo.teach(0, created_by=speaker, term=term, meaning="义")
        await repo.decide(entry.id, scope_group_id=0, created_by=speaker, status="accepted")

    import src.state.runtime as runtime_module

    monkeypatch.setattr(slang_service, "learning_enabled", lambda: True)
    with _patch(runtime_module, "persona_slang", repo):
        alone = await prompt_terms(0, created_by=1001)
        everyone = await prompt_terms(0)

    assert alone == ("甲语（义）",)
    assert set(everyone) == {"甲语（义）", "乙语（义）"}


# ---------------------------------------------------------------------------
# Stickers phase one
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sticker_state() -> None:
    reset_runtime_state()


def _write_library(directory: Path, entries: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps({"stickers": entries}, ensure_ascii=False), encoding="utf-8"
    )


def test_sticker_manifest_cache_does_not_leak_between_libraries(
    tmp_path: Path,
) -> None:
    """A relocated library must not inherit the previous library's cache.

    The manifest cache is time-boxed, so a cache keyed only by time served one
    library's contents to another library for the whole window. That surfaced as
    an order-dependent sticker-listing failure in the desktop API tests.
    """

    import src.services.stickers as stickers_module

    library_a = tmp_path / "alpha"
    library_b = tmp_path / "beta"
    _write_library(library_a, [{"file": "a.png", "tags": ["甲"]}])
    _write_library(library_b, [{"file": "b.png", "tags": ["乙"]}])
    (library_a / "a.png").write_bytes(b"png-a")
    (library_b / "b.png").write_bytes(b"png-b")

    reset_runtime_state()
    with _patch(stickers_module, "_STICKER_DIR", library_a):
        names_a = {entry.path.name for entry in stickers_module._load_manifest().entries}
    with _patch(stickers_module, "_STICKER_DIR", library_b):
        names_b = {entry.path.name for entry in stickers_module._load_manifest().entries}

    assert names_a == {"a.png"}
    assert names_b == {"b.png"}

    # Same library still hits the cache rather than re-reading mid-window.
    with _patch(stickers_module, "_STICKER_DIR", library_b):
        _write_library(library_b, [{"file": "late.png", "tags": ["丙"]}])
        (library_b / "late.png").write_bytes(b"png-l")
        cached = {entry.path.name for entry in stickers_module._load_manifest().entries}
    assert cached == {"b.png"}

    reset_runtime_state()
    with _patch(stickers_module, "_STICKER_DIR", library_b):
        refreshed = {entry.path.name for entry in stickers_module._load_manifest().entries}
    assert refreshed == {"late.png"}


def test_stickers_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_STICKERS_ENABLED", raising=False)
    assert stickers_enabled() is False
    assert select_sticker("闲聊", "哈哈") is None


def test_sticker_selection_matches_tags_and_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    library = tmp_path / "stickers"
    _write_library(
        library,
        [
            {"file": "happy.png", "tags": ["开心", "大笑"]},
            {"file": "sleep.png", "tags": ["睡觉"]},
            {"file": "../evil.png", "tags": ["开心"]},  # traversal rejected
        ],
    )
    (library / "happy.png").write_bytes(b"png")
    (library / "sleep.png").write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)

    chosen = select_sticker("闲聊", "哈哈 笑死")
    assert chosen is not None and chosen.name == "happy.png"
    assert select_sticker("市场", "报价数据") is None, "market text never earns stickers"
    assert select_sticker(None, "完全没有关键词的普通回复") is None


def test_sticker_daily_cap_blocks_after_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.services.stickers as stickers_module

    monkeypatch.setenv("TAFFY_STICKERS_ENABLED", "true")
    # Patch the module constants directly: they are read once at import time,
    # so setenv() here is too late when another test imported the module first
    # (CI vs local import order differs).  Patch the value, not the env.
    monkeypatch.setattr(stickers_module, "_DAILY_CAP", 1)
    monkeypatch.setattr(stickers_module, "_COOLDOWN", 0.0)
    library = tmp_path / "stickers"
    _write_library(library, [{"file": "happy.png", "tags": ["开心"]}])
    (library / "happy.png").write_bytes(b"png")
    monkeypatch.setattr(stickers_module, "_STICKER_DIR", library)

    key = ("g_2001", 1)
    assert stickers_module.cooldown_and_cap_allow(key) is True
    stickers_module.record_send(key)
    assert stickers_module.cooldown_and_cap_allow(key) is False, "daily cap enforced"


@pytest.mark.asyncio
async def test_id_keyed_mutations_stay_inside_the_callers_scope(tmp_path: Path) -> None:
    """批 2 第 1 项：按 id 的操作必须钉在调用方 scope 内。

    假绿防线（B 的警告）：**不依赖生产存量**（真实库该表 0 行）—— 夹具自己合成
    三类行，且每一类都非空，再断言「落在别人 scope 的 id 一律动不了」。
    ``scope_group_id=0`` 是**跨用户共享的私聊桶**（该表主键没有 user 列），
    所以它额外需要 `created_by` 作为归属判据。
    """

    repo = await _repo(tmp_path)

    # 三类行，各自非空：群 2001、私聊(owner 1001)、私聊(owner 1002)
    group_row, _ = await repo.teach(2001, created_by=1001, term="群词", meaning="群里的")
    mine, _ = await repo.teach(0, created_by=1001, term="我的词", meaning="私聊A")
    other, _ = await repo.teach(0, created_by=1002, term="别人的词", meaning="私聊B")

    assert {group_row.scope_group_id, mine.scope_group_id, other.scope_group_id} == {0, 2001}
    assert mine.id != other.id

    # ① 跨群：拿 2001 的 id 去 9999 的 scope 里 decide/delete → 一律动不了
    assert await repo.decide(group_row.id, scope_group_id=9999, status="accepted") is None
    assert await repo.delete(group_row.id, scope_group_id=9999) is None
    assert [item.term for item in await repo.pending(2001)] == ["群词"], "行必须原样保留"

    # ② 私聊桶跨用户：用 owner 1001 去动 owner 1002 的行 → 动不了
    assert await repo.decide(other.id, scope_group_id=0, created_by=1001, status="accepted") is None
    assert await repo.delete(other.id, scope_group_id=0, created_by=1001) is None
    assert await repo.set_enabled(other.id, enabled=True, scope_group_id=0, created_by=1001) is None
    assert {item.term for item in await repo.pending(0)} == {"我的词", "别人的词"}, "两行都还在"

    # ③ 正向对照：同一个 scope 内操作必须成功（否则上面三条可能只是"全都失败"）
    assert await repo.decide(group_row.id, scope_group_id=2001, status="accepted") is not None
    assert (
        await repo.decide(other.id, scope_group_id=0, created_by=1002, status="accepted")
        is not None
    )
    assert (
        await repo.decide(mine.id, scope_group_id=0, created_by=1001, status="accepted") is not None
    )
    a_terms = {item.term for item in await repo.accepted_all(2001)}
    b_terms = {item.term for item in await repo.accepted_all(0)}
    assert a_terms and b_terms and a_terms.isdisjoint(b_terms), "两桶各自非空且交集为空"


@pytest.mark.asyncio
async def test_private_scope_id_operations_require_the_ownership_predicate(
    tmp_path: Path,
) -> None:
    """私聊桶（0）是跨用户共享的：缺 `created_by` 必须显式报错，而不是退化成"只按 id"。"""

    repo = await _repo(tmp_path)
    row, _ = await repo.teach(0, created_by=1001, term="词", meaning="义")

    with pytest.raises(ValueError):
        await repo.decide(row.id, scope_group_id=0, status="accepted")
    with pytest.raises(ValueError):
        await repo.delete(row.id, scope_group_id=0)

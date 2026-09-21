"""窗口B：挂刀指数适配器（iflow.work priority_archive dump）。

沙箱纪律：所有同步走注入 transport/临时库，绝不触碰网络与生产库。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services import hoard_index as hoard_module
from src.services.hoard_index import (
    HoardIndexStore,
    derive_snapshot,
    format_hoard_summary,
)
from src.services.resilience import CircuitBreaker

NOW = 1_800_000_000


def _line(
    name: str = "AK-47 | Redline (Field-Tested)",
    appid: int = 730,
    weighted: float | None = 0.72,
    update_time: int | None = NOW,
) -> str:
    entry: dict[str, object] = {
        "hash_name": name,
        "appid": appid,
        "cn_name": name,
        "en_name": name,
        "meta_info": {},
    }
    if weighted is not None:
        entry["weighted_ratio"] = weighted
    if update_time is not None:
        entry["update_time"] = update_time
    return json.dumps(entry, ensure_ascii=False)


def _dump(items: list[str]) -> str:
    return "\n".join(items) + "\n"


def _zip_of(text: str) -> bytes:
    buffer = __import__("io").BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dump.json", text)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 解析与派生
# ---------------------------------------------------------------------------


def test_parse_dump_skips_malformed_lines() -> None:
    items = hoard_module._parse_dump("not json\n{}\n[]\n")
    assert len(items) == 1


def test_derive_snapshot_median_and_percentiles() -> None:
    items = [
        json.loads(_line(f"Item {index}", appid=730, weighted=0.5 + index / 100))
        for index in range(20)
    ]
    snapshot = derive_snapshot(items, fetched_at=NOW, dump_name="2026-05-20-12-15.zip")
    assert snapshot.median_ratio == pytest.approx(0.6)  # 第10/20 个（0.5+9/100）
    assert snapshot.p25_ratio is not None and snapshot.p25_ratio <= snapshot.median_ratio
    assert snapshot.p75_ratio is not None and snapshot.p75_ratio >= snapshot.median_ratio
    assert snapshot.item_count == 20
    assert snapshot.cs2_count == 20


def test_derive_snapshot_excludes_non_cs2_and_sentinels() -> None:
    items = [
        json.loads(_line("CS2 A", appid=730, weighted=0.8)),
        json.loads(_line("Dota A", appid=570, weighted=0.5)),
        json.loads(_line("No ratio", appid=730, weighted=None)),
        json.loads(_line("Sentinel", appid=730, weighted=255)),
    ]
    snapshot = derive_snapshot(items, fetched_at=NOW)
    assert snapshot.cs2_count == 3
    assert snapshot.median_ratio == pytest.approx(0.8)


def test_derive_snapshot_no_ratios_is_honest() -> None:
    snapshot = derive_snapshot([json.loads(_line(weighted=None))], fetched_at=NOW)
    assert snapshot.median_ratio is None
    assert format_hoard_summary(snapshot) is None


def test_format_hoard_summary_contains_median_percentile_and_age() -> None:
    items = [
        json.loads(_line(f"Item {index}", appid=730, weighted=0.5 + index / 100, update_time=NOW))
        for index in range(20)
    ]
    snapshot = derive_snapshot(items, fetched_at=NOW)
    line = format_hoard_summary(snapshot)
    assert line is not None
    assert "挂刀" in line and "中位" in line and "分位" in line


def test_format_hoard_summary_archive_modes() -> None:
    import time
    from dataclasses import replace

    items = [json.loads(_line(f"Item {index}", appid=730, weighted=0.6)) for index in range(20)]
    base = derive_snapshot(items, fetched_at=int(time.time()))
    fresh = replace(base, dump_time=int(time.time()))
    fresh_line = format_hoard_summary(fresh)
    assert fresh_line is not None
    assert "今日中位" in fresh_line and "存档" not in fresh_line

    old = replace(base, dump_time=int(time.time()) - 100 * 86400)
    old_line = format_hoard_summary(old)
    assert old_line is not None
    assert "存档中位" in old_line and "存档 100 天前" in old_line
    assert "仅供历史参考" in old_line


# ---------------------------------------------------------------------------
# 存储：单飞 / TTL / 快照保留
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> HoardIndexStore:
    # 模块级熔断器是共享单例，测试里隔离，防止用例间累计打开；
    # enabled 开关同样在 fixture 内生效，覆盖单个测试里的多次 sync 调用
    with (
        patch.object(
            hoard_module,
            "get_circuit_breaker",
            return_value=CircuitBreaker("hoard-index-test"),
        ),
        patch.object(hoard_module, "hoard_index_enabled", return_value=True),
    ):
        yield HoardIndexStore(path=tmp_path / "hoard.db")


async def test_sync_blocking_persists_snapshot(store: HoardIndexStore) -> None:
    text = _dump([_line(f"Item {index}", weighted=0.6 + index / 1000) for index in range(20)])

    async def fake_list() -> str:
        return "2026-05-20-12-15.zip"

    async def fake_download(_name: str) -> str:
        return text

    with (
        patch.object(store, "_latest_dump_name", fake_list),
        patch.object(hoard_module, "_download_dump", fake_download),
    ):
        await store.initialize()
        ok = await store.sync_if_due(now=NOW, force=True)
    assert ok is True
    snapshot = await store.latest_snapshot()
    assert snapshot is not None
    assert snapshot.dump_name == "2026-05-20-12-15.zip"
    assert snapshot.median_ratio is not None


async def test_sync_ttl_skips_within_window(store: HoardIndexStore) -> None:
    text = _dump([_line(f"Item {index}", weighted=0.6 + index / 1000) for index in range(20)])

    async def fake_list() -> str:
        return "2026-05-20-12-15.zip"

    async def fake_download(_name: str) -> str:
        return text

    with (
        patch.object(store, "_latest_dump_name", fake_list),
        patch.object(hoard_module, "_download_dump", fake_download),
    ):
        await store.initialize()
        await store.sync_if_due(now=NOW, force=True)
        calls = []

        async def counting_list() -> str:
            calls.append(1)
            return "2026-05-20-12-15.zip"

        with patch.object(store, "_latest_dump_name", counting_list):
            ok = await store.sync_if_due(now=NOW + 3600)
    assert ok is True
    assert calls == []  # TTL 内不再拉列表


async def test_sync_failure_keeps_previous_snapshot(store: HoardIndexStore) -> None:
    text = _dump([_line(f"Item {index}", weighted=0.6 + index / 1000) for index in range(20)])

    async def fake_list() -> str:
        return "2026-05-20-12-15.zip"

    async def fake_download(_name: str) -> str:
        return text

    with (
        patch.object(store, "_latest_dump_name", fake_list),
        patch.object(hoard_module, "_download_dump", fake_download),
    ):
        await store.initialize()
        await store.sync_if_due(now=NOW, force=True)

    async def failing_list() -> str:
        raise RuntimeError("upstream down")

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    with (
        patch.object(store, "_latest_dump_name", failing_list),
        patch.object(hoard_module, "_record_telemetry", noop),
    ):
        ok = await store.sync_if_due(now=NOW + 86400, force=True)
    assert ok is True  # 旧快照保留
    snapshot = await store.latest_snapshot()
    assert snapshot is not None and snapshot.fetched_at == NOW


async def test_sync_disabled_returns_false(store: HoardIndexStore) -> None:
    with patch.object(hoard_module, "hoard_index_enabled", return_value=False):
        await store.initialize()
        assert await store.sync_if_due(now=NOW) is False

    # 恢复 enabled 以便 fixture 上下文一致
    hoard_module.hoard_index_enabled = lambda: True


async def test_background_sync_returns_without_snapshot(store: HoardIndexStore) -> None:
    async def fake_list() -> str:
        return "x.zip"

    async def fake_download(_name: str) -> str:
        raise OSError("synthetic-unreachable")

    # background=True spawns a real _run_sync task; stub the module-level
    # download so the spawned task fails fast instead of ever resolving
    # api.iflow.work.  The task is awaited *inside* the patch scope so it
    # finishes (or is cancelled) while the stub is still active.
    with (
        patch.object(store, "_latest_dump_name", fake_list),
        patch.object(hoard_module, "_download_dump", fake_download),
    ):
        await store.initialize()
        ok = await store.sync_if_due(now=NOW, background=True)
        spawned = store._sync_task
        if spawned is not None:
            try:
                await asyncio.wait_for(spawned, timeout=5)
            except asyncio.TimeoutError:
                spawned.cancel()
    assert ok is False  # 无快照 → 诚实"正在同步"
    await store.close()


async def test_item_ratio_roundtrip(store: HoardIndexStore) -> None:
    text = _dump(
        [
            _line("AK-47 | Redline (Field-Tested)", weighted=0.72),
            _line("AWP | Dragon Lore (Factory New)", weighted=0.55),
            _line("Dota Item", appid=570, weighted=0.5),
        ]
    )

    async def fake_list() -> str:
        return "2026-05-20-12-15.zip"

    async def fake_download(_name: str) -> str:
        return text

    with (
        patch.object(store, "_latest_dump_name", fake_list),
        patch.object(hoard_module, "_download_dump", fake_download),
    ):
        await store.initialize()
        await store.sync_if_due(now=NOW, force=True)
    assert await store.item_ratio("AK-47 | Redline (Field-Tested)") == pytest.approx(0.72)
    assert await store.item_ratio("AWP | Dragon Lore (Factory New)") == pytest.approx(0.55)
    assert await store.item_ratio("Dota Item") is None  # 非 CS2 不入库
    assert await store.item_ratio("Not Tracked") is None


# ---------------------------------------------------------------------------
# URL 校验与 zip 解包
# ---------------------------------------------------------------------------


def test_validate_public_url_rejects_private_hosts() -> None:
    assert hoard_module._validate_public_url("http://localhost/x") is False
    assert hoard_module._validate_public_url("https://127.0.0.1/x") is False
    assert hoard_module._validate_public_url("https://192.168.1.1/x") is False
    assert hoard_module._validate_public_url("file:///etc/passwd") is False
    assert hoard_module._validate_public_url("https://api.iflow.work/x") is True


def test_extract_zip_text_single_member() -> None:
    assert hoard_module._extract_zip_text(bytearray(_zip_of("hello"))) == "hello"


def test_extract_zip_text_rejects_bad_zip() -> None:
    with pytest.raises(ValueError):
        hoard_module._extract_zip_text(bytearray(b"not a zip"))

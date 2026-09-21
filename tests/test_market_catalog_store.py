from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.services import market_catalog_store as catalog_module
from src.services.market_catalog import CatalogEntry
from src.services.market_catalog_store import (
    CATALOG_QUOTA_RETRY_SECONDS,
    MarketCatalogStore,
)
from src.services.resource_governor import resource_governor


@pytest.mark.asyncio
async def test_close_cancels_and_reaps_background_refresh(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_refresh(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=blocking_refresh,
        wall_clock=lambda: 1_800_000_000,
        min_full_entries=1,
    )
    service.schedule_refresh_if_due()
    await started.wait()

    await service.close()

    assert cancelled.is_set()
    assert service._refresh_task is None


@pytest.mark.asyncio
async def test_full_catalog_refresh_is_durable_and_not_repeated_while_fresh(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    market_get = AsyncMock(
        return_value={
            "success": True,
            "data": [
                {
                    "marketHashName": "AK-47 | Leet Museo (Field-Tested)",
                    "name": "AK-47 | 抽象派 1337 (久经沙场)",
                },
                {
                    "marketHashName": "M4A4 | In Living Color (Factory New)",
                    "name": "M4A4 | 活色生香 (崭新出厂)",
                },
            ],
        }
    )
    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=market_get,
        wall_clock=lambda: now,
        min_full_entries=1,
    )

    assert await service.refresh_if_due() == 2
    entries = await service.entries()
    assert (
        CatalogEntry(
            "AK-47 | Leet Museo (Field-Tested)",
            "AK-47 | 抽象派 1337 (久经沙场)",
        )
        in entries
    )
    assert len(entries) == 2
    assert await service.refresh_if_due() == 0
    market_get.assert_awaited_once_with("/open/cs2/v1/base", None, 60 * 60)

    restarted = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=AsyncMock(side_effect=AssertionError("must not refresh")),
        wall_clock=lambda: now + 60,
        min_full_entries=1,
    )
    assert await restarted.entries() == entries


@pytest.mark.asyncio
async def test_catalog_quota_failure_uses_last_success_and_bounded_retry(
    tmp_path: Path,
) -> None:
    clock = [1_800_000_000]
    successful = AsyncMock(
        return_value={
            "success": True,
            "data": [
                {
                    "marketHashName": "AWP | Wildfire (Factory New)",
                    "name": "AWP | 野火 (崭新出厂)",
                }
            ],
        }
    )
    path = tmp_path / "market.db"
    first = MarketCatalogStore(
        path=path,
        market_get=successful,
        wall_clock=lambda: clock[0],
        min_full_entries=1,
    )
    assert await first.refresh_if_due() == 1
    expected = await first.entries()

    clock[0] += 24 * 60 * 60 + 10 * 60
    failing = AsyncMock(return_value={"success": False, "errorCode": 4005})
    restarted = MarketCatalogStore(
        path=path,
        market_get=failing,
        wall_clock=lambda: clock[0],
        min_full_entries=1,
    )
    assert await restarted.entries() == expected
    clock[0] += CATALOG_QUOTA_RETRY_SECONDS - 1
    assert await restarted.refresh_if_due() == 0
    failing.assert_awaited_once()


@pytest.mark.asyncio
async def test_retired_quote_snapshot_table_is_removed(tmp_path: Path) -> None:
    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=AsyncMock(return_value={"success": False, "errorCode": 4005}),
        wall_clock=lambda: 1_800_000_000,
    )

    def create_legacy_table(connection):
        connection.execute("CREATE TABLE quote_snapshots (value INTEGER)")

    await service._database_call(create_legacy_table)
    await service.initialize()

    def table_exists(connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quote_snapshots'"
        ).fetchone()
        return row is not None

    assert not await service._database_call(table_exists)


@pytest.mark.asyncio
async def test_catalog_integrity_gate_preserves_last_complete_catalog(tmp_path: Path) -> None:
    now = [1_800_000_000]

    def payload(count: int) -> dict[str, object]:
        return {
            "success": True,
            "data": [
                {"marketHashName": f"Item {index}", "name": f"饰品 {index}"}
                for index in range(count)
            ],
        }

    source = AsyncMock(side_effect=[payload(100), payload(89)])
    service = MarketCatalogStore(
        path=tmp_path / "gate.db",
        market_get=source,
        wall_clock=lambda: now[0],
        min_full_entries=100,
    )
    assert await service.refresh_if_due() == 100
    now[0] += 25 * 60 * 60
    assert await service.refresh_if_due() == 0
    assert await service.full_count() == 100
    status = await service.status()
    assert status["last_success_at"] == 1_800_000_000
    assert str(status["last_error"]).startswith("integrity_gate:89<")


@pytest.mark.asyncio
async def test_live_search_entries_are_persisted_without_downgrading_full_rows(
    tmp_path: Path,
) -> None:
    response = {
        "success": True,
        "data": [{"marketHashName": "Full Item", "name": "全量项"}],
    }
    service = MarketCatalogStore(
        path=tmp_path / "live.db",
        market_get=AsyncMock(return_value=response),
        wall_clock=lambda: 1_800_000_000,
        min_full_entries=1,
    )
    assert await service.refresh_if_due() == 1
    added = await service.persist_search_entries(
        (
            CatalogEntry("Full Item", "更新显示名"),
            CatalogEntry("Live Item", "即时项"),
            CatalogEntry("", "invalid"),
        )
    )
    assert added == 2
    assert await service.full_count() == 1
    entries = await service.entries()
    assert CatalogEntry("Full Item", "更新显示名") in entries
    status = await service.status()
    assert status["full_count"] == 1 and status["live_count"] == 1
    assert await service.persist_search_entries(()) == 0


@pytest.mark.asyncio
async def test_sample_pool_is_stable_bounded_and_prioritises_requested_items(
    tmp_path: Path,
) -> None:
    clock = [1_800_000_000]
    response = {
        "success": True,
        "data": [
            {"marketHashName": f"Item {index}", "name": f"饰品 {index}"} for index in range(20)
        ],
    }
    service = MarketCatalogStore(
        path=tmp_path / "sample.db",
        market_get=AsyncMock(return_value=response),
        wall_clock=lambda: clock[0],
        min_full_entries=1,
    )
    assert await service.refresh_if_due() == 20
    clock[0] += 10
    await service.persist_search_entries(
        (
            CatalogEntry("Item 7", "饰品 7"),
            CatalogEntry("Live Requested", "用户查询项"),
        )
    )

    first = await service.sample_entries(5)
    second = await service.sample_entries(5)

    assert first == second
    assert len(first) == 5
    assert {entry.market_name for entry in first[:2]} == {"Item 7", "Live Requested"}
    clock[0] += 10
    assert await service.mark_requested("Item 3")
    assert not await service.mark_requested("Missing Item")
    assert (await service.sample_entries(1))[0].market_name == "Item 3"


@pytest.mark.asyncio
async def test_catalog_refresh_is_paused_while_cs2_is_running(tmp_path: Path) -> None:
    source = AsyncMock()
    service = MarketCatalogStore(path=tmp_path / "game.db", market_get=source)
    original = resource_governor._game_running
    try:
        resource_governor._game_running = True
        assert await service.refresh_if_due() == 0
    finally:
        resource_governor._game_running = original
    source.assert_not_awaited()


def test_full_catalog_parser_deduplicates_and_rejects_invalid_rows() -> None:
    rows = catalog_module._parse_catalog(
        {
            "success": True,
            "data": [
                {"marketHashName": "A", "name": "甲"},
                {"marketHashName": "A", "name": "重复"},
                {"marketHashName": "", "name": "空"},
                {"marketHashName": "B", "name": ""},
                "bad",
            ],
        },
        123,
    )
    assert rows == [("A", "甲", 123)]
    assert catalog_module._parse_catalog({"success": False}, 123) == []
    assert catalog_module._parse_catalog({"success": True, "data": "bad"}, 123) == []


@pytest.mark.asyncio
async def test_catalog_network_failure_is_retried_later_without_deleting_rows(
    tmp_path: Path,
) -> None:
    source = AsyncMock(side_effect=TimeoutError("offline"))
    service = MarketCatalogStore(
        path=tmp_path / "offline.db",
        market_get=source,
        wall_clock=lambda: 1_800_000_000,
    )
    assert await service.refresh_if_due() == 0
    assert await service.refresh_if_due() == 0
    source.assert_awaited_once()
    status = await service.status()
    assert status["full_count"] == 0 and status["next_attempt_at"] is not None


# ─── Sitemap catalog sync ────────────────────────────────


def _sitemap_xml(names: list[str]) -> str:
    urls = "".join(f"<url><loc>https://steamdt.com/cs2/{name}</loc></url>" for name in names)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://steamdt.com/trend</loc></url>"
        "<url><loc>https://steamdt.com/mkt</loc></url>"
        "<url><loc>https://steamdt.com/cs2/tracker</loc></url>"
        "<url><loc>https://steamdt.com/cs2/null</loc></url>"
        f"{urls}</urlset>"
    )


def test_sitemap_parser_filters_decodes_and_deduplicates() -> None:
    from src.services.market_catalog_store import parse_sitemap_names

    xml = _sitemap_xml(
        [
            "AK-47%20%7C%20Asiimov%20(Minimal%20Wear)",
            "AK-47%20%7C%20Asiimov%20(Minimal%20Wear)",
            "Sticker%20%7C%20Hello%20AK-47/",
            "%E5%8D%B0%E8%8A%B1%7C%E5%B8%8C%E6%9C%9B%E4%B9%8B%E6%98%9F",
        ]
    )
    assert parse_sitemap_names(xml) == (
        "AK-47 | Asiimov (Minimal Wear)",
        "Sticker | Hello AK-47",
        "印花|希望之星",
    )


def test_sitemap_parser_rejects_invalid_xml() -> None:
    from src.services.market_catalog_store import parse_sitemap_names

    with pytest.raises(ValueError, match="not valid XML"):
        parse_sitemap_names("<urlset><url>")


@pytest.mark.asyncio
async def test_sitemap_migration_accepts_new_source_and_keeps_rows(tmp_path: Path) -> None:
    database = tmp_path / "market.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE catalog (
            market_name TEXT PRIMARY KEY NOT NULL,
            display_name TEXT NOT NULL,
            category TEXT NOT NULL,
            catalog_source TEXT NOT NULL
                CHECK (catalog_source IN ('seed', 'open_api', 'live_search')),
            catalog_updated_at INTEGER NOT NULL CHECK (catalog_updated_at > 0),
            last_requested_at INTEGER NOT NULL DEFAULT 0
                CHECK (last_requested_at >= 0)
        ) WITHOUT ROWID;
        """
    )
    connection.execute(
        "INSERT INTO catalog VALUES ('AK-47 | Redline (Field-Tested)', 'AK-47 | 红线',"
        " '', 'live_search', 1800000000, 0)"
    )
    connection.commit()
    connection.close()

    service = MarketCatalogStore(
        path=database,
        sitemap_fetch=AsyncMock(),
        min_sitemap_entries=1,
    )
    await service.initialize()
    await service._set_state_value("sitemap_probe", "1", 1_800_000_000)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO catalog VALUES ('Glock-18 | Fade (Factory New)',"
        " 'Glock-18 | 渐变之色', '', 'sitemap', 1800000001, 0)"
    )
    connection.commit()
    sources = dict(connection.execute("SELECT market_name, catalog_source FROM catalog"))
    connection.close()
    assert sources == {
        "AK-47 | Redline (Field-Tested)": "live_search",
        "Glock-18 | Fade (Factory New)": "sitemap",
    }
    assert await service.sitemap_count() == 1


@pytest.mark.asyncio
async def test_sitemap_sync_persists_names_without_touching_richer_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_module, "SITEMAP_SYNC_ENABLED", True)
    now = 1_800_000_000
    clock = {"now": now}
    names = [f"Item {index} (Field-Tested)" for index in range(5)]
    shared_name = "AK-47 | Asiimov (Minimal Wear)"
    xml = _sitemap_xml([name.replace(" ", "%20").replace("|", "%7C") for name in names])
    fetch = AsyncMock(return_value=xml)

    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=AsyncMock(),
        sitemap_fetch=fetch,
        sitemap_url="https://example.test/sitemap.xml",
        wall_clock=lambda: clock["now"],
        min_full_entries=1,
        min_sitemap_entries=3,
    )
    await service.persist_search_entries(
        (CatalogEntry(shared_name, "AK-47 | 二西莫夫 (略有磨损)"),)
    )

    stored = await service.sync_sitemap_if_due()
    assert stored == 5
    assert fetch.await_count == 1
    assert await service.sitemap_count() == 5

    entries = {entry.market_name: entry.display_name for entry in await service.entries()}
    # The richer live_search row is preserved verbatim: same source, real display name.
    assert entries[shared_name] == "AK-47 | 二西莫夫 (略有磨损)"
    assert entries[names[0]] == names[0]

    status = await service.status()
    assert status["sitemap_count"] == 5
    assert status["live_count"] == 1

    # A fresh window does not refetch while the success interval is unexpired.
    clock["now"] = now + 60
    assert await service.sync_sitemap_if_due() == 0
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_sitemap_integrity_gate_keeps_last_snapshot_on_shrunk_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_module, "SITEMAP_SYNC_ENABLED", True)
    now = 1_800_000_000
    clock = {"now": now}
    first = [f"Item {index}" for index in range(10)]
    responses = [
        _sitemap_xml([name.replace(" ", "%20") for name in first]),
        _sitemap_xml(["Only%20One"]),
    ]
    fetch = AsyncMock(side_effect=responses)

    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=AsyncMock(),
        sitemap_fetch=fetch,
        sitemap_url="https://example.test/sitemap.xml",
        wall_clock=lambda: clock["now"],
        min_full_entries=1,
        min_sitemap_entries=3,
    )
    assert await service.sync_sitemap_if_due() == 10

    clock["now"] = now + catalog_module.SITEMAP_REFRESH_SECONDS + 1
    assert await service.sync_sitemap_if_due() == 0
    assert fetch.await_count == 2
    assert await service.sitemap_count() == 10

    status = await service.status()
    assert status["last_error"] is None  # the open-api error key stays untouched
    assert await service._state_value("sitemap_last_error") == "integrity_gate:1<6"


@pytest.mark.asyncio
async def test_sitemap_sync_is_disabled_by_default_in_sandbox(
    tmp_path: Path,
) -> None:
    assert catalog_module.SITEMAP_SYNC_ENABLED is False
    fetch = AsyncMock()
    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=AsyncMock(),
        sitemap_fetch=fetch,
        min_full_entries=1,
    )
    assert await service.sync_sitemap_if_due() == 0
    fetch.assert_not_awaited()
    service.schedule_sitemap_sync_if_due()
    assert service._sitemap_sync_task is None


@pytest.mark.asyncio
async def test_sitemap_sync_network_failure_is_retried_after_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_module, "SITEMAP_SYNC_ENABLED", True)
    now = 1_800_000_000
    clock = {"now": now}
    fetch = AsyncMock(side_effect=RuntimeError("request failed"))

    service = MarketCatalogStore(
        path=tmp_path / "market.db",
        market_get=AsyncMock(),
        sitemap_fetch=fetch,
        sitemap_url="https://example.test/sitemap.xml",
        wall_clock=lambda: clock["now"],
        min_full_entries=1,
        min_sitemap_entries=1,
    )
    assert await service.sync_sitemap_if_due() == 0
    assert fetch.await_count == 1

    clock["now"] = now + 10
    assert await service.sync_sitemap_if_due() == 0
    assert fetch.await_count == 1  # backoff window still unexpired

    clock["now"] = now + catalog_module.SITEMAP_FAILURE_RETRY_SECONDS + 1
    assert await service.sync_sitemap_if_due() == 0
    assert fetch.await_count == 2

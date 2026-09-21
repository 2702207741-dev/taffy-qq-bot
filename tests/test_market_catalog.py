from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.services import market_catalog
from src.services.market_catalog import CatalogEntry, search_market_catalog


@pytest.mark.asyncio
async def test_live_catalog_search_parses_and_deduplicates_bounded_candidates() -> None:
    response = {
        "success": True,
        "data": [
            {
                "name": "M4A4 | 活色生香 (崭新出厂)",
                "marketHashName": "M4A4 | In Living Color (Factory New)",
            },
            {
                "name": "duplicate",
                "marketHashName": "M4A4 | In Living Color (Factory New)",
            },
            {"name": "missing market name"},
        ],
    }
    with patch.object(
        market_catalog,
        "request_json",
        AsyncMock(return_value=response),
    ) as cached_request:
        entries = await search_market_catalog("活色生香 崭新")

    assert entries == (
        CatalogEntry(
            "M4A4 | In Living Color (Factory New)",
            "M4A4 | 活色生香 (崭新出厂)",
        ),
    )
    cached_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_catalog_search_rejects_invalid_queries_without_network() -> None:
    with patch.object(
        market_catalog,
        "request_json",
        AsyncMock(),
    ) as cached_request:
        assert await search_market_catalog("") == ()
        assert await search_market_catalog("x" * 101) == ()
        assert await search_market_catalog("bad\nquery") == ()

    cached_request.assert_not_awaited()


def test_live_catalog_parser_rejects_wrong_shapes_and_invalid_rows() -> None:
    assert market_catalog._parse_entries({"success": False}) == ()
    assert market_catalog._parse_entries({"success": True, "data": "bad"}) == ()
    parsed = market_catalog._parse_entries(
        {
            "success": True,
            "data": [
                "bad",
                {"marketHashName": "", "name": "missing"},
                {"marketHashName": "A", "name": "甲"},
                {"marketHashName": "A", "name": "duplicate"},
            ],
        }
    )
    assert parsed == (CatalogEntry("A", "甲"),)


@pytest.mark.asyncio
async def test_live_catalog_cache_and_singleflight_share_one_request() -> None:
    market_catalog._CACHE.clear()
    market_catalog._INFLIGHT.clear()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def request(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {
            "success": True,
            "data": [{"marketHashName": "A", "name": "甲"}],
        }

    with patch.object(market_catalog, "request_json", request):
        first = asyncio.create_task(search_market_catalog("并发查询"))
        await started.wait()
        second = asyncio.create_task(search_market_catalog("并发查询"))
        await asyncio.sleep(0)
        release.set()
        left, right = await asyncio.gather(first, second)
        cached = await search_market_catalog("并发查询")
    assert left == right == cached == (CatalogEntry("A", "甲"),)
    assert calls == 1


@pytest.mark.asyncio
async def test_live_catalog_failure_is_short_cached_and_bounded() -> None:
    market_catalog._CACHE.clear()
    market_catalog._INFLIGHT.clear()
    request = AsyncMock(side_effect=TimeoutError)
    with patch.object(market_catalog, "request_json", request):
        assert await search_market_catalog("失败查询") == ()
        assert await search_market_catalog("失败查询") == ()
    request.assert_awaited_once()

    market_catalog._CACHE.clear()
    now = 100.0
    for index in range(501):
        market_catalog._CACHE[f"old-{index}"] = (now + index, ())
    with (
        patch.object(market_catalog.time, "monotonic", return_value=now),
        patch.object(market_catalog, "request_json", AsyncMock(return_value={"success": False})),
    ):
        await search_market_catalog("触发清理")
    assert len(market_catalog._CACHE) == 501


@pytest.mark.asyncio
async def test_live_catalog_cancelled_waiter_clears_inflight() -> None:
    market_catalog._CACHE.clear()
    market_catalog._INFLIGHT.clear()
    started = asyncio.Event()
    release = asyncio.Event()

    async def request(*_args: object, **_kwargs: object) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"success": True, "data": [{"marketHashName": "A", "name": "甲"}]}

    with patch.object(market_catalog, "request_json", request):
        waiter = asyncio.create_task(search_market_catalog("取消查询"))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # The shared fetch survives the abandoned waiter...
        assert len(market_catalog._INFLIGHT) == 1
        release.set()
        for _ in range(20):
            if not market_catalog._INFLIGHT:
                break
            await asyncio.sleep(0)

        # ...and its done callback removes it once finished.
        assert market_catalog._INFLIGHT == {}

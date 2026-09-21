from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.services import market
from src.services.http_client import SharedHTTPClient
from src.services.resilience import get_circuit_breaker


def test_market_gateway_classifies_operations_paths_and_ttls() -> None:
    assert market._market_operation("GET:broad") == "provider.market_broad"
    assert market._market_operation("/price/single") == "provider.market_price"
    assert market._market_operation("CATALOG") == "provider.market_catalog"
    assert market._market_operation("kline") == "provider.market_kline"
    assert market._market_operation("other") == "provider.market_other"
    assert market._valid_path("/open/cs2/v1/base")
    for path in ("bad", "/open/a?x=1", "/open/a#x", "/open/a\\b", "/open//a", "/open/../x"):
        assert not market._valid_path(path)
    assert market._bounded_ttl(None) == 60
    assert market._bounded_ttl(True) == 0
    assert market._bounded_ttl(float("inf")) == 0
    assert market._bounded_ttl(-1) == 0
    assert market._bounded_ttl(9_999) == 3_600


def test_market_headers_require_key_and_valid_base() -> None:
    with patch.object(market, "ST_KEY", ""):
        with pytest.raises(RuntimeError, match="API_KEY"):
            market.SteamDTService.headers()
    with patch.object(market, "ST_KEY", "key"), patch.object(market, "ST_BASE", ""):
        with pytest.raises(RuntimeError, match="BASE_URL"):
            market.SteamDTService.headers()
    with patch.object(market, "ST_KEY", "key"):
        assert market.SteamDTService.headers() == {"Authorization": "Bearer key"}


@pytest.mark.asyncio
async def test_market_cache_deep_copies_hits_refusals_and_evicts_oldest() -> None:
    service = market.SteamDTService(max_cache=10)
    request = AsyncMock(return_value={"success": True, "data": {"value": 1}})
    first = await service.cached_request("GET:broad", 60, request)
    first["data"]["value"] = 99
    second = await service.cached_request("GET:broad", 60, request)
    assert second["data"]["value"] == 1
    request.assert_awaited_once()
    refused = await service.cached_request("other", 60, AsyncMock(return_value={"success": False}))
    assert not refused["success"]
    for index in range(11):
        await service._fetch_and_cache(
            f"key-{index}",
            60,
            AsyncMock(return_value={"success": True, "index": index}),
        )
    assert len(service.cache) == 10 and "key-0" not in service.cache


@pytest.mark.asyncio
async def test_market_cache_handles_100_equal_and_100_distinct_concurrent_requests() -> None:
    service = market.SteamDTService(max_cache=256)
    calls = 0

    async def request() -> dict[str, object]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"success": True, "call": calls}

    with patch.object(market.telemetry, "record", AsyncMock()):
        equal = await asyncio.gather(
            *(service.cached_request("same", 60, request) for _ in range(100))
        )
        assert calls == 1
        assert all(result == equal[0] for result in equal)

        distinct = await asyncio.gather(
            *(service.cached_request(f"different-{index}", 60, request) for index in range(100))
        )
        assert calls == 101
        assert len(distinct) == 100
        assert len(service.inflight) == 0


@pytest.mark.asyncio
async def test_market_cache_records_timeout_error_and_canceled_fetch_cleanup() -> None:
    service = market.SteamDTService()
    with pytest.raises(TimeoutError):
        await service.cached_request("GET:broad", 0, AsyncMock(side_effect=TimeoutError))
    with pytest.raises(RuntimeError):
        await service.cached_request("GET:other", 0, AsyncMock(side_effect=RuntimeError))
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    future.cancel()
    service.inflight["x"] = future  # type: ignore[assignment]
    service._forget_inflight("x", future)  # type: ignore[arg-type]
    assert "x" not in service.inflight


@pytest.mark.asyncio
async def test_market_cache_telemetry_failure_never_changes_results_or_errors() -> None:
    service = market.SteamDTService()
    request = AsyncMock(return_value={"success": True, "value": 1})
    telemetry = AsyncMock(side_effect=RuntimeError("forced telemetry failure"))

    with patch.object(market.telemetry, "record", telemetry):
        assert await service.cached_request("GET:broad", 60, request) == {
            "success": True,
            "value": 1,
        }
        assert await service.cached_request("GET:broad", 60, request) == {
            "success": True,
            "value": 1,
        }
        with pytest.raises(ValueError, match="original market error"):
            await service.cached_request(
                "GET:other",
                0,
                AsyncMock(side_effect=ValueError("original market error")),
            )

    request.assert_awaited_once_with()
    assert telemetry.await_count == 3


@pytest.mark.asyncio
async def test_market_get_validates_size_and_maps_provider_results() -> None:
    service = market.SteamDTService()
    breaker = get_circuit_breaker("provider.market_other")
    await breaker.succeed()
    with (
        patch.object(market, "ST_KEY", "key"),
        patch.object(market, "ST_BASE", "https://example.test"),
        patch.object(market, "request_json", AsyncMock(return_value={"success": True})) as request,
    ):
        assert (await service.get("/open/cs2/v1/other", {"a": 1}, ttl=0))["success"]
    request.assert_awaited_once()
    with patch.object(market, "ST_KEY", "key"):
        too_large = await service.get("/open/cs2/v1/other", {"x": "y" * 9_000})
    assert not too_large["success"]

    await breaker.succeed()
    with (
        patch.object(market, "ST_KEY", "key"),
        patch.object(market, "request_json", AsyncMock(return_value={"success": False})),
    ):
        assert not (await market.SteamDTService().get("/open/cs2/v1/other", ttl=0))["success"]


@pytest.mark.asyncio
async def test_broad_get_falls_back_on_exception_and_failed_response() -> None:
    fallback = {
        "success": True,
        "data": {
            "broadMarketIndex": 900,
            "diffYesterdayRatio": 0,
            "updateTime": 1_800_000_000,
        },
    }
    breaker = get_circuit_breaker("provider.market_broad")
    for primary in (
        AsyncMock(side_effect=RuntimeError),
        AsyncMock(return_value={"success": False}),
    ):
        await breaker.succeed()
        with (
            patch.object(market, "ST_KEY", "key"),
            patch.object(market, "request_json", primary),
            patch.object(market, "get_fallback_broad_market", AsyncMock(return_value=fallback)),
        ):
            result = await market.SteamDTService().get(market._BROAD_INDEX_PATH, ttl=0)
        assert result == fallback


@pytest.mark.asyncio
async def test_market_post_validates_body_circuit_and_provider_failures() -> None:
    service = market.SteamDTService()
    assert not (await service.post("invalid"))["success"]
    with patch.object(market, "ST_KEY", "key"):
        assert not (await service.post("/open/cs2/v1/item", {"x": "y" * 70_000}))["success"]

    breaker = get_circuit_breaker("provider.market_post")
    await breaker.succeed()
    with (
        patch.object(market, "ST_KEY", "key"),
        patch.object(market, "request_json", AsyncMock(return_value={"success": True})),
    ):
        assert (await service.post("/open/cs2/v1/item", {"x": 1}))["success"]
    await breaker.succeed()
    with (
        patch.object(market, "ST_KEY", "key"),
        patch.object(market, "request_json", AsyncMock(side_effect=RuntimeError)),
    ):
        assert not (await market.SteamDTService().post("/open/cs2/v1/item"))["success"]
    await breaker.succeed()
    for _ in range(5):
        await breaker.fail()
    request = AsyncMock()
    with patch.object(market, "ST_KEY", "key"), patch.object(market, "request_json", request):
        assert not (await market.SteamDTService().post("/open/cs2/v1/item"))["success"]
    request.assert_not_awaited()
    await breaker.succeed()


@pytest.mark.asyncio
async def test_broad_summary_fallback_timeout_and_module_facades() -> None:
    with (
        patch.object(market, "request_json", AsyncMock(return_value={})),
        patch.object(market.market_service, "get", AsyncMock(side_effect=TimeoutError)),
    ):
        assert not (await market.fetch_broad_summary())["success"]

    fake = SimpleNamespace(
        get=AsyncMock(return_value={"get": True}),
        post=AsyncMock(return_value={"post": True}),
        cached_request=AsyncMock(return_value={"cache": True}),
        headers=lambda: {"Authorization": "Bearer hidden"},
    )
    with patch.object(market, "market_service", fake):
        assert await market.st_get("/open/x") == {"get": True}
        assert await market.st_post("/open/x") == {"post": True}
        assert await market.cached_market_request("x", 1, AsyncMock()) == {"cache": True}
        assert market.market_headers() == {"Authorization": "Bearer hidden"}

    with patch.object(
        market, "cached_market_request", AsyncMock(return_value={"ok": True})
    ) as cached:
        assert await market.get_broad_summary() == {"ok": True}
    cached.assert_awaited_once_with(
        market._BROAD_SUMMARY_CACHE_KEY,
        30.0,
        market.fetch_broad_summary,
        stale_grace=market._BROAD_SUMMARY_STALE_GRACE,
    )


@pytest.mark.asyncio
async def test_broad_summary_recovers_stale_pool_inside_the_same_command() -> None:
    clients: list[httpx.AsyncClient] = []
    payload = {
        "success": True,
        "data": {
            "broadMarketIndex": 911.67,
            "diffYesterdayRatio": -0.22,
            "diffYesterday": -2.01,
            "updateTime": int(time.time()),
        },
    }

    def stale(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("stale", request=request)

    def recovered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    def factory() -> httpx.AsyncClient:
        handler = stale if not clients else recovered
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    service = SharedHTTPClient(client_factory=factory)
    fallback = AsyncMock()
    with (
        patch.object(market, "request_json", service.request_json),
        patch.object(market.market_service, "get", fallback),
        patch("src.services.http_client.asyncio.sleep", AsyncMock()),
    ):
        result = await market.fetch_broad_summary()
    assert result == payload
    fallback.assert_not_awaited()
    assert len(clients) == 2 and clients[0].is_closed
    assert (await service.snapshot()).rebuilds == 1
    await service.close()

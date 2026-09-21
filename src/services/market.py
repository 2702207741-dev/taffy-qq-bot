"""SteamDT market gateway with bounded cache and request single-flight."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .config import bounded_base_url, bounded_env_int, env_secret
from .http_client import request_json
from .market_temperature import get_fallback_broad_market
from .market_validation import validate_broad_market_response
from .resilience import CircuitOpenError, get_circuit_breaker
from .telemetry import telemetry

logger = logging.getLogger("taffy.services.market")

ST_KEY = env_secret("STEAMDT_API_KEY")
ST_BASE = bounded_base_url("STEAMDT_BASE_URL", "https://open.steamdt.com")
MAX_CACHE = bounded_env_int("MARKET_CACHE_SIZE", 500, 10, 10_000)

_GENERIC_ERROR = {"success": False, "errorMsg": "市场数据暂时不可用"}
_INVALID_PATH = {"success": False, "errorMsg": "无效的市场接口路径"}
_BROAD_INDEX_PATH = "/open/cs2/broad/v1/index"
_BROAD_SUMMARY_URL = "https://www.steamdt.com/api/index/statistics/v1/summary"
_BROAD_SUMMARY_CACHE_KEY = "GET:public-broad-summary"
_BROAD_SUMMARY_STALE_GRACE = float(
    bounded_env_int("TAFFY_BROAD_SUMMARY_STALE_GRACE_SECONDS", 120, 0, 900)
)


def _market_operation(cache_key: str) -> str:
    if "broad" in cache_key:
        return "provider.market_broad"
    if "/price/" in cache_key:
        return "provider.market_price"
    if "CATALOG" in cache_key or "/v1/base" in cache_key:
        return "provider.market_catalog"
    if "kline" in cache_key:
        return "provider.market_kline"
    return "provider.market_other"


def _valid_path(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("/open/") or len(path) > 256:
        return False
    if any(marker in path for marker in ("?", "#", "\\", "//")):
        return False
    return all(segment not in {".", ".."} for segment in path.split("/"))


def _bounded_ttl(value: float | None) -> float:
    if value is None:
        return 60.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    result = float(value)
    if not math.isfinite(result):
        return 0.0
    return min(3_600.0, max(0.0, result))


async def _record_market_telemetry(
    operation: str,
    outcome: str,
    started: float,
    *,
    cache_hit: bool = False,
) -> None:
    """Keep optional metrics from changing market availability or errors."""

    try:
        await telemetry.record(
            operation,
            outcome,
            (time.perf_counter() - started) * 1000,
            cache_hit=cache_hit,
        )
    except Exception as exc:
        logger.warning("Market telemetry unavailable error=%s", type(exc).__name__)


class SteamDTService:
    """Market API service safe for concurrent callers in one worker."""

    def __init__(self, *, max_cache: int = MAX_CACHE) -> None:
        self.max_cache = max(10, min(max_cache, 10_000))
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.state_lock = asyncio.Lock()
        self.get_bulkhead = asyncio.Semaphore(10)
        self.post_bulkhead = asyncio.Semaphore(5)

    @staticmethod
    def headers() -> dict[str, str]:
        if not ST_KEY:
            raise RuntimeError("STEAMDT_API_KEY is not configured")
        if not ST_BASE:
            raise RuntimeError("STEAMDT_BASE_URL is invalid")
        return {"Authorization": f"Bearer {ST_KEY}"}

    async def cached_request(
        self,
        cache_key: str,
        ttl: float,
        request: Callable[[], Awaitable[dict[str, Any]]],
        *,
        stale_grace: float | None = None,
    ) -> dict[str, Any]:
        """Coalesce equal misses and shield the shared fetch from waiter cancellation.

        With ``stale_grace`` set, an expired entry is still served while its
        age stays within the grace window; one background refresh then
        updates the cache without blocking the caller.  That refresh failing
        keeps the stale value (failed answers are never cached), and beyond
        the grace window the call blocks on a fresh fetch exactly as before.
        """

        started = time.perf_counter()
        operation = _market_operation(cache_key)
        now = time.monotonic()
        cached_result: dict[str, Any] | None = None
        stale_result: dict[str, Any] | None = None
        async with self.state_lock:
            cached = self.cache.get(cache_key)
            if cached and now - cached[0] < ttl:
                # Responses are shared read-only: every consumer passes them
                # into pure validators/renderers, and the cache entry was
                # written as an independent deepcopy in _fetch_and_cache.
                cached_result = cached[1]
                task = None
            elif (
                cached
                and stale_grace is not None
                and stale_grace > 0
                and now - cached[0] < ttl + stale_grace
            ):
                stale_result = cached[1]
                task = self.inflight.get(cache_key)
                if task is None:
                    task = self._start_fetch(cache_key, ttl, request)
            else:
                task = self.inflight.get(cache_key)
                if task is None:
                    task = self._start_fetch(cache_key, ttl, request)
        if cached_result is not None:
            await _record_market_telemetry(operation, "success", started, cache_hit=True)
            return cached_result
        if stale_result is not None:
            await _record_market_telemetry(operation, "success", started, cache_hit=True)
            logger.debug("Served stale-within-grace market cache entry key=%s", cache_key)
            return stale_result
        assert task is not None
        try:
            result = await asyncio.shield(task)
        except TimeoutError:
            await _record_market_telemetry(operation, "timeout", started)
            raise
        except Exception:
            await _record_market_telemetry(operation, "error", started)
            raise
        await _record_market_telemetry(
            operation,
            "success" if result.get("success") else "refused",
            started,
        )
        # ``result`` is the fetch object; the cache already holds its own
        # deepcopy, so handing the original to the caller copies nothing.
        return result

    def _start_fetch(
        self,
        cache_key: str,
        ttl: float,
        request: Callable[[], Awaitable[dict[str, Any]]],
    ) -> asyncio.Task[dict[str, Any]]:
        task = asyncio.create_task(self._fetch_and_cache(cache_key, ttl, request))
        self.inflight[cache_key] = task

        def forget(completed: asyncio.Future[dict[str, Any]]) -> None:
            self._forget_inflight(cache_key, completed)

        task.add_done_callback(forget)
        return task

    def cache_age(self, cache_key: str) -> float | None:
        """Age of the cached entry in seconds, or None when not cached."""

        cached = self.cache.get(cache_key)
        if cached is None:
            return None
        return max(0.0, time.monotonic() - cached[0])

    def _forget_inflight(
        self,
        cache_key: str,
        completed: asyncio.Future[dict[str, Any]],
    ) -> None:
        if self.inflight.get(cache_key) is completed:
            self.inflight.pop(cache_key, None)
        # A cancelled waiter deliberately does not cancel the shared fetch.
        # Retrieve its eventual exception here so an abandoned failed request
        # cannot produce an unhandled-task warning during shutdown.
        try:
            completed.exception()
        except asyncio.CancelledError:
            pass

    async def _fetch_and_cache(
        self,
        cache_key: str,
        ttl: float,
        request: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        data = await request()
        if data.get("success") and ttl > 0:
            async with self.state_lock:
                if len(self.cache) >= self.max_cache and cache_key not in self.cache:
                    oldest = min(self.cache, key=lambda key: self.cache[key][0])
                    self.cache.pop(oldest, None)
                self.cache[cache_key] = (time.monotonic(), copy.deepcopy(data))
        return data

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        if not _valid_path(path):
            return dict(_INVALID_PATH)
        try:
            serialized = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, default=str)
            if len(serialized) > 8_192:
                raise ValueError("market request parameters are too large")
            cache_key = f"GET:{path}:{serialized}"
            headers = self.headers()
            breaker = get_circuit_breaker(_market_operation(cache_key))

            async def fetch() -> dict[str, Any]:
                try:
                    await breaker.acquire()
                except CircuitOpenError:
                    return dict(_GENERIC_ERROR)
                try:
                    async with self.get_bulkhead:
                        data = await request_json(
                            "GET",
                            f"{ST_BASE}{path}",
                            params=params,
                            headers=headers,
                            total_timeout=45.0,
                        )
                except Exception:
                    await breaker.fail()
                    if path == _BROAD_INDEX_PATH:
                        fallback = await get_fallback_broad_market()
                        if fallback.get("success"):
                            logger.warning(
                                "Broad market primary request failed; using validated daily fallback"
                            )
                            return fallback
                    raise
                if data.get("success"):
                    await breaker.succeed()
                else:
                    await breaker.fail()
                if data.get("success") or path != _BROAD_INDEX_PATH:
                    return data
                fallback = await get_fallback_broad_market()
                if fallback.get("success"):
                    logger.warning(
                        "Broad market primary response failed; using validated daily fallback"
                    )
                    return fallback
                return data

            return await self.cached_request(cache_key, _bounded_ttl(ttl), fetch)
        except Exception as exc:
            logger.warning("SteamDT GET failed path=%s error=%s", path, type(exc).__name__)
            return dict(_GENERIC_ERROR)

    async def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not _valid_path(path):
            return dict(_INVALID_PATH)
        try:
            encoded = json.dumps(body or {}, ensure_ascii=False, default=str)
            if len(encoded) > 65_536:
                raise ValueError("market request body is too large")
            breaker = get_circuit_breaker("provider.market_post")
            try:
                await breaker.acquire()
            except CircuitOpenError:
                return dict(_GENERIC_ERROR)
            try:
                async with self.post_bulkhead:
                    result = await request_json(
                        "POST",
                        f"{ST_BASE}{path}",
                        json=body or {},
                        headers=self.headers(),
                        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
                        total_timeout=45.0,
                    )
            except Exception:
                await breaker.fail()
                raise
            if result.get("success"):
                await breaker.succeed()
            else:
                await breaker.fail()
            return result
        except Exception as exc:
            logger.warning("SteamDT POST failed path=%s error=%s", path, type(exc).__name__)
            return dict(_GENERIC_ERROR)


market_service = SteamDTService()


async def st_get(
    path: str,
    params: dict[str, Any] | None = None,
    ttl: float | None = None,
) -> dict[str, Any]:
    return await market_service.get(path, params, ttl)


async def st_post(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await market_service.post(path, body)


async def cached_market_request(
    cache_key: str,
    ttl: float,
    request: Callable[[], Awaitable[dict[str, Any]]],
    *,
    stale_grace: float | None = None,
) -> dict[str, Any]:
    return await market_service.cached_request(cache_key, ttl, request, stale_grace=stale_grace)


def market_cache_age(cache_key: str) -> float | None:
    """Age of a cached market response in seconds, or None when not cached."""

    return market_service.cache_age(cache_key)


async def fetch_broad_summary() -> dict[str, Any]:
    """Fetch the fast public index summary, falling back to the legacy gateway."""

    try:
        data = await request_json(
            "GET",
            _BROAD_SUMMARY_URL,
            params={"timestamp": int(time.time() * 1000)},
            attempts=1,
            timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0),
            total_timeout=4.0,
        )
        validate_broad_market_response(data)
        return data
    except Exception as exc:
        logger.warning(
            "Fast broad summary failed error=%s; using legacy fallback", type(exc).__name__
        )

    try:
        async with asyncio.timeout(4.0):
            return await market_service.get(_BROAD_INDEX_PATH, ttl=30)
    except TimeoutError:
        logger.warning("Legacy broad fallback exceeded its response deadline")
        return dict(_GENERIC_ERROR)


async def get_broad_summary() -> dict[str, Any]:
    """Return one validated summary with a 30-second cache and single-flight.

    Within the bounded stale grace window an expired entry is still served
    while one background refresh runs; the window never reaches the broad
    freshness limit enforced by ``market_validation``, so served data stays
    honest about its age.
    """

    return await cached_market_request(
        _BROAD_SUMMARY_CACHE_KEY,
        30.0,
        fetch_broad_summary,
        stale_grace=_BROAD_SUMMARY_STALE_GRACE,
    )


def market_headers() -> dict[str, str]:
    return market_service.headers()


__all__ = [
    "MAX_CACHE",
    "ST_BASE",
    "ST_KEY",
    "SteamDTService",
    "cached_market_request",
    "fetch_broad_summary",
    "get_broad_summary",
    "market_cache_age",
    "market_headers",
    "market_service",
    "st_get",
    "st_post",
]

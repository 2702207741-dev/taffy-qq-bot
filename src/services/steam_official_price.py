"""Valve official market price lookups with cache, breaker and degradation.

The Steam Community priceoverview endpoint is free but strictly rate-limited
(~200 requests / 5 minutes per IP), so every lookup goes through a 300-second
in-memory LRU cache, a shared circuit breaker and a two-flight semaphore.  A
failed or rate-limited lookup never silently substitutes a SteamDT quote:
callers receive ``None`` and render their own degradation copy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx

from .http_client import request_json
from .resilience import CircuitOpenError, get_circuit_breaker

logger = logging.getLogger("taffy.steam_official_price")

_OFFICIAL_URL = "https://steamcommunity.com/market/priceoverview/"
_CACHE_TTL_SECONDS = 300
_REQUEST_TIMEOUT_SECONDS = 5.0
_CURRENCY_CNY = 23
_BREAKER_NAME = "steam-official-price"
_MAX_CACHE_ENTRIES = 512

_price_cache: OrderedDict[str, tuple[float, OfficialPrice]] = OrderedDict()
_cache_lock = asyncio.Lock()
_semaphore = asyncio.Semaphore(2)


@dataclass(frozen=True, slots=True)
class OfficialPrice:
    market_hash_name: str
    lowest_price: str
    median_price: str
    volume: str


def _parse_payload(name: str, payload: dict[str, Any]) -> OfficialPrice | None:
    if payload.get("success") is not True:
        return None
    return OfficialPrice(
        market_hash_name=name,
        lowest_price=str(payload.get("lowest_price") or ""),
        median_price=str(payload.get("median_price") or ""),
        volume=str(payload.get("volume") or ""),
    )


def _counts_against_breaker(error: Exception) -> bool:
    """Only transport failures and rate-limit/5xx answers trip the breaker.

    The shared client surfaces its final failure reason as ``RuntimeError``
    messages: ``HTTP <code>``, the transport exception class name, or one of a
    few body-shape markers.  A business answer such as ``success=false`` (item
    not tracked by Valve) parses fine and never reaches this function.
    """

    message = str(error)
    if message.startswith("HTTP "):
        code = message[5:].strip()
        return code in {"408", "425", "429"} or code.startswith("5")
    if message in {"JSONDecodeError", "ValueError"}:
        return False
    return True


async def get_official_price(market_hash_name: str) -> OfficialPrice | None:
    """Return the cached or freshly fetched official price, ``None`` on failure.

    Cache hit does not consult the breaker; only transport-level fetch failures
    and 429/5xx responses count against it, while an open circuit returns
    ``None`` immediately.
    """

    name = str(market_hash_name).strip()
    if not name:
        return None

    async with _cache_lock:
        cached = _price_cache.get(name)
        if cached is not None:
            _price_cache.move_to_end(name)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    breaker = get_circuit_breaker(_BREAKER_NAME)
    try:
        await breaker.acquire()
    except CircuitOpenError:
        logger.warning("Official price breaker is open; degrading name=%s", name[:40])
        return None

    try:
        async with _semaphore:
            payload = await request_json(
                "GET",
                _OFFICIAL_URL,
                params={
                    "appid": 730,
                    "currency": _CURRENCY_CNY,
                    "market_hash_name": name,
                },
                attempts=1,
                timeout=httpx.Timeout(
                    connect=2.0,
                    read=_REQUEST_TIMEOUT_SECONDS,
                    write=2.0,
                    pool=2.0,
                ),
                total_timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        logger.warning(
            "Official price fetch failed name=%s error=%s",
            name[:40],
            type(exc).__name__,
        )
        if isinstance(exc, asyncio.CancelledError):
            raise
        if _counts_against_breaker(exc):
            await breaker.fail()
        return None

    price = _parse_payload(name, payload)
    if price is None:
        # Business answer (unknown item / rate-limited body): do not punish
        # the breaker for Valve simply not having the item.
        return None
    await breaker.succeed()
    async with _cache_lock:
        _price_cache[name] = (time.monotonic() + _CACHE_TTL_SECONDS, price)
        _price_cache.move_to_end(name)
        while len(_price_cache) > _MAX_CACHE_ENTRIES:
            _price_cache.popitem(last=False)
    return price


__all__ = ["OfficialPrice", "get_official_price"]

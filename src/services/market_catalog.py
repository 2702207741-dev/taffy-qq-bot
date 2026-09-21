"""Bounded on-demand lookup for names absent from the durable market catalog."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .http_client import request_json

_BASE_URL = "https://www.steamdt.com/api"
_SEARCH_PATH = "/user/skin/v1/auto-completion"
_SUCCESS_CACHE_SECONDS = 60 * 60
_FAILURE_CACHE_SECONDS = 2 * 60
_MAX_RESULTS = 200
_CACHE: dict[str, tuple[float, tuple[CatalogEntry, ...]]] = {}
_INFLIGHT: dict[str, asyncio.Task[tuple[CatalogEntry, ...]]] = {}
_STATE_LOCK = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    market_name: str
    display_name: str


def _parse_entries(response: Mapping[str, Any]) -> tuple[CatalogEntry, ...]:
    raw_entries = response.get("data") if response.get("success") else None
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        return ()

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for raw in raw_entries[:_MAX_RESULTS]:
        if not isinstance(raw, Mapping):
            continue
        market_name = str(raw.get("marketHashName", "")).strip()
        display_name = str(raw.get("name", "")).strip()
        if (
            not market_name
            or not display_name
            or market_name in seen
            or len(market_name) > 240
            or len(display_name) > 300
        ):
            continue
        seen.add(market_name)
        entries.append(CatalogEntry(market_name, display_name))
    return tuple(entries)


async def search_market_catalog(query: str) -> tuple[CatalogEntry, ...]:
    """Return bounded verified candidates for one user query, or no candidates."""

    clean_query = query.strip()
    if (
        not clean_query
        or len(clean_query) > 100
        or any(ord(character) < 32 for character in clean_query)
    ):
        return ()

    cache_key = clean_query.casefold()
    now = time.monotonic()
    async with _STATE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        task = _INFLIGHT.get(cache_key)
        if task is None:
            task = asyncio.create_task(_fetch_entries(clean_query), name="market-catalog-search")
            _INFLIGHT[cache_key] = task

            def forget(completed: asyncio.Future[tuple[CatalogEntry, ...]]) -> None:
                # A cancelled waiter must not leak the completed task; the
                # done callback runs even when every waiter abandoned the
                # shielded await.
                if _INFLIGHT.get(cache_key) is completed:
                    _INFLIGHT.pop(cache_key, None)

            task.add_done_callback(forget)
    entries = await asyncio.shield(task)
    ttl = _SUCCESS_CACHE_SECONDS if entries else _FAILURE_CACHE_SECONDS
    async with _STATE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + ttl, entries)
        if len(_CACHE) > 500:
            oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
            _CACHE.pop(oldest, None)
    return entries


async def _fetch_entries(clean_query: str) -> tuple[CatalogEntry, ...]:
    try:
        response = await request_json(
            "GET",
            f"{_BASE_URL}{_SEARCH_PATH}",
            params={"content": clean_query},
            attempts=2,
            total_timeout=12.0,
        )
    except Exception:
        return ()
    return _parse_entries(response)


__all__ = ["CatalogEntry", "search_market_catalog"]

"""CSQAQ open-platform gateway: cross-platform quotes, rent yield and supply.

This is a pure complement to the SteamDT market gateway — it only answers
questions SteamDT cannot (BUFF/悠悠有品/C5/IGXE/ECO platform prices, rent
yields, global supply counts).  Everything here is fail-closed: a missing
token, an open circuit, a rate-limit answer or a malformed payload is
reported as "no data" and never guessed around.  The documented upstream
contract allows one request per second per IP, so responses and resolved
good ids are cached aggressively.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from src.state.csqaq_repository import CachedGoodId

from .config import bounded_base_url, bounded_env_float, bounded_env_int, env_secret
from .http_client import request_json
from .resilience import CircuitOpenError, get_circuit_breaker
from .telemetry import telemetry
from .tz import configured_timezone

logger = logging.getLogger("taffy.services.csqaq")

CSQAQ_KEY = env_secret("CSQAQ_API_KEY")
CSQAQ_BASE = bounded_base_url("CSQAQ_BASE_URL", "https://api.csqaq.com")

_SUGGEST_PATH = "/api/v1/search/suggest"
_GOOD_ID_PATH = "/api/v1/info/get_good_id"
_GOOD_DETAIL_PATH = "/api/v1/info/good"

_MIN_INTERVAL = bounded_env_float("CSQAQ_MIN_INTERVAL_SECONDS", 1.1, 0.05, 60.0)
_DETAIL_TTL = float(bounded_env_int("CSQAQ_DETAIL_TTL_SECONDS", 600, 30, 3_600))
_GOOD_ID_MAX_AGE = bounded_env_int("CSQAQ_GOOD_ID_MAX_AGE_SECONDS", 2_592_000, 3_600, 31_622_400)
_MAX_DETAIL_CACHE = 512
_MAX_ITEMS = 20

_UNCONFIGURED_ERROR = {"success": False, "errorMsg": "CSQAQ 数据源未配置", "unconfigured": True}
_GENERIC_ERROR = {"success": False, "errorMsg": "CSQAQ 数据暂时不可用"}

#: Transport seam for tests: ``(method, url, headers, params, json) -> envelope``.
CsqaqTransport = Callable[
    [str, str, dict[str, str], dict[str, Any] | None, dict[str, Any] | None],
    Awaitable[dict[str, Any]],
]


class GoodIdCache(Protocol):
    """Minimal persistence contract for resolved CSQAQ good ids."""

    async def get(self, market_hash_name: str, *, max_age_seconds: int) -> CachedGoodId | None:
        """Return a mapping only while younger than the age cap."""

    async def put(
        self,
        market_hash_name: str,
        *,
        good_id: int,
        csqaq_name: str,
        resolved_at: int | None = None,
    ) -> CachedGoodId:
        """Insert or refresh one mapping."""


@dataclass(frozen=True, slots=True)
class CsqaqPlatformQuote:
    """One platform's sell/buy snapshot as returned by CSQAQ."""

    platform: str
    sell_price: float | None
    sell_num: int | None
    buy_price: float | None
    buy_num: int | None


@dataclass(frozen=True, slots=True)
class CsqaqRentInfo:
    """悠悠有品 rent snapshot; C5/IGXE rent fields are not modelled yet."""

    short_price: float | None
    long_price: float | None
    short_annual_yield: float | None
    long_annual_yield: float | None
    leasing_num: int | None


@dataclass(frozen=True, slots=True)
class CsqaqGoodDetail:
    """Validated, typed projection of ``GET /api/v1/info/good``."""

    good_id: int
    market_hash_name: str | None
    name_cn: str | None
    supply_count: int | None
    data_age_seconds: float | None
    quotes: tuple[CsqaqPlatformQuote, ...]
    rent: CsqaqRentInfo | None
    price_change_7d: float | None
    price_change_30d: float | None
    steam_turnover_number: int | None = None
    steam_turnover_avg_price: float | None = None
    turnover_period_at: float | None = None


def _opt_float(value: object, *, minimum: float | None = 0.0) -> float | None:
    """Coerce one optional numeric field; anything malformed becomes None."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def _opt_int(value: object, *, minimum: int = 0) -> int | None:
    """Coerce one optional integer field; anything malformed becomes None.

    CSQAQ sometimes returns numeric ids as JSON strings (``"6796"``), so a
    pure digit string is accepted; booleans and anything else stay None.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        value = int(text)
    if not isinstance(value, int):
        return None
    if value < minimum:
        return None
    return value


def _opt_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maximum:
        return None
    return text


def _parse_csqaq_time(value: object) -> float | None:
    """Parse an epoch or a Beijing-time date string (ISO-T or space form)."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number < 0:
            return None
        return number if number < 1e12 else number / 1000.0
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        number = float(text)
        return number if number < 1e12 else number / 1000.0
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=configured_timezone())
        return parsed.timestamp()
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=configured_timezone())
        except ValueError:
            continue
        return parsed.timestamp()
    return None


def _platform_quote(info: Mapping[str, Any], platform: str) -> CsqaqPlatformQuote | None:
    sell_price = _opt_float(info.get(f"{platform}_sell_price"))
    sell_num = _opt_int(info.get(f"{platform}_sell_num"))
    buy_price = _opt_float(info.get(f"{platform}_buy_price"))
    buy_num = _opt_int(info.get(f"{platform}_buy_num"))
    if sell_price is None and sell_num is None and buy_price is None and buy_num is None:
        return None
    return CsqaqPlatformQuote(
        platform=platform,
        sell_price=sell_price,
        sell_num=sell_num,
        buy_price=buy_price,
        buy_num=buy_num,
    )


_PLATFORM_PREFIXES = ("steam", "buff", "yyyp", "c5", "igxe", "eco")


def parse_good_detail(good_id: int, data: Mapping[str, Any]) -> CsqaqGoodDetail | None:
    """Project the raw ``info/good`` payload; None on any structural mismatch."""

    if not isinstance(data, Mapping):
        return None
    info = data.get("goods_info")
    if not isinstance(info, Mapping):
        return None
    resolved_id = _opt_int(info.get("id"), minimum=1)
    if resolved_id is None or resolved_id != good_id:
        return None

    quotes = tuple(
        quote
        for prefix in _PLATFORM_PREFIXES
        if (quote := _platform_quote(info, prefix)) is not None
    )
    short_price = _opt_float(info.get("yyyp_lease_price"))
    long_price = _opt_float(info.get("yyyp_long_lease_price"))
    short_annual = _opt_float(info.get("yyyp_lease_annual"), minimum=None)
    long_annual = _opt_float(info.get("yyyp_long_lease_annual"), minimum=None)
    leasing_num = _opt_int(info.get("yyyp_lease_num"))
    rent: CsqaqRentInfo | None = None
    if any(
        field is not None
        for field in (short_price, long_price, short_annual, long_annual, leasing_num)
    ):
        rent = CsqaqRentInfo(
            short_price=short_price,
            long_price=long_price,
            short_annual_yield=short_annual,
            long_annual_yield=long_annual,
            leasing_num=leasing_num,
        )

    updated_epoch = _parse_csqaq_time(info.get("updated_at"))
    data_age = max(0.0, time.time() - updated_epoch) if updated_epoch is not None else None
    return CsqaqGoodDetail(
        good_id=good_id,
        market_hash_name=_opt_text(info.get("market_hash_name"), maximum=256),
        name_cn=_opt_text(info.get("name"), maximum=200),
        supply_count=_opt_int(info.get("statistic")),
        data_age_seconds=data_age,
        quotes=quotes,
        rent=rent,
        price_change_7d=_opt_float(info.get("sell_price_rate_7"), minimum=None),
        price_change_30d=_opt_float(info.get("sell_price_rate_30"), minimum=None),
        steam_turnover_number=_opt_int(info.get("turnover_number")),
        steam_turnover_avg_price=_opt_float(info.get("turnover_avg_price")),
        turnover_period_at=_parse_csqaq_time(info.get("period_at")),
    )


def parse_suggest_items(data: object) -> tuple[tuple[int, str], ...]:
    """Project ``search/suggest`` rows into ``(good_id, label)`` pairs."""

    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return ()
    items: list[tuple[int, str]] = []
    for entry in data[:_MAX_ITEMS]:
        if not isinstance(entry, Mapping):
            continue
        identifier = _opt_int(entry.get("id"), minimum=1)
        label = _opt_text(entry.get("value"), maximum=200)
        if identifier is not None and label is not None:
            items.append((identifier, label))
    return tuple(items)


def parse_good_id_items(data: object) -> tuple[tuple[int, str], ...]:
    """Project ``info/get_good_id`` rows (mapping or list) into id/name pairs."""

    container: object = data
    if isinstance(data, Mapping):
        container = data.get("data")
    if isinstance(container, Mapping):
        container = list(container.values())
    if not isinstance(container, Sequence) or isinstance(container, (str, bytes)):
        return ()
    items: list[tuple[int, str]] = []
    for entry in container[:_MAX_ITEMS]:
        if not isinstance(entry, Mapping):
            continue
        identifier = _opt_int(entry.get("id"), minimum=1)
        label = _opt_text(entry.get("name"), maximum=200)
        if identifier is not None and label is not None:
            items.append((identifier, label))
    return tuple(items)


class _RateGate:
    """Serialise upstream calls behind the documented one-per-second limit.

    The interval is measured from the previous request's *completion*, not
    from when its slot was reserved: a slow TLS handshake otherwise lets the
    follow-up request land right behind the previous one inside the same
    upstream window and draw a 429.  The lock is held for the whole request,
    so concurrent callers simply queue behind each other.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._ready_at = 0.0

    async def run(self, request: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        async with self._lock:
            wait = self._ready_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await request()
            finally:
                self._ready_at = time.monotonic() + self._min_interval


def _normalize_envelope(raw: object) -> dict[str, Any]:
    """Map the ``{code, msg, data}`` contract onto the internal shape."""

    if not isinstance(raw, Mapping):
        return dict(_GENERIC_ERROR)
    code = raw.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return dict(_GENERIC_ERROR)
    if code != 0 and not 200 <= code < 300:
        message = raw.get("msg") if isinstance(raw.get("msg"), str) else ""
        return {"success": False, "code": code, "errorMsg": message or "CSQAQ 数据暂时不可用"}
    data = raw.get("data")
    return {"success": True, "code": code, "data": data}


class CsqaqService:
    """CSQAQ gateway safe for concurrent callers in one worker."""

    def __init__(
        self,
        *,
        transport: CsqaqTransport | None = None,
        min_interval: float | None = None,
        detail_ttl: float | None = None,
        good_id_max_age: int | None = None,
        good_id_cache: GoodIdCache | None = None,
    ) -> None:
        self._transport = transport or _default_transport
        self._gate = _RateGate(min_interval if min_interval is not None else _MIN_INTERVAL)
        self._detail_ttl = detail_ttl if detail_ttl is not None else _DETAIL_TTL
        self._good_id_max_age = good_id_max_age if good_id_max_age is not None else _GOOD_ID_MAX_AGE
        self._good_id_cache = good_id_cache
        self._detail_cache: dict[int, tuple[float, CsqaqGoodDetail]] = {}
        self._detail_inflight: dict[int, asyncio.Task[dict[str, Any]]] = {}
        self._detail_lock = asyncio.Lock()

    @staticmethod
    def configured() -> bool:
        return bool(CSQAQ_KEY) and bool(CSQAQ_BASE)

    def attach_good_id_cache(self, cache: GoodIdCache) -> None:
        """Wire the durable good_id cache (called from the composition root)."""

        self._good_id_cache = cache

    @staticmethod
    def headers() -> dict[str, str]:
        if not CSQAQ_KEY:
            raise RuntimeError("CSQAQ_API_KEY is not configured")
        if not CSQAQ_BASE:
            raise RuntimeError("CSQAQ_BASE_URL is invalid")
        return {"ApiToken": CSQAQ_KEY}

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one upstream call behind breaker, rate gate and normalization."""

        operation = (
            "provider.csqaq_detail" if path == _GOOD_DETAIL_PATH else "provider.csqaq_search"
        )
        breaker = get_circuit_breaker(operation)
        started = time.perf_counter()

        try:
            try:
                await breaker.acquire()
            except CircuitOpenError:
                await _record_csqaq_telemetry(operation, "refused", started)
                return dict(_GENERIC_ERROR)
            try:
                raw = await self._gate.run(
                    lambda: self._transport(
                        method, f"{CSQAQ_BASE}{path}", self.headers(), params, json_body
                    )
                )
            except Exception:
                await breaker.fail()
                await _record_csqaq_telemetry(operation, "error", started)
                raise
            normalized = _normalize_envelope(raw)
            if normalized["success"]:
                await breaker.succeed()
                await _record_csqaq_telemetry(operation, "success", started)
            else:
                await breaker.fail()
                await _record_csqaq_telemetry(operation, "refused", started)
            return normalized
        except Exception as exc:
            logger.warning("CSQAQ %s failed error=%s", path, type(exc).__name__)
            return dict(_GENERIC_ERROR)

    async def suggest(self, text: str) -> dict[str, Any]:
        """Fuzzy search; returns ``items`` as ``(good_id, label)`` pairs."""

        query = text.strip()
        if not query or not self.configured():
            return dict(_UNCONFIGURED_ERROR)
        response = await self._call("GET", _SUGGEST_PATH, params={"text": query})
        if not response.get("success"):
            return response
        return {**response, "items": parse_suggest_items(response.get("data"))}

    async def exact_search(self, text: str) -> dict[str, Any]:
        """Exact search; returns ``items`` as ``(good_id, name)`` pairs."""

        query = text.strip()
        if not query or not self.configured():
            return dict(_UNCONFIGURED_ERROR)
        body = {"page_index": 1, "page_size": _MAX_ITEMS, "search": query}
        response = await self._call("POST", _GOOD_ID_PATH, json_body=body)
        if not response.get("success"):
            return response
        return {**response, "items": parse_good_id_items(response.get("data"))}

    async def good_detail(self, good_id: int) -> dict[str, Any]:
        """Typed item detail with a bounded TTL cache and single-flight."""

        if isinstance(good_id, bool) or not isinstance(good_id, int) or good_id <= 0:
            return dict(_GENERIC_ERROR)
        if not self.configured():
            return dict(_UNCONFIGURED_ERROR)
        async with self._detail_lock:
            cached = self._detail_cache.get(good_id)
            if cached and time.monotonic() - cached[0] < self._detail_ttl:
                return {"success": True, "detail": cached[1]}
            inflight = self._detail_inflight.get(good_id)
            if inflight is None:
                inflight = asyncio.create_task(self._fetch_detail(good_id))
                self._detail_inflight[good_id] = inflight

                def forget(completed: asyncio.Future[dict[str, Any]]) -> None:
                    if self._detail_inflight.get(good_id) is completed:
                        self._detail_inflight.pop(good_id, None)
                    try:
                        completed.exception()
                    except asyncio.CancelledError:
                        pass

                inflight.add_done_callback(forget)
        try:
            return await asyncio.shield(inflight)
        except Exception:
            return dict(_GENERIC_ERROR)

    async def _fetch_detail(self, good_id: int) -> dict[str, Any]:
        response = await self._call("GET", _GOOD_DETAIL_PATH, params={"id": good_id})
        if not response.get("success"):
            # A failed or malformed answer is never cached.
            return response
        detail = parse_good_detail(good_id, response.get("data") or {})
        if detail is None:
            return {
                "success": False,
                "errorMsg": "CSQAQ 响应结构不符合预期",
            }
        async with self._detail_lock:
            if len(self._detail_cache) >= _MAX_DETAIL_CACHE and good_id not in self._detail_cache:
                oldest = min(self._detail_cache, key=lambda key: self._detail_cache[key][0])
                self._detail_cache.pop(oldest, None)
            self._detail_cache[good_id] = (time.monotonic(), detail)
        return {"success": True, "detail": detail}

    async def resolve_good_id(
        self,
        query: str,
        *,
        market_hash_name: str | None = None,
    ) -> CachedGoodId | None:
        """Resolve one mapping cache-first; only confident matches are stored.

        A candidate counts as confident when its CSQAQ name equals the query
        or the expected market_hash_name (case-insensitive).  Anything else
        returns None instead of guessing.
        """

        cache_key = (market_hash_name or query).strip()
        if not cache_key:
            return None
        cache = self._good_id_cache
        if cache is not None:
            cached = await cache.get(cache_key, max_age_seconds=self._good_id_max_age)
            if cached is not None:
                return cached

        expected = (market_hash_name or "").strip().casefold() or None
        wanted = query.strip().casefold()
        response = await self.exact_search(query)
        candidates: tuple[tuple[int, str], ...] = tuple(response.get("items") or ())
        if not candidates:
            response = await self.suggest(query)
            candidates = tuple(response.get("items") or ())
        for identifier, label in candidates:
            folded = label.casefold()
            if folded == wanted or (expected is not None and folded == expected):
                resolved = CachedGoodId(
                    good_id=identifier,
                    csqaq_name=label,
                    resolved_at=int(time.time()),
                )
                if cache is not None:
                    await cache.put(
                        cache_key,
                        good_id=identifier,
                        csqaq_name=label,
                        resolved_at=resolved.resolved_at,
                    )
                return resolved
        return None

    async def verified_detail(
        self,
        query: str,
        *,
        market_hash_name: str,
        max_candidates: int = 3,
    ) -> dict[str, Any]:
        """Return one ``CsqaqGoodDetail`` proven to be the requested item.

        CSQAQ names items with Steam's Chinese market names while this bot
        resolves English hash names, so string matching alone cannot close
        the gap.  Fuzzy suggestions are instead *verified* one by one: each
        candidate's detail payload carries the authoritative
        ``market_hash_name``, and only an exact (case-insensitive) match is
        returned.  Anything else reports "unconfirmed" instead of guessing.
        """

        name = market_hash_name.strip()
        if not name:
            return dict(_GENERIC_ERROR)
        if not self.configured():
            return dict(_UNCONFIGURED_ERROR)
        expected = name.casefold()
        cache = self._good_id_cache

        if cache is not None:
            cached = await cache.get(name, max_age_seconds=self._good_id_max_age)
            if cached is not None:
                response = await self.good_detail(cached.good_id)
                detail = response.get("detail") if response.get("success") else None
                if detail is not None and (detail.market_hash_name or "").casefold() == expected:
                    return {"success": True, "detail": detail}

        response = await self.suggest(query)
        candidates: tuple[tuple[int, str], ...] = tuple(response.get("items") or ())
        for identifier, _label in candidates[: max(1, min(int(max_candidates), _MAX_ITEMS))]:
            detail_response = await self.good_detail(identifier)
            detail = detail_response.get("detail") if detail_response.get("success") else None
            if detail is None or (detail.market_hash_name or "").casefold() != expected:
                continue
            if cache is not None:
                await cache.put(
                    name,
                    good_id=detail.good_id,
                    csqaq_name=detail.name_cn or _label,
                    resolved_at=int(time.time()),
                )
            return {"success": True, "detail": detail}
        return {
            "success": False,
            "errorMsg": "CSQAQ 侧无法确认该饰品，本次不展示其数据",
        }


async def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
) -> dict[str, Any]:
    return await request_json(
        method,
        url,
        params=params,
        json=json_body,
        # One server-directed retry: on 429/5xx the shared client honours
        # Retry-After (bounded at 15s), which is the polite way to survive a
        # transient throttle instead of failing the whole CSQAQ side.
        attempts=2,
        total_timeout=30.0,
        headers=headers,
    )


async def _record_csqaq_telemetry(
    operation: str,
    outcome: str,
    started: float,
    *,
    cache_hit: bool = False,
) -> None:
    """Keep optional metrics from changing CSQAQ availability or errors."""

    try:
        await telemetry.record(
            operation,
            outcome,
            (time.perf_counter() - started) * 1000,
            cache_hit=cache_hit,
        )
    except Exception as exc:
        logger.warning("CSQAQ telemetry unavailable error=%s", type(exc).__name__)


csqaq_service = CsqaqService()


__all__ = [
    "CSQAQ_BASE",
    "CSQAQ_KEY",
    "CsqaqGoodDetail",
    "CsqaqPlatformQuote",
    "CsqaqRentInfo",
    "CsqaqService",
    "CsqaqTransport",
    "CachedGoodId",
    "GoodIdCache",
    "csqaq_service",
    "parse_good_detail",
    "parse_good_id_items",
    "parse_suggest_items",
]

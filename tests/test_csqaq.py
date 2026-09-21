"""Fail-closed behaviour of the CSQAQ open-platform gateway (V0.4 P0)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.services import csqaq as csqaq_module
from src.services.csqaq import (
    CachedGoodId,
    CsqaqService,
    _normalize_envelope,
    _parse_csqaq_time,
    parse_good_detail,
    parse_good_id_items,
    parse_suggest_items,
)
from src.services.resilience import get_circuit_breaker
from src.state.csqaq_repository import CsqaqGoodIdRepository
from src.state.runtime import database as runtime_database

_CSQAQ_BREAKERS = ("provider.csqaq_detail", "provider.csqaq_search")


@pytest.fixture(autouse=True)
async def _reset_csqaq_breakers():
    """Keep the process-wide breaker registry from leaking across tests."""

    for name in _CSQAQ_BREAKERS:
        await get_circuit_breaker(name).succeed()
    yield
    for name in _CSQAQ_BREAKERS:
        await get_circuit_breaker(name).succeed()


@pytest.fixture(autouse=True)
def _pin_fake_csqaq_key(monkeypatch: pytest.MonkeyPatch):
    """Pin a test-only placeholder key so CI (no .env) and local runs behave
    identically on the configured code path.

    Without this, module-level ``CSQAQ_KEY`` reads the real value from the
    host ``.env`` locally but resolves to ``""`` on CI, so every
    ``CsqaqService`` test short-circuits to "not configured" there.  The
    value is an obviously non-credential placeholder; tests that exercise
    the fail-closed path explicitly reset the key to ``""``.
    """

    monkeypatch.setattr(
        csqaq_module,
        "CSQAQ_KEY",
        "taffy-test-csqaq-key-not-a-real-credential-0000",
    )


# ---------------------------------------------------------------------------
# Transport fakes
# ---------------------------------------------------------------------------


class FakeTransport:
    """Record calls and replay canned envelopes per path."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.calls.append((method, url, dict(headers), params or json_body))
        for marker, payload in self.responses.items():
            if marker in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected url: {url}")


GOOD_DETAIL_DATA = {
    "goods_info": {
        "id": 7310,
        "name": "蝴蝶刀（★）",
        "market_hash_name": "★ Butterfly Knife",
        "statistic": 123_456,
        "updated_at": "2026-08-29 12:00:00",
        "steam_sell_price": 100.5,
        "steam_sell_num": 10,
        "steam_buy_price": 88,
        "buff_sell_price": 95,
        "buff_sell_num": 200,
        "buff_buy_price": 90,
        "buff_buy_num": 5,
        "yyyp_sell_price": 92,
        "yyyp_sell_num": 0,
        "yyyp_lease_price": 3.2,
        "yyyp_long_lease_price": 30,
        "yyyp_lease_annual": 0.123,
        "yyyp_long_lease_annual": 0.08,
        "yyyp_lease_num": 7,
        "sell_price_rate_7": -2.5,
        "sell_price_rate_30": 4.75,
        "c5_sell_price": -1,
    }
}


def _good_envelope(data: Any) -> dict[str, Any]:
    return {"code": 200, "msg": "ok", "data": data}


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def test_parse_good_detail_projects_all_platforms_rent_and_supply() -> None:
    detail = parse_good_detail(7310, GOOD_DETAIL_DATA)

    assert detail is not None
    assert detail.good_id == 7310
    assert detail.market_hash_name == "★ Butterfly Knife"
    assert detail.name_cn == "蝴蝶刀（★）"
    assert detail.supply_count == 123_456
    assert detail.data_age_seconds is not None and detail.data_age_seconds >= 0
    assert detail.price_change_7d == -2.5
    assert detail.price_change_30d == 4.75
    platforms = {quote.platform: quote for quote in detail.quotes}
    assert set(platforms) == {"steam", "buff", "yyyp"}
    assert platforms["steam"].sell_price == 100.5
    assert platforms["buff"].buy_num == 5
    assert platforms["yyyp"].sell_num == 0
    assert detail.rent is not None
    assert detail.rent.short_price == 3.2
    assert detail.rent.long_price == 30.0
    assert detail.rent.short_annual_yield == 0.123
    assert detail.rent.leasing_num == 7


def test_parse_good_detail_rejects_mismatched_or_missing_id() -> None:
    assert parse_good_detail(999, GOOD_DETAIL_DATA) is None
    assert parse_good_detail(1, {"nope": True}) is None
    assert parse_good_detail(1, {"goods_info": {"id": None}}) is None
    assert parse_good_detail(1, {"goods_info": {"id": "7310"}}) is None
    assert parse_good_detail(1, "not-a-mapping") is None


def test_parse_good_detail_null_safety_keeps_valid_facets_only() -> None:
    detail = parse_good_detail(5, {"goods_info": {"id": 5, "steam_sell_num": 3}})
    assert detail is not None
    assert detail.market_hash_name is None
    assert detail.supply_count is None
    assert detail.data_age_seconds is None
    quotes = {quote.platform: quote for quote in detail.quotes}
    assert set(quotes) == {"steam"}
    assert quotes["steam"].sell_price is None and quotes["steam"].sell_num == 3
    assert detail.rent is None


def test_parse_good_detail_drops_invalid_values_instead_of_guessing() -> None:
    payload = {
        "goods_info": {
            "id": 7,
            "statistic": -5,
            "steam_sell_price": "n/a",
            "buff_sell_num": True,
            "yyyp_lease_annual": "12%",
            "sell_price_rate_7": None,
            "updated_at": "not-a-date",
        }
    }
    detail = parse_good_detail(7, payload)
    assert detail is not None
    assert detail.supply_count is None
    assert detail.data_age_seconds is None
    assert all(quote.platform != "buff" for quote in detail.quotes)


def test_parse_csqaq_time_accepts_epoch_and_dates() -> None:
    assert _parse_csqaq_time(1_756_442_400) == 1_756_442_400.0
    assert _parse_csqaq_time("1756442400") == 1_756_442_400.0
    assert _parse_csqaq_time(1_756_442_400_000) == 1_756_442_400.0
    assert _parse_csqaq_time("2026-08-29 12:00:00") is not None
    assert _parse_csqaq_time("2026-08-29T22:23:32") is not None
    assert _parse_csqaq_time("2026/08/29 12:00:00") is not None
    assert _parse_csqaq_time("2026-08-29") is not None
    assert _parse_csqaq_time("garbage") is None
    assert _parse_csqaq_time(None) is None
    assert _parse_csqaq_time(True) is None


def test_parse_suggest_items_and_good_id_items() -> None:
    suggest = parse_suggest_items(
        [{"id": 1, "value": "蝴蝶刀"}, {"id": 0, "value": "bad"}, {"id": 2}, "junk"]
    )
    assert suggest == ((1, "蝴蝶刀"),)

    mapping = parse_good_id_items(
        {"data": {"a": {"id": 11, "name": "AK47"}, "b": {"id": 12, "name": "M4A1"}}}
    )
    assert mapping == ((11, "AK47"), (12, "M4A1"))

    listing = parse_good_id_items({"data": [{"id": 13, "name": "沙鹰"}]})
    assert listing == ((13, "沙鹰"),)
    assert parse_good_id_items(None) == ()
    assert parse_good_id_items({"data": None}) == ()


def test_parsers_accept_numeric_string_ids_from_live_payloads() -> None:
    """CSQAQ returns ``id`` as JSON strings; they must still resolve."""

    suggest = parse_suggest_items(
        [{"id": "6796", "value": "蝴蝶刀（★）"}, {"id": "6.5", "value": "bad"}]
    )
    assert suggest == ((6796, "蝴蝶刀（★）"),)

    mapping = parse_good_id_items({"data": {"a": {"id": "7310", "name": "AK47"}}})
    assert mapping == ((7310, "AK47"),)

    payload = {
        "goods_info": {"id": "7310", "statistic": "11299", "updated_at": "2026-08-29T22:23:32"}
    }
    detail = parse_good_detail(7310, payload)
    assert detail is not None
    assert detail.supply_count == 11_299
    assert detail.data_age_seconds is not None


# ---------------------------------------------------------------------------
# Envelope normalization and fail-closed service behaviour
# ---------------------------------------------------------------------------


def test_normalize_envelope_accepts_zero_and_2xx_rejects_the_rest() -> None:
    assert _normalize_envelope({"code": 0, "data": {"x": 1}})["success"] is True
    assert _normalize_envelope({"code": 200, "data": None})["success"] is True
    for code in (400, 401, 404, 429, 500, 503):
        normalized = _normalize_envelope({"code": code, "msg": "boom", "data": None})
        assert normalized["success"] is False, code
        assert normalized["errorMsg"] == "boom"
    assert _normalize_envelope({"code": "200"})["success"] is False
    assert _normalize_envelope(None)["success"] is False
    assert _normalize_envelope("oops")["success"] is False


def test_service_without_token_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csqaq_module, "CSQAQ_KEY", "")
    transport = FakeTransport({})
    service = CsqaqService(transport=transport, min_interval=0.0)

    assert service.configured() is False
    with pytest.raises(RuntimeError, match="CSQAQ_API_KEY"):
        service.headers()


async def test_suggest_and_exact_search_require_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(csqaq_module, "CSQAQ_KEY", "")
    transport = FakeTransport({})
    service = CsqaqService(transport=transport, min_interval=0.0)
    blocked = await service.suggest("蝴蝶刀")
    assert blocked["success"] is False
    assert blocked.get("unconfigured") is True
    blocked_exact = await service.exact_search("蝴蝶刀")
    assert blocked_exact.get("unconfigured") is True
    blocked_detail = await service.good_detail(7310)
    assert blocked_detail.get("unconfigured") is True
    assert transport.calls == []


async def test_suggest_parses_items_and_normalizes_upstream_failures() -> None:
    transport = FakeTransport(
        {
            "/search/suggest": {"code": 429, "msg": "too many", "data": None},
        }
    )
    service = CsqaqService(transport=transport, min_interval=0.0)
    response = await service.suggest("蝴蝶刀")
    assert response["success"] is False
    assert response["errorMsg"] == "too many"

    ok_transport = FakeTransport(
        {"/search/suggest": _good_envelope([{"id": 7310, "value": "蝴蝶刀"}])}
    )
    ok_service = CsqaqService(transport=ok_transport, min_interval=0.0)
    ok = await ok_service.suggest("蝴蝶刀")
    assert ok["success"] is True
    assert ok["items"] == ((7310, "蝴蝶刀"),)
    assert ok_transport.calls[0][2] == {"ApiToken": csqaq_module.CSQAQ_KEY}


async def test_transport_exception_becomes_generic_error_and_trips_breaker() -> None:
    transport = FakeTransport({"/info/good": RuntimeError("network down")})
    service = CsqaqService(transport=transport, min_interval=0.0)
    first = await service.good_detail(7310)
    assert first["success"] is False
    # Circuit still closed (single failure), so the second call hits transport again.
    second = await service.good_detail(7310)
    assert second["success"] is False
    assert len(transport.calls) >= 2


async def test_rate_gate_serializes_calls_behind_the_interval() -> None:
    transport = FakeTransport({"/search/suggest": _good_envelope([])})
    service = CsqaqService(transport=transport, min_interval=0.2)
    started = time.monotonic()
    await service.suggest("a")
    await service.suggest("b")
    elapsed = time.monotonic() - started
    assert elapsed >= 0.18, "the second call must wait a full interval after the first completes"


async def test_rate_gate_spaces_after_slow_completions_not_starts() -> None:
    """A slow request must push the next one a full interval past its end.

    Regression: the gate used to reserve its slot when a call *started*, so a
    1.4s TLS+request meant the follow-up landed 0.04s after the previous one
    on the wire and the upstream answered 429.
    """

    class SlowTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__({"/search/suggest": _good_envelope([])})
            self.completions: list[float] = []

        async def __call__(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            params: dict[str, Any] | None,
            json_body: dict[str, Any] | None,
        ) -> dict[str, Any]:
            await asyncio.sleep(0.15)
            self.completions.append(time.monotonic())
            return await super().__call__(method, url, headers, params, json_body)

    transport = SlowTransport()
    service = CsqaqService(transport=transport, min_interval=0.2)
    await service.suggest("a")
    await service.suggest("b")

    assert len(transport.completions) == 2
    gap = transport.completions[1] - transport.completions[0]
    assert gap >= 0.19, f"second request must start a full interval after the first ends, got {gap}"


async def test_good_detail_cache_hit_avoids_second_network_call() -> None:
    transport = FakeTransport({"/info/good": _good_envelope(GOOD_DETAIL_DATA)})
    service = CsqaqService(transport=transport, min_interval=0.0, detail_ttl=60.0)
    first = await service.good_detail(7310)
    assert first["success"] is True and first["detail"].supply_count == 123_456
    second = await service.good_detail(7310)
    assert second["success"] is True
    assert len(transport.calls) == 1


async def test_good_detail_never_caches_failures() -> None:
    class FlakyTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__({})
            self.count = 0

        async def __call__(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            params: dict[str, Any] | None,
            json_body: dict[str, Any] | None,
        ) -> dict[str, Any]:
            self.count += 1
            if self.count == 1:
                return {"code": 500, "msg": "server", "data": None}
            return _good_envelope(GOOD_DETAIL_DATA)

    transport = FlakyTransport()
    service = CsqaqService(transport=transport, min_interval=0.0)
    failed = await service.good_detail(7310)
    assert failed["success"] is False
    recovered = await service.good_detail(7310)
    assert recovered["success"] is True
    assert transport.count == 2


async def test_good_detail_rejects_invalid_ids() -> None:
    transport = FakeTransport({})
    service = CsqaqService(transport=transport, min_interval=0.0)
    for bad in (0, -1, "7310", True, 1.5):
        response = await service.good_detail(bad)  # type: ignore[arg-type]
        assert response["success"] is False
    assert transport.calls == []


async def test_resolve_good_id_uses_cache_and_requires_confident_match() -> None:
    class MemoryCache:
        def __init__(self) -> None:
            self.store: dict[str, CachedGoodId] = {}
            self.puts: list[str] = []

        async def get(self, market_hash_name: str, *, max_age_seconds: int) -> CachedGoodId | None:
            return self.store.get(market_hash_name)

        async def put(
            self,
            market_hash_name: str,
            *,
            good_id: int,
            csqaq_name: str,
            resolved_at: int | None = None,
        ) -> CachedGoodId:
            self.puts.append(market_hash_name)
            resolved = CachedGoodId(
                good_id=good_id, csqaq_name=csqaq_name, resolved_at=resolved_at or 0
            )
            self.store[market_hash_name] = resolved
            return resolved

    cache = MemoryCache()
    transport = FakeTransport(
        {
            "/info/get_good_id": _good_envelope(
                {"data": {"7310": {"id": 7310, "name": "★ Butterfly Knife"}}}
            ),
            "/search/suggest": _good_envelope([{"id": 7310, "value": "蝴蝶刀"}]),
        }
    )
    service = CsqaqService(transport=transport, min_interval=0.0, good_id_cache=cache)

    resolved = await service.resolve_good_id(
        "★ butterfly knife", market_hash_name="★ Butterfly Knife"
    )
    assert resolved is not None and resolved.good_id == 7310
    assert cache.puts == ["★ Butterfly Knife"]

    # Second resolution must come from the cache without any new network call.
    calls_before = len(transport.calls)
    again = await service.resolve_good_id("★ Butterfly Knife")
    assert again is not None and again.good_id == 7310
    assert len(transport.calls) == calls_before

    # Ambiguous answers are never guessed.
    ambiguous_transport = FakeTransport(
        {"/info/get_good_id": _good_envelope({"data": {"1": {"id": 1, "name": "完全无关的饰品"}}})}
    )
    ambiguous_service = CsqaqService(
        transport=ambiguous_transport, min_interval=0.0, good_id_cache=MemoryCache()
    )
    assert await ambiguous_service.resolve_good_id("★ Butterfly Knife") is None


async def test_resolve_good_id_falls_back_to_suggest_endpoint() -> None:
    transport = FakeTransport(
        {
            "/info/get_good_id": {"code": 404, "msg": "not found", "data": None},
            "/search/suggest": _good_envelope([{"id": 88, "value": "蝴蝶刀（★）"}]),
        }
    )
    service = CsqaqService(transport=transport, min_interval=0.0)
    resolved = await service.resolve_good_id("蝴蝶刀（★）")
    assert resolved is not None and resolved.good_id == 88


# ---------------------------------------------------------------------------
# Durable good_id cache repository (SQLite)
# ---------------------------------------------------------------------------


async def test_good_id_repository_roundtrip_ttl_and_cleanup() -> None:
    repository = CsqaqGoodIdRepository(runtime_database)
    await repository.initialize()

    stored = await repository.put(
        "★ Butterfly Knife",
        good_id=7310,
        csqaq_name="蝴蝶刀（★）",
        resolved_at=int(time.time()) - 10,
    )
    assert stored.good_id == 7310

    fresh = await repository.get("★ Butterfly Knife", max_age_seconds=3_600)
    assert fresh is not None and fresh.csqaq_name == "蝴蝶刀（★）"

    expired = await repository.get("★ Butterfly Knife", max_age_seconds=5)
    assert expired is None

    zero_age = await repository.get("★ Butterfly Knife", max_age_seconds=0)
    assert zero_age is None

    assert await repository.get("Unknown Item", max_age_seconds=3_600) is None

    purged = await repository.purge_older_than(cutoff=int(time.time()) + 1)
    assert purged >= 1
    assert await repository.get("★ Butterfly Knife", max_age_seconds=3_600) is None


async def test_good_id_repository_rejects_invalid_arguments() -> None:
    repository = CsqaqGoodIdRepository(runtime_database)
    await repository.initialize()
    with pytest.raises(ValueError, match="market_hash_name"):
        await repository.get("", max_age_seconds=60)
    with pytest.raises(ValueError, match="market_hash_name"):
        await repository.get("x" * 300, max_age_seconds=60)
    with pytest.raises(ValueError, match="good_id"):
        await repository.put("ok-name", good_id=0, csqaq_name="name")
    with pytest.raises(ValueError, match="good_id"):
        await repository.put("ok-name", good_id=True, csqaq_name="name")
    with pytest.raises(ValueError, match="csqaq_name"):
        await repository.put("ok-name", good_id=1, csqaq_name="")
    with pytest.raises(ValueError, match="cutoff"):
        await repository.purge_older_than(cutoff=-1)

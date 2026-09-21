"""P0 hoard-radar tests: float resolver six-state matrix and cache flow."""

from __future__ import annotations

import httpx
import pytest

from src.services.float_resolver import (
    HttpFloatResolver,
    ItemFloatAnchors,
    NullFloatResolver,
    build_default_resolver,
    extract_inspect_url,
    hash_inspect_url,
    normalize_paintwear,
    parse_float_payload,
    safe_inspect_url,
)


def _payload(**items: dict[str, object]) -> dict[str, object]:
    return {"items": list(items.values())}


def _item(url: str, **fields: object) -> dict[str, object]:
    merged: dict[str, object] = {"inspect_url": url}
    merged.update(fields)
    return merged


URL_A = "steam://connect/1/2"
URL_B = "https://api.steampowered.com/IEconItem/730/2"


class _MemoryCache:
    def __init__(self) -> None:
        self.saved: dict[str, tuple[ItemFloatAnchors, int]] = {}

    async def load_float_cache(self, hashes):
        found: dict[str, ItemFloatAnchors] = {}
        for digest in hashes:
            entry = self.saved.get(digest)
            if entry is not None:
                found[digest] = entry[0]
        return found

    async def save_float_cache(self, entries):
        self.saved.update(entries)
        return len(entries)


def test_safe_inspect_url_whitelist_is_fail_closed() -> None:
    assert safe_inspect_url("steam://connect/1/2") is not None
    assert safe_inspect_url("https://api.steampowered.com/ieconitem/x") is not None
    # Non-whitelisted domains / loopback / private / garbage are all dropped.
    for bad in [
        "http://127.0.0.1:8080/x",
        "http://192.168.1.4/x",
        "http://10.0.0.9/x",
        "https://evil.example.com/inspect",
        "ftp://api.steampowered.com/x",
        "steam://",
        123,
        None,
        "x" * 700,
    ]:
        assert safe_inspect_url(bad) is None


def test_extract_inspect_url_reads_actions_and_drops_unknown_links() -> None:
    record = {
        "actions": [
            {"name": "行情", "link": "https://steamdt.com/x"},
            {"name": "Inspect", "link": "steam://connect/9/8"},
        ]
    }
    assert extract_inspect_url(record) == "steam://connect/9/8"
    assert extract_inspect_url({"actions": [{"link": "https://evil.example.com"}]}) is None
    assert extract_inspect_url({}) is None
    assert extract_inspect_url({"actions": "not-a-list"}) is None


def test_normalize_paintwear_rejects_booleans_and_out_of_range() -> None:
    assert normalize_paintwear(0.1234) == pytest.approx(0.1234)
    assert normalize_paintwear("0.5") == pytest.approx(0.5)
    assert normalize_paintwear(True) is None
    assert normalize_paintwear(-0.1) is None
    assert normalize_paintwear(1.5) is None
    assert normalize_paintwear(None) is None


def test_parse_float_payload_skips_broken_items_only() -> None:
    body = {
        "items": [
            _item(URL_A, paintwear=0.21, market_hash_name="AK-47", paint_index=302),
            _item("steam://connect/3/4"),  # missing paintwear -> skipped
            _item(URL_B, paintwear=7.0),  # out of range -> skipped
            "garbage",
        ]
    }
    resolved = parse_float_payload(body, [URL_A, URL_B])
    assert set(resolved) == {URL_A}
    assert resolved[URL_A].paintwear == pytest.approx(0.21)
    # Structurally broken bodies resolve to nothing at all.
    assert parse_float_payload({"items": None}, [URL_A]) == {}
    assert parse_float_payload({"nope": 1}, [URL_A]) == {}


@pytest.mark.asyncio
async def test_null_resolver_yields_nothing_without_any_network_call() -> None:
    resolver = NullFloatResolver()
    assert await resolver.resolve([URL_A, URL_B]) == {}


@pytest.mark.asyncio
async def test_http_resolver_six_state_matrix_over_mock_transport() -> None:
    """正常 / 字段缺失 / 403私有 / 429限流 / 5xx / JSON截断 + 超时全部降级。"""

    ok_body = _payload(a=_item(URL_A, paintwear=0.11))

    def handler_for(mode: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if mode == "ok":
                return httpx.Response(200, json=ok_body)
            if mode == "missing_fields":
                return httpx.Response(200, json={"items": [_item(URL_A)]})
            if mode == "private":
                return httpx.Response(403, json={"error": "forbidden"})
            if mode == "rate_limited":
                return httpx.Response(429, json={"error": "slow down"})
            if mode == "server_error":
                return httpx.Response(503, text="boom")
            if mode == "truncated":
                return httpx.Response(
                    200,
                    content=b'{"items": [{"inspect_url": "' + URL_A.encode(),
                    headers={"content-type": "application/json"},
                )
            raise AssertionError(mode)

        return handler

    cache = _MemoryCache()

    async def failing_transport(url: str, payload: object) -> dict[str, object]:
        raise TimeoutError("upstream timeout")

    timeout_resolver = HttpFloatResolver(
        cache, base_url="https://float.test", transport=failing_transport
    )
    assert await timeout_resolver.resolve([URL_A]) == {}

    from src.services.http_client import SharedHTTPClient

    for mode, expected_hits in [
        ("ok", 1),
        ("missing_fields", 0),
        ("private", 0),
        ("rate_limited", 0),
        ("server_error", 0),
        ("truncated", 0),
    ]:
        round_client = SharedHTTPClient(
            client_factory=lambda m=mode: httpx.AsyncClient(
                transport=httpx.MockTransport(handler_for(m))
            )
        )
        cache_round = _MemoryCache()

        async def seam(url: str, payload: object, _client=round_client):
            return await _client.request_json(
                "POST", url, json=dict(payload), attempts=1, total_timeout=5.0
            )

        resolver = HttpFloatResolver(
            cache_round,
            base_url="https://float.test",
            transport=seam,
        )
        result = await resolver.resolve([URL_A])
        assert len(result) == expected_hits, f"mode={mode} result={result}"
        # A successful round must persist into the durable cache.
        if expected_hits:
            digest = hash_inspect_url(URL_A)
            assert digest in cache_round.saved
            second = await resolver.resolve([URL_A])
            assert second[URL_A].paintwear == pytest.approx(0.11)
            # Second round was served from cache: no additional quota call.

    del cache  # retained for reuse by future cases


@pytest.mark.asyncio
async def test_hourly_quota_blocks_further_upstream_calls() -> None:
    calls: list[str] = []
    cache = _MemoryCache()

    async def transport(url: str, payload: object) -> dict[str, object]:
        calls.append(url)
        return _payload(a=_item(URL_A, paintwear=0.05))

    resolver = HttpFloatResolver(
        cache,
        base_url="https://float.test",
        transport=transport,
        hourly_limit=2,
        clock=lambda: 10_000.0,
    )
    fresh = str(_item(URL_A, paintwear=0.05)["inspect_url"])
    other = "steam://connect/7/9"

    first = await resolver.resolve([fresh])
    assert first[fresh].paintwear == pytest.approx(0.05)
    # Same URL resolves from cache: still exactly one upstream call so far.
    cached_hit = await resolver.resolve([fresh])
    assert cached_hit[fresh].paintwear == pytest.approx(0.05)
    assert len(calls) == 1

    async def blocked(url: str, payload: object):  # pragma: no cover - must not run
        raise AssertionError("quota-exhausted upstream should not be called")

    # Burn the second quota slot on a distinct miss.
    resolver._quota_window.clear()
    resolver._quota_window.extend([10_000.0, 10_001.0])
    resolver._transport = blocked  # type: ignore[method-assign]
    assert await resolver.resolve([other]) == {}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_upstream_call(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _MemoryCache()
    digest = hash_inspect_url(URL_A)
    cache.saved[digest] = (ItemFloatAnchors(paintwear=0.42, market_hash_name="M4A1-S"), 1000)
    calls: list[str] = []

    async def transport(url: str, payload: object) -> dict[str, object]:
        calls.append(url)
        return {}

    resolver = HttpFloatResolver(cache, base_url="https://float.test", transport=transport)
    got = await resolver.resolve([URL_A])
    assert got[URL_A].paintwear == pytest.approx(0.42)
    assert calls == []


@pytest.mark.asyncio
async def test_build_default_resolver_disabled_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.float_resolver import FLOAT_API_BASE_ENV, FLOAT_HOURLY_LIMIT_ENV

    monkeypatch.delenv(FLOAT_API_BASE_ENV, raising=False)
    monkeypatch.delenv(FLOAT_HOURLY_LIMIT_ENV, raising=False)
    assert build_default_resolver(_MemoryCache()) is None

    monkeypatch.setenv(FLOAT_API_BASE_ENV, "not-a-url")
    assert build_default_resolver(_MemoryCache()) is None

    monkeypatch.setenv(FLOAT_API_BASE_ENV, "https://float.example.internal")
    built = build_default_resolver(_MemoryCache())
    assert isinstance(built, HttpFloatResolver)

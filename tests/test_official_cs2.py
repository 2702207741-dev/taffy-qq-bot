from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.services.official_cs2 import (
    EsportsMatch,
    OfficialCS2Report,
    OfficialCS2Service,
    _default_list_request,
    _parse_datetime,
    _safe_update_url,
    format_official_report,
    parse_pandascore_matches,
    parse_valve_updates,
)
from src.services.resilience import get_circuit_breaker
from src.services.verified import DataProvenance, UnavailableReason, VerifiedResult

NOW = datetime(2026, 7, 14, 8, tzinfo=timezone.utc)


def _valve_payload(*, published: datetime = NOW - timedelta(hours=1)) -> dict[str, object]:
    return {
        "appnews": {
            "appid": 730,
            "newsitems": [
                {
                    "title": "<b>CS2 Update</b>",
                    "url": "https://store.steampowered.com/news/app/730/view/123",
                    "date": int(published.timestamp()),
                    "contents": "[h1]Verified[/h1] patch notes",
                }
            ],
        }
    }


def _match(
    *,
    identifier: int = 1,
    status: str = "finished",
    begins: datetime = NOW - timedelta(hours=2),
    include_scores: bool = True,
) -> dict[str, object]:
    return {
        "id": identifier,
        "name": "Team A vs Team B",
        "status": status,
        "begin_at": begins.isoformat(),
        "league": {"name": "Verified League"},
        "opponents": [
            {"opponent": {"id": 11, "name": "Team A"}},
            {"opponent": {"id": 22, "name": "Team B"}},
        ],
        "results": (
            [{"team_id": 11, "score": 2}, {"team_id": 22, "score": 1}] if include_scores else []
        ),
    }


def test_valve_parser_accepts_current_official_items_and_cleans_markup() -> None:
    parsed = parse_valve_updates(_valve_payload(), fetched_at=NOW)
    assert len(parsed) == 1
    assert parsed[0].title == "CS2 Update"
    assert parsed[0].summary == "Verified patch notes"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"appnews": {"appid": 999, "newsitems": []}},
        {"appnews": {"appid": 730, "newsitems": "bad"}},
        {"appnews": {"appid": 730, "newsitems": []}},
        {
            "appnews": {
                "appid": 730,
                "newsitems": [
                    {
                        "title": "bad host",
                        "url": "https://evil.example/news",
                        "date": int(NOW.timestamp()),
                    }
                ],
            }
        },
    ],
)
def test_valve_parser_rejects_wrong_or_incomplete_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_valve_updates(payload, fetched_at=NOW)


def test_valve_parser_rejects_future_old_duplicate_and_invalid_dates() -> None:
    items = []
    for date, url in (
        (NOW + timedelta(hours=1), "https://store.steampowered.com/news/1"),
        (NOW - timedelta(days=91), "https://store.steampowered.com/news/2"),
    ):
        item = _valve_payload(published=date)["appnews"]["newsitems"][0]  # type: ignore[index]
        items.append({**item, "url": url})
    items.extend(
        [
            {"title": "missing date", "url": "https://store.steampowered.com/news/3"},
            {"title": "bool date", "url": "https://store.steampowered.com/news/4", "date": True},
        ]
    )
    with pytest.raises(ValueError):
        parse_valve_updates({"appnews": {"appid": 730, "newsitems": items}}, fetched_at=NOW)


def test_official_helper_parsers_reject_unsafe_dates_and_urls() -> None:
    assert _parse_datetime(None) is None
    assert _parse_datetime("x" * 81) is None
    assert _parse_datetime("not-a-date") is None
    assert _parse_datetime("2026-07-14T01:00:00") is None
    assert _safe_update_url(None) is None
    assert _safe_update_url("x" * 2049) is None
    assert _safe_update_url("http://store.steampowered.com/news") is None
    assert _safe_update_url("https://user:pass@store.steampowered.com/news") is None


def test_pandascore_parser_requires_complete_match_fields() -> None:
    completed = parse_pandascore_matches([_match()], fetched_at=NOW, completed=True)
    assert completed[0].score_a == 2 and completed[0].score_b == 1
    upcoming = parse_pandascore_matches(
        [_match(status="not_started", begins=NOW + timedelta(hours=3), include_scores=False)],
        fetched_at=NOW,
        completed=False,
    )
    assert upcoming[0].score_a is None
    with pytest.raises(ValueError):
        parse_pandascore_matches("bad", fetched_at=NOW, completed=True)
    invalid = [
        _match(include_scores=False),
        _match(identifier=2, status="canceled"),
        _match(identifier=3, begins=NOW - timedelta(days=20)),
    ]
    with pytest.raises(ValueError):
        parse_pandascore_matches(invalid, fetched_at=NOW, completed=True)


def test_pandascore_parser_skips_duplicate_invalid_teams_results_and_league() -> None:
    duplicate_team = _match(identifier=2)
    duplicate_team["opponents"] = [
        {"opponent": {"id": 11, "name": "A"}},
        {"opponent": {"id": 11, "name": "B"}},
    ]
    missing_league = _match(identifier=3)
    missing_league["league"] = None
    invalid_results = _match(identifier=4)
    invalid_results["results"] = ["bad", {"team_id": True, "score": -1}]
    valid = _match(identifier=5)
    result = parse_pandascore_matches(
        ["bad", duplicate_team, missing_league, invalid_results, valid, valid],
        fetched_at=NOW,
        completed=True,
    )
    assert [item.match_id for item in result] == [5]


@pytest.mark.asyncio
async def test_default_pandascore_request_enforces_size_and_decodes_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return [{"ok": True}]

    client = AsyncMock()
    client.get.return_value = Response(b"small")

    @asynccontextmanager
    async def lease_client():
        yield client

    monkeypatch.setattr("src.services.official_cs2.lease_http_client", lease_client)
    assert await _default_list_request("https://example.test", {"A": "B"}, {"x": 1}) == [
        {"ok": True}
    ]
    client.get.return_value = Response(b"x" * 1_500_001)
    with pytest.raises(ValueError, match="too large"):
        await _default_list_request("https://example.test", {}, {})


@pytest.mark.asyncio
async def test_default_pandascore_request_retries_one_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        content = b"small"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return [{"recovered": True}]

    request = httpx.Request("GET", "https://example.test")
    client = AsyncMock()
    client.get.side_effect = [httpx.ConnectError("stale", request=request), Response()]

    @asynccontextmanager
    async def lease_client():
        yield client

    monkeypatch.setattr("src.services.official_cs2.lease_http_client", lease_client)
    with patch("src.services.official_cs2.asyncio.sleep", AsyncMock()) as sleep:
        assert await _default_list_request("https://example.test", {}, {}) == [{"recovered": True}]
    sleep.assert_awaited_once_with(0.1)
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_official_service_caches_and_singleflights_valve_requests() -> None:
    await get_circuit_breaker("valve-news").succeed()
    requester = AsyncMock(return_value=_valve_payload())
    monotonic = [10.0]
    service = OfficialCS2Service(
        valve_requester=requester,
        clock=lambda: NOW,
        monotonic=lambda: monotonic[0],
    )
    left, right = await asyncio.gather(service.valve_updates(), service.valve_updates())
    assert left.is_available and right.is_available
    requester.assert_awaited_once()
    cached = await service.valve_updates()
    assert cached is left
    monotonic[0] += 301
    await service.valve_updates()
    assert requester.await_count == 2
    assert requester.await_args.kwargs["attempts"] == 1
    assert requester.await_args.kwargs["total_timeout"] == 2.0
    assert requester.await_args.kwargs["timeout"].connect == 1.2


@pytest.mark.asyncio
async def test_official_service_maps_valve_errors_without_guessing() -> None:
    await get_circuit_breaker("valve-news").succeed()
    upstream = OfficialCS2Service(
        valve_requester=AsyncMock(side_effect=TimeoutError), clock=lambda: NOW
    )
    assert (await upstream.valve_updates()).reason is UnavailableReason.UPSTREAM_ERROR
    await get_circuit_breaker("valve-news").succeed()
    invalid = OfficialCS2Service(valve_requester=AsyncMock(return_value={}), clock=lambda: NOW)
    assert (await invalid.valve_updates()).reason is UnavailableReason.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_official_service_returns_immediately_while_valve_circuit_is_open() -> None:
    breaker = get_circuit_breaker("valve-news")
    await breaker.succeed()
    for _ in range(5):
        await breaker.fail()
    requester = AsyncMock()
    service = OfficialCS2Service(valve_requester=requester, clock=lambda: NOW)
    assert (await service.valve_updates()).reason is UnavailableReason.CIRCUIT_OPEN
    requester.assert_not_awaited()
    await breaker.succeed()


@pytest.mark.asyncio
async def test_pandascore_uses_bearer_header_and_maps_rate_limit() -> None:
    await get_circuit_breaker("pandascore-matches").succeed()
    captured: dict[str, object] = {}

    async def requester(url: str, headers: object, params: object) -> object:
        captured.update(url=url, headers=headers, params=params)
        return [_match(status="not_started", begins=NOW + timedelta(hours=1), include_scores=False)]

    service = OfficialCS2Service(
        match_requester=requester,
        pandascore_token="secret-token",
        clock=lambda: NOW,
    )
    result = await service.matches(completed=False)
    assert result.is_available
    assert captured["headers"] == {
        "Authorization": "Bearer secret-token",
        "Accept": "application/json",
    }
    assert "secret-token" not in str(captured["url"])
    assert "secret-token" not in str(captured["params"])

    request = httpx.Request("GET", "https://api.pandascore.co/test")
    response = httpx.Response(429, request=request)
    limited = OfficialCS2Service(
        match_requester=AsyncMock(
            side_effect=httpx.HTTPStatusError("limited", request=request, response=response)
        ),
        pandascore_token="token",
        clock=lambda: NOW,
    )
    assert (await limited.matches(completed=True)).reason is UnavailableReason.RATE_LIMITED


@pytest.mark.asyncio
async def test_pandascore_maps_server_transport_and_validation_failures() -> None:
    breaker = get_circuit_breaker("pandascore-matches")
    request = httpx.Request("GET", "https://api.pandascore.co/test")
    for failure, reason in (
        (
            httpx.HTTPStatusError(
                "server", request=request, response=httpx.Response(500, request=request)
            ),
            UnavailableReason.UPSTREAM_ERROR,
        ),
        (TimeoutError(), UnavailableReason.UPSTREAM_ERROR),
    ):
        await breaker.succeed()
        service = OfficialCS2Service(
            match_requester=AsyncMock(side_effect=failure),
            pandascore_token="token",
            clock=lambda: NOW,
        )
        assert (await service.matches(completed=True)).reason is reason
    await breaker.succeed()
    invalid = OfficialCS2Service(
        match_requester=AsyncMock(return_value=[]),
        pandascore_token="token",
        clock=lambda: NOW,
    )
    assert (await invalid.matches(completed=False)).reason is UnavailableReason.VALIDATION_FAILED

    await breaker.succeed()
    for _ in range(5):
        await breaker.fail()
    blocked = OfficialCS2Service(
        match_requester=AsyncMock(), pandascore_token="token", clock=lambda: NOW
    )
    assert (await blocked.matches(completed=False)).reason is UnavailableReason.CIRCUIT_OPEN
    await breaker.succeed()


@pytest.mark.asyncio
async def test_missing_pandascore_token_and_report_section_selection() -> None:
    service = OfficialCS2Service(pandascore_token="", clock=lambda: NOW)
    assert (await service.matches(completed=False)).reason is UnavailableReason.NOT_CONFIGURED
    report = await service.report(include_updates=False, include_matches=True)
    assert report.updates is None
    assert report.upcoming is not None and report.completed is not None
    rendered = format_official_report(report, asks_benefit=True)
    assert "赛事接口尚未配置" in rendered
    assert "不等于饰品价格利好" in rendered


@pytest.mark.asyncio
async def test_report_reaps_sibling_tasks_after_unexpected_failure() -> None:
    service = OfficialCS2Service(pandascore_token="token", clock=lambda: NOW)
    both_started = asyncio.Event()
    release = asyncio.Event()
    counters = {"started": 0, "cancelled": 0}

    async def slow_matches(*, completed: bool):
        del completed
        counters["started"] += 1
        if counters["started"] == 2:
            both_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            counters["cancelled"] += 1
            raise

    async def failing_updates():
        await both_started.wait()
        raise RuntimeError("forced unexpected failure")

    service.matches = slow_matches  # type: ignore[method-assign]
    service.valve_updates = failing_updates  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="forced unexpected failure"):
        await service.report(include_updates=True, include_matches=True)

    assert counters == {"started": 2, "cancelled": 2}


def test_official_formatter_never_invents_missing_sections() -> None:
    report = OfficialCS2Report(
        updates=VerifiedResult.unavailable(UnavailableReason.STALE),
        upcoming=VerifiedResult.unavailable(UnavailableReason.VALIDATION_FAILED),
        completed=VerifiedResult.unavailable(UnavailableReason.VALIDATION_FAILED),
    )
    rendered = format_official_report(report)
    assert "暂时不可用" in rendered
    assert "没有通过队伍、时间、状态和比分完整校验" in rendered


def test_official_formatter_renders_complete_schedule_and_score_verbatim() -> None:
    provenance = DataProvenance("PandaScore", "/matches", NOW, NOW, "f" * 64)
    upcoming = EsportsMatch(
        1, "A vs B", "not_started", NOW + timedelta(hours=1), "League", "A", "B", None, None
    )
    completed = EsportsMatch(
        2, "C vs D", "finished", NOW - timedelta(hours=1), "League", "C", "D", 2, 0
    )
    rendered = format_official_report(
        OfficialCS2Report(
            upcoming=VerifiedResult.available((upcoming,), provenance),
            completed=VerifiedResult.available((completed,), provenance),
        )
    )
    assert "A vs B" in rendered
    assert "C 2:0 D" in rendered

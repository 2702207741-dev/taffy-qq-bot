from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
import pytest
from nonebot.exception import FinishedException

from src.services import fact_sources
from src.services.fact_sources import (
    FactBundle,
    FactSource,
    FactSourceService,
    fetch_fact_source,
    news_source_is_current,
    parse_fact_page,
    source_domain,
    validate_news_bundle_dates,
)
from src.services.http_client import SharedHTTPClient
from src.services.search import SearchResult

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import news


def test_fact_sources_require_https_and_an_allowlisted_domain() -> None:
    assert source_domain("https://www.hltv.org/news/1") == "hltv.org"
    assert source_domain("http://www.hltv.org/news/1") is None
    assert source_domain("https://hltv.org.attacker.example/news/1") is None
    assert source_domain("https://user@hltv.org/news/1") is None


def test_news_sources_require_parseable_recent_page_dates() -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    recent = FactSource(
        title="Recent",
        url="https://www.hltv.org/news/recent",
        domain="hltv.org",
        body="recent body " * 30,
        fetched_at="2026-07-14T01:00:00+00:00",
        page_time="2026-07-13T20:00:00+00:00",
    )
    old = FactSource(
        title="Old",
        url="https://liquipedia.net/counterstrike/old",
        domain="liquipedia.net",
        body="old body " * 30,
        fetched_at="2026-07-14T01:00:00+00:00",
        page_time="2026-01-01",
    )
    assert news_source_is_current(recent, now=now)
    assert not news_source_is_current(old, now=now)

    validated = validate_news_bundle_dates(
        FactBundle(query="CS2新闻", intent="news", sources=(recent, old)),
        now=now,
    )
    assert not validated.is_verified
    assert validated.sources == (recent,)
    assert "45天" in (validated.error or "")


def _source(result: SearchResult, *, page_time: str | None = None) -> FactSource:
    domain = source_domain(result.url)
    assert domain is not None
    return FactSource(
        title=result.title,
        url=result.url,
        domain=domain,
        body=(result.snippet + " verified full page body ") * 20,
        fetched_at="2026-07-13T05:00:00+00:00",
        page_time=page_time,
    )


def test_parse_fact_page_extracts_body_title_and_page_time() -> None:
    html = """
    <html><head>
      <title>CS2 Update Notes</title>
      <meta property="article:published_time" content="2026-07-12T10:30:00Z">
      <script>ignore this forged content</script>
    </head><body><main>
      <h1>Update</h1>
      <p>This official update contains enough readable body text for verification.</p>
      <p>It explains the change, its scope, rollout, compatibility, and player impact.</p>
      <p>The remaining paragraph makes the fixture intentionally longer than the minimum.</p>
    </main></body></html>
    """

    title, body, page_time = parse_fact_page(html)

    assert title == "CS2 Update Notes"
    assert "official update" in body
    assert "forged content" not in body
    assert page_time == "2026-07-12T10:30:00+00:00"


@pytest.mark.asyncio
async def test_fact_page_fetch_recovers_after_stale_connection() -> None:
    clients: list[httpx.AsyncClient] = []
    html = (
        "<html><head><title>Recovered source</title></head><body><main><p>"
        + "verified CS2 source body with enough detail " * 20
        + "</p></main></body></html>"
    )

    def stale(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("stale", request=request)

    def recovered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    def factory() -> httpx.AsyncClient:
        handler = stale if not clients else recovered
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    service = SharedHTTPClient(client_factory=factory)
    result = SearchResult("fallback", "snippet", "https://www.hltv.org/news/recovery")
    with (
        patch.object(fact_sources, "lease_http_client", service.lease),
        patch.object(fact_sources.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        source = await fetch_fact_source(result)
    assert source.title == "Recovered source"
    assert source.domain == "hltv.org"
    assert len(clients) == 2 and clients[0].is_closed
    assert (await service.snapshot()).rebuilds == 1
    sleep.assert_awaited_once_with(0.1)
    await service.close()


@pytest.mark.asyncio
async def test_same_domain_pages_do_not_satisfy_independent_source_gate() -> None:
    hits = [
        SearchResult("one", "latest update first", "https://counter-strike.net/news/one"),
        SearchResult("two", "latest update second", "https://blog.counter-strike.net/news/two"),
    ]
    service = FactSourceService(
        searcher=AsyncMock(return_value=hits),
        fetcher=lambda result: _async_source(result),
    )

    bundle = await service.collect("latest update", search_queries=("query",))

    assert not bundle.is_verified
    assert len(bundle.sources) == 1
    assert bundle.error is not None
    assert "当前仅获取到1个" in bundle.error


async def _async_source(result: SearchResult) -> FactSource:
    return _source(result, page_time="2026-07-12")


@pytest.mark.asyncio
async def test_two_domains_return_structured_verified_evidence() -> None:
    hits = [
        SearchResult(
            "official",
            "NiKo current team roster official details",
            "https://counter-strike.net/news/one",
        ),
        SearchResult(
            "report",
            "NiKo current team roster independent details",
            "https://www.hltv.org/news/two",
        ),
    ]
    service = FactSourceService(
        searcher=AsyncMock(return_value=hits),
        fetcher=_async_source,
    )

    bundle = await service.collect(
        "NiKo current team",
        intent="player_team",
        search_queries=("query",),
    )

    assert bundle.is_verified
    assert {source.domain for source in bundle.sources} == {"counter-strike.net", "hltv.org"}
    document = bundle.as_dict()
    assert document["verified"] is True
    assert document["sources"][0]["url"].startswith("https://")  # type: ignore[index]
    rendered = bundle.render_context()
    assert "数据时间（抓取）: 2026-07-13T05:00:00+00:00" in rendered
    assert "页面发布时间: 2026-07-12" in rendered


@pytest.mark.asyncio
async def test_one_declared_primary_source_satisfies_primary_source_policy() -> None:
    hits = [
        SearchResult(
            "official",
            "latest update official details",
            "https://counter-strike.net/news/one",
        )
    ]
    service = FactSourceService(
        searcher=AsyncMock(return_value=hits),
        fetcher=_async_source,
    )

    bundle = await service.collect(
        "latest update",
        intent="version",
        authoritative_domains=("counter-strike.net",),
        search_queries=("query",),
    )

    assert bundle.is_verified
    assert bundle.verification_basis == "authoritative_primary"
    assert bundle.as_dict()["verification_basis"] == "authoritative_primary"


@pytest.mark.asyncio
async def test_one_non_primary_source_does_not_satisfy_primary_source_policy() -> None:
    hits = [SearchResult("report", "latest update details", "https://www.hltv.org/news/one")]
    service = FactSourceService(
        searcher=AsyncMock(return_value=hits),
        fetcher=_async_source,
    )

    bundle = await service.collect(
        "latest update",
        intent="version",
        authoritative_domains=("counter-strike.net",),
        search_queries=("query",),
    )

    assert not bundle.is_verified
    assert bundle.verification_basis is None


@pytest.mark.asyncio
async def test_generic_landing_pages_are_not_counted_as_fact_evidence() -> None:
    hits = [
        SearchResult(
            "Counter-Strike Wiki",
            "NiKo current team roster Team Falcons",
            "https://liquipedia.net/counterstrike/Main_Page",
        )
    ]
    service = FactSourceService(
        searcher=AsyncMock(return_value=hits),
        fetcher=_async_source,
    )

    bundle = await service.collect(
        "NiKo current team",
        intent="team_player",
        search_queries=("query",),
    )

    assert not bundle.is_verified
    assert bundle.sources == ()


@pytest.mark.asyncio
async def test_news_does_not_call_model_below_two_domain_gate() -> None:
    event = type(
        "Event",
        (),
        {"user_id": 101, "get_plaintext": staticmethod(lambda: "CS2新闻 NiKo")},
    )()
    from src.services.official_cs2 import OfficialCS2Report
    from src.services.verified import UnavailableReason, VerifiedResult

    report = OfficialCS2Report(
        updates=VerifiedResult.unavailable(UnavailableReason.VALIDATION_FAILED),
        upcoming=VerifiedResult.unavailable(UnavailableReason.NOT_CONFIGURED),
        completed=VerifiedResult.unavailable(UnavailableReason.NOT_CONFIGURED),
    )
    finish = AsyncMock(side_effect=FinishedException())

    with (
        patch.object(news._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(news, "v4_enabled_for_user", return_value=True),
        patch.object(news.official_cs2, "report", AsyncMock(return_value=report)),
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]

    assert "模型补写" in str(finish.await_args.args[0])

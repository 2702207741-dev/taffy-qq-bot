"""Verified page-body evidence for time-sensitive CS2 facts.

Search snippets are discovery hints only.  A fact bundle becomes usable after
full page bodies from at least two independently hosted sources were fetched.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .http_client import lease_http_client
from .search import SearchResult, clean_search_field, web_search_results

logger = logging.getLogger("taffy.services.fact_sources")

_ALLOWED_SOURCE_DOMAINS = (
    "counter-strike.net",
    "store.steampowered.com",
    "steamcommunity.com",
    "hltv.org",
    "liquipedia.net",
    "blast.tv",
    "esl.com",
    "eslfaceitgroup.com",
    "faceit.com",
    "pgl-esports.com",
    "steamdt.com",
)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_PAGE_BYTES = 750_000
_MAX_BODY_CHARS = 6_000
_MIN_BODY_CHARS = 160
_MAX_REDIRECTS = 3
_FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0)
_FETCH_SEMAPHORE = asyncio.Semaphore(4)
NEWS_MAX_AGE = timedelta(days=45)
_NEWS_FUTURE_TOLERANCE = timedelta(days=1)
_DATE_PUBLISHED_RE = re.compile(
    r"""["']datePublished["']\s*:\s*["']([^"']{4,100})["']""",
    re.IGNORECASE,
)
_QUERY_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{1,31}", re.IGNORECASE)
_QUERY_TOKEN_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "counter",
        "cs",
        "cs2",
        "csgo",
        "current",
        "currently",
        "in",
        "is",
        "latest",
        "news",
        "now",
        "player",
        "roster",
        "strike",
        "team",
        "the",
        "version",
        "what",
        "where",
        "who",
    }
)
_GENERIC_SOURCE_PATHS = frozenset(
    {
        "",
        "counterstrike",
        "counterstrike/main_page",
        "counter-strike",
        "news",
    }
)
_INTENT_CONTEXT_MARKERS: dict[str, tuple[str, ...]] = {
    "team_player": (
        "team",
        "roster",
        "player",
        "joined",
        "signed",
        "transfer",
        "效力",
        "战队",
        "队伍",
        "阵容",
        "加入",
        "转会",
    ),
    "version": ("update", "patch", "release notes", "更新", "补丁", "版本", "改动"),
    "event": (
        "tournament",
        "match",
        "event",
        "winner",
        "champion",
        "final",
        "major",
        "iem",
        "blast",
        "pgl",
        "esl",
        "赛事",
        "比赛",
        "决赛",
        "冠军",
    ),
}


class FactSourceError(RuntimeError):
    """A candidate page could not become bounded factual evidence."""


def fact_query_anchors(query: str) -> tuple[str, ...]:
    """Extract stable Latin identifiers that retrieved pages must actually contain."""

    return tuple(
        dict.fromkeys(
            token
            for match in _QUERY_TOKEN_RE.finditer(str(query).casefold())
            if (token := match.group(0)) not in _QUERY_TOKEN_STOPWORDS
        )
    )


def source_is_relevant(source: FactSource, query: str, intent: str) -> bool:
    """Reject generic landing pages and bodies unrelated to the user's entities."""

    path = urlsplit(source.url).path.casefold().strip("/")
    if path in _GENERIC_SOURCE_PATHS or path.endswith("/main_page"):
        return False

    haystack = f"{source.title}\n{source.body}".casefold()
    anchors = fact_query_anchors(query)
    if anchors and not all(anchor in haystack for anchor in anchors):
        return False

    markers = _INTENT_CONTEXT_MARKERS.get(str(intent).casefold(), ())
    return not markers or any(marker in haystack for marker in markers)


@dataclass(frozen=True, slots=True)
class FactSource:
    """One successfully fetched page and its provenance metadata."""

    title: str
    url: str
    domain: str
    body: str
    fetched_at: str
    page_time: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FactBundle:
    """Evidence returned to a factual router or news summarizer."""

    query: str
    intent: str
    sources: tuple[FactSource, ...]
    required_domains: int = 2
    authoritative_domains: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_verified(self) -> bool:
        domains = {source.domain for source in self.sources}
        has_primary = bool(domains.intersection(self.authoritative_domains))
        return self.error is None and (has_primary or len(domains) >= self.required_domains)

    @property
    def verification_basis(self) -> str | None:
        if not self.is_verified:
            return None
        domains = {source.domain for source in self.sources}
        if domains.intersection(self.authoritative_domains):
            return "authoritative_primary"
        return "independent_domains"

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "intent": self.intent,
            "verified": self.is_verified,
            "verification_basis": self.verification_basis,
            "required_domains": self.required_domains,
            "authoritative_domains": list(self.authoritative_domains),
            "error": self.error,
            "sources": [source.as_dict() for source in self.sources],
        }

    def render_context(self, *, max_chars: int = 7_000) -> str:
        """Render bounded page bodies with explicit provenance for an LLM."""

        sections: list[str] = []
        per_source = max(300, min(2_500, max_chars // max(1, len(self.sources))))
        for index, source in enumerate(self.sources, 1):
            page_time = source.page_time or "未识别"
            sections.append(
                f"[来源{index}] {source.title}\n"
                f"URL: {source.url}\n"
                f"数据时间（抓取）: {source.fetched_at}\n"
                f"页面发布时间: {page_time}\n"
                f"正文: {source.body[:per_source]}"
            )
        return "\n\n".join(sections)[:max_chars]


def _page_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        match = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", candidate)
        if match is None:
            return None
        try:
            year, month, day = (int(part) for part in match.groups())
            parsed = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def news_source_is_current(source: FactSource, *, now: datetime | None = None) -> bool:
    """News evidence must expose a parseable and recent publication date."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    published = _page_datetime(source.page_time)
    if published is None or published > current + _NEWS_FUTURE_TOLERANCE:
        return False
    return current - published <= NEWS_MAX_AGE


def validate_news_bundle_dates(
    bundle: FactBundle,
    *,
    now: datetime | None = None,
) -> FactBundle:
    """Remove undated/old pages and recompute the news verification gate."""

    sources = tuple(source for source in bundle.sources if news_source_is_current(source, now=now))
    domains = {source.domain for source in sources}
    has_primary = bool(domains.intersection(bundle.authoritative_domains))
    verified = has_primary or len(domains) >= bundle.required_domains
    return FactBundle(
        query=bundle.query,
        intent=bundle.intent,
        sources=sources,
        required_domains=bundle.required_domains,
        authoritative_domains=bundle.authoritative_domains,
        error=(
            None
            if bundle.error is None and verified
            else (
                "无法核实：新闻来源必须包含可解析的页面发布时间，且发布时间不得早于45天；"
                f"当前有效来源{len(domains)}个。"
            )
        ),
    )


class _ReadablePageParser(HTMLParser):
    _BLOCKED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form"}
    _DATE_META_NAMES = {
        "article:published_time",
        "date",
        "datepublished",
        "dc.date",
        "og:published_time",
        "publishdate",
        "pubdate",
        "sailthru.date",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.date_candidates: list[str] = []
        self._blocked: list[str] = []
        self._in_title = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if normalized_tag in self._BLOCKED_TAGS:
            self._blocked.append(normalized_tag)
            return
        if self._blocked:
            return
        if normalized_tag == "title":
            self._in_title = True
        elif normalized_tag == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").casefold()
            content = attributes.get("content", "")
            if name in self._DATE_META_NAMES and content:
                self.date_candidates.append(content)
        elif normalized_tag == "time" and attributes.get("datetime"):
            self.date_candidates.append(attributes["datetime"])

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._blocked:
            if normalized_tag == self._blocked[-1]:
                self._blocked.pop()
            return
        if normalized_tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._blocked:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        else:
            self.text_parts.append(cleaned)


def source_domain(url: str) -> str | None:
    """Return the canonical allowlisted source domain for a safe HTTP URL."""

    try:
        parsed = urlsplit(str(url).strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port not in {80, 443})
    ):
        return None
    hostname = parsed.hostname.rstrip(".").casefold()
    for domain in _ALLOWED_SOURCE_DOMAINS:
        if hostname == domain or hostname.endswith(f".{domain}"):
            return domain
    return None


def _normalise_page_time(value: object) -> str | None:
    cleaned = clean_search_field(value, 100)
    if not cleaned:
        return None
    candidate = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # Preserve a bounded date-like value instead of guessing its timezone.
        if re.search(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", cleaned):
            return cleaned
        return None
    return parsed.isoformat(timespec="seconds")


def parse_fact_page(html: str, *, fallback_title: str = "") -> tuple[str, str, str | None]:
    """Extract readable body text and publication time from bounded HTML."""

    bounded_html = str(html)[:_MAX_PAGE_BYTES]
    parser = _ReadablePageParser()
    try:
        parser.feed(bounded_html)
        parser.close()
    except (ValueError, TypeError) as exc:
        raise FactSourceError("page HTML could not be parsed") from exc

    title = clean_search_field(" ".join(parser.title_parts) or fallback_title, 240)
    body = clean_search_field(" ".join(parser.text_parts), _MAX_BODY_CHARS)
    if len(body) < _MIN_BODY_CHARS:
        raise FactSourceError("page body is too short to verify")

    date_candidates = list(parser.date_candidates)
    if match := _DATE_PUBLISHED_RE.search(bounded_html):
        date_candidates.insert(0, match.group(1))
    page_time = next(
        (
            normalized
            for value in date_candidates
            if (normalized := _normalise_page_time(value)) is not None
        ),
        None,
    )
    return title, body, page_time


async def fetch_fact_source(result: SearchResult) -> FactSource:
    """Fetch one allowlisted page body with bounded redirects and response size."""

    current_url = result.url
    domain = source_domain(current_url)
    if domain is None:
        raise FactSourceError("source URL is not allowlisted")

    async with _FETCH_SEMAPHORE:
        async with asyncio.timeout(20.0):
            redirect_count = 0
            recovery_retry_available = True
            while redirect_count <= _MAX_REDIRECTS:
                current_domain = source_domain(current_url)
                if current_domain is None:
                    raise FactSourceError("redirect left the source allowlist")
                redirected = False
                try:
                    async with lease_http_client() as client:
                        async with client.stream(
                            "GET",
                            current_url,
                            headers={
                                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                                "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6",
                            },
                            timeout=_FETCH_TIMEOUT,
                        ) as response:
                            if response.status_code in _REDIRECT_STATUSES:
                                location = response.headers.get("location")
                                if not location or redirect_count >= _MAX_REDIRECTS:
                                    raise FactSourceError("source redirect could not be followed")
                                current_url = urljoin(current_url, location)
                                redirect_count += 1
                                redirected = True
                            else:
                                response.raise_for_status()
                                content_type = response.headers.get("content-type", "").casefold()
                                if content_type and not (
                                    content_type.startswith("text/")
                                    or "html" in content_type
                                    or "xhtml" in content_type
                                ):
                                    raise FactSourceError("source did not return readable text")

                                payload = bytearray()
                                async for chunk in response.aiter_bytes(chunk_size=65_536):
                                    payload.extend(chunk)
                                    if len(payload) > _MAX_PAGE_BYTES:
                                        raise FactSourceError("source page is too large")
                                encoding = response.encoding or "utf-8"
                except httpx.TransportError:
                    if not recovery_retry_available:
                        raise
                    recovery_retry_available = False
                    await asyncio.sleep(0.1)
                    continue
                if redirected:
                    continue
                try:
                    html = bytes(payload).decode(encoding, errors="replace")
                except LookupError:
                    html = bytes(payload).decode("utf-8", errors="replace")
                title, body, page_time = parse_fact_page(
                    html,
                    fallback_title=result.title or current_domain,
                )
                fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return FactSource(
                    title=title or current_domain,
                    url=current_url,
                    domain=current_domain,
                    body=body,
                    fetched_at=fetched_at,
                    page_time=page_time,
                )
    # Unreachable today (the redirect branch raises before the loop can
    # exhaust), but kept as the typed terminal for the fetch state machine.
    raise FactSourceError("source fetch ended unexpectedly")


SearchProvider = Callable[[str, int], Awaitable[list[SearchResult]]]
SourceFetcher = Callable[[SearchResult], Awaitable[FactSource]]


def build_fact_search_queries(query: str, intent: str = "general") -> tuple[str, ...]:
    """Build domain-specific discovery queries for a classified factual intent."""

    clean_query = clean_search_field(query, 300)
    if not clean_query:
        return ()
    normalized_intent = clean_search_field(intent, 40).casefold() or "general"
    year = datetime.now(timezone.utc).year
    if normalized_intent in {"player", "player_team", "team_player", "roster", "team"}:
        return (
            f"CS2 {clean_query} current team roster {year} site:hltv.org",
            f"CS2 {clean_query} roster {year} site:liquipedia.net",
            f"CS2 {clean_query} {year} site:blast.tv OR site:esl.com",
        )
    if normalized_intent in {"update", "version", "game_update"}:
        return (
            f"Counter-Strike 2 {clean_query} {year} site:counter-strike.net",
            f"Counter-Strike 2 {clean_query} {year} site:store.steampowered.com",
            f"Counter-Strike 2 {clean_query} {year} site:steamcommunity.com",
        )
    if normalized_intent in {"event", "match", "tournament", "esports"}:
        return (
            f"CS2 {clean_query} {year} site:hltv.org",
            f"CS2 {clean_query} {year} site:liquipedia.net",
            f"CS2 {clean_query} {year} site:blast.tv OR site:esl.com",
        )
    if normalized_intent in {"price", "market", "market_price"}:
        return (
            f"CS2 {clean_query} price site:steamcommunity.com/market",
            f"CS2 {clean_query} price site:steamdt.com",
        )
    return (
        f"Counter-Strike 2 {clean_query} {year} site:counter-strike.net",
        f"Counter-Strike 2 {clean_query} {year} site:steamcommunity.com OR site:store.steampowered.com",
        f"CS2 {clean_query} {year} site:hltv.org",
        f"CS2 {clean_query} {year} site:liquipedia.net",
    )


class FactSourceService:
    """Collect independently hosted, full-page evidence for one fact query."""

    def __init__(
        self,
        *,
        searcher: SearchProvider = web_search_results,
        fetcher: SourceFetcher = fetch_fact_source,
        max_sources: int = 4,
    ) -> None:
        self.searcher = searcher
        self.fetcher = fetcher
        self.max_sources = max(2, min(int(max_sources), 8))

    async def collect(
        self,
        query: str,
        *,
        intent: str = "general",
        min_domains: int = 2,
        authoritative_domains: Sequence[str] = (),
        search_queries: Sequence[str] | None = None,
    ) -> FactBundle:
        clean_query = clean_search_field(query, 300)
        required_domains = max(1, min(int(min_domains), self.max_sources))
        primary_domains = tuple(
            dict.fromkeys(
                domain
                for value in authoritative_domains
                if (domain := clean_search_field(value, 100).rstrip(".").casefold())
                in _ALLOWED_SOURCE_DOMAINS
            )
        )
        queries = tuple(search_queries or build_fact_search_queries(clean_query, intent))
        if not clean_query or not queries:
            return FactBundle(
                query=clean_query,
                intent=intent,
                sources=(),
                required_domains=required_domains,
                authoritative_domains=primary_domains,
                error="无法核实：事实查询为空或没有可用来源。",
            )

        search_batches = await asyncio.gather(
            *(self.searcher(search_query, 5) for search_query in queries),
            return_exceptions=True,
        )
        candidates: list[SearchResult] = []
        seen_urls: set[str] = set()
        candidates_per_domain: dict[str, int] = {}
        for batch in search_batches:
            if isinstance(batch, BaseException):
                logger.warning("Fact discovery failed error=%s", type(batch).__name__)
                continue
            for candidate in batch:
                candidate_domain = source_domain(candidate.url)
                if candidate_domain is None or candidate.url in seen_urls:
                    continue
                if candidates_per_domain.get(candidate_domain, 0) >= 2:
                    continue
                seen_urls.add(candidate.url)
                candidates_per_domain[candidate_domain] = (
                    candidates_per_domain.get(candidate_domain, 0) + 1
                )
                candidates.append(candidate)

        fetched = await asyncio.gather(
            *(self.fetcher(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        by_domain: dict[str, FactSource] = {}
        for candidate, result in zip(candidates, fetched, strict=True):
            if isinstance(result, BaseException):
                logger.info(
                    "Fact source fetch failed domain=%s error=%s",
                    source_domain(candidate.url) or "rejected",
                    type(result).__name__,
                )
                continue
            actual_domain = source_domain(result.url)
            if actual_domain is None or result.domain != actual_domain:
                logger.warning("Fact fetcher returned inconsistent source provenance")
                continue
            if not source_is_relevant(result, clean_query, intent):
                logger.info(
                    "Fact source rejected as irrelevant domain=%s intent=%s",
                    actual_domain,
                    intent,
                )
                continue
            by_domain.setdefault(actual_domain, result)

        sources = tuple(list(by_domain.values())[: self.max_sources])
        has_primary = bool(set(by_domain).intersection(primary_domains))
        if not has_primary and len(by_domain) < required_domains:
            if primary_domains:
                policy = (
                    "需要一个指定官方一手来源（"
                    + "、".join(primary_domains)
                    + f"）或至少{required_domains}个不同域名的可读取正文"
                )
            else:
                policy = f"至少需要{required_domains}个不同域名的可读取正文"
            return FactBundle(
                query=clean_query,
                intent=intent,
                sources=sources,
                required_domains=required_domains,
                authoritative_domains=primary_domains,
                error=(f"无法核实：{policy}，当前仅获取到{len(by_domain)}个。"),
            )
        return FactBundle(
            query=clean_query,
            intent=intent,
            sources=sources,
            required_domains=required_domains,
            authoritative_domains=primary_domains,
        )


fact_source_service = FactSourceService()


async def collect_fact_sources(
    query: str,
    intent: str = "general",
    *,
    min_domains: int = 2,
    authoritative_domains: Sequence[str] = (),
) -> FactBundle:
    """Public factual-router API returning evidence and verification state."""

    return await fact_source_service.collect(
        query,
        intent=intent,
        min_domains=min_domains,
        authoritative_domains=authoritative_domains,
    )


async def collect_news_facts(query: str) -> FactBundle:
    """Collect two-domain page evidence for the dedicated CS2 news command."""

    bundle = await collect_fact_sources(query, "news", min_domains=2)
    return validate_news_bundle_dates(bundle)


__all__ = [
    "FactBundle",
    "FactSource",
    "FactSourceError",
    "FactSourceService",
    "build_fact_search_queries",
    "collect_fact_sources",
    "collect_news_facts",
    "fact_query_anchors",
    "fact_source_service",
    "fetch_fact_source",
    "news_source_is_current",
    "parse_fact_page",
    "source_is_relevant",
    "source_domain",
    "validate_news_bundle_dates",
]

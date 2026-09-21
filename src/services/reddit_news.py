"""Reddit community-news pipeline: fetch Atom feeds, filter, dedupe, format.

Feeds are pulled on demand from the ``CS2爆料`` command family.  Every
subreddit is refetched at most once per poll interval (hard floor 60s to
respect the unauthenticated rate limit), responses travel through the
shared HTTP client with proxy self-healing, and a dedicated circuit
breaker opens on repeated failures.  Output is always labelled as
community speculation, never as official confirmation.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from src.services.config import bounded_env_int
from src.state.reddit_post_repository import RedditPost, RedditPostRepository

from .http_client import request_text
from .resilience import CircuitOpenError, get_circuit_breaker

logger = logging.getLogger("taffy.services.reddit_news")

DEFAULT_SUBREDDITS = ("GlobalOffensive", "cs2", "csgomarketforum")
DEFAULT_KEYWORDS = (
    "update",
    "leak",
    "case",
    "skin",
    "price",
    "market",
    "patch",
    "rumer",
    "rumor",
    "trade",
    "trading",
    "cashout",
    "money",
    "更新",
    "爆料",
    "饰品",
    "价格",
    "市场",
    "箱子",
    "交易",
)

POLL_INTERVAL_SECONDS = bounded_env_int("TAFFY_REDDIT_POLL_INTERVAL", 300, 60, 86_400)
FEED_ENTRY_LIMIT = bounded_env_int("TAFFY_REDDIT_FEED_LIMIT", 25, 5, 100)
MAX_REPORT_POSTS = bounded_env_int("TAFFY_REDDIT_MAX_POSTS", 3, 1, 10)
RETENTION_SECONDS = bounded_env_int(
    "TAFFY_REDDIT_RETENTION_SECONDS", 30 * 86_400, 86_400, 400 * 86_400
)
_CIRCUIT_KEY = "reddit-rss"
_FEED_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/new/.rss?limit={limit}"
_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; taffy-bot/1.0)"}
_FETCH_TIMEOUT_SECONDS = 20.0
_MAX_TITLE_LENGTH = 300

TextFetcher = Callable[[str], Awaitable[str]]


class RedditNewsUnavailable(RuntimeError):
    """Raised when feeds cannot be fetched; the command must fail closed."""


async def _default_fetch(url: str) -> str:
    return await request_text(
        "GET",
        url,
        attempts=2,
        total_timeout=_FETCH_TIMEOUT_SECONDS,
        headers=_REQUEST_HEADERS,
    )


def _subreddits() -> tuple[str, ...]:
    raw = os.environ.get("TAFFY_REDDIT_SUBREDDITS", "").strip()
    if not raw:
        return DEFAULT_SUBREDDITS
    cleaned = []
    for part in raw.replace("，", ",").split(","):
        name = part.strip().strip("/").removeprefix("r/").removeprefix("R/")
        if name and len(name) <= 40 and name not in cleaned:
            cleaned.append(name)
    return tuple(cleaned[:10]) or DEFAULT_SUBREDDITS


def _parse_timestamp(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


@dataclass(frozen=True, slots=True)
class AtomEntry:
    post_id: str
    title: str
    summary: str
    author: str
    url: str
    published_at: int


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _extract_markdown_text(content_html: str, *, maximum: int = 150) -> str:
    """Extract the post selftext from Reddit's HTML ``<content>`` field.

    The feed embeds the Markdown-rendered body inside ``<div class="md">``.
    Unescape entities, drop tags, collapse whitespace, and trim to a bounded
    summary.  No extra network request is required.
    """

    if not content_html:
        return ""
    match = re.search(r'<div class="md">(.*?)</div>', content_html, re.DOTALL)
    fragment = match.group(1) if match is not None else content_html
    text = html.unescape(_TAG_RE.sub(" ", fragment))
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:maximum]


def parse_atom_entries(xml_text: str, subreddit: str) -> tuple[AtomEntry, ...]:
    """Extract validated entries from one Reddit Atom feed document."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError("reddit feed is not valid XML") from exc
    entries: list[AtomEntry] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if tag != "entry":
            continue
        fields: dict[str, str] = {}
        for child in element:
            child_tag = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
            if child_tag in {"id", "title", "updated", "published"} and child.text:
                fields[child_tag] = child.text.strip()
            elif child_tag == "author":
                name = child.find("{http://www.w3.org/2005/Atom}name")
                if name is not None and name.text:
                    fields["author"] = name.text.strip()
            elif child_tag == "content":
                fields["content"] = child.text or ""
            elif child_tag == "link":
                href = child.get("href", "")
                if href and "href" not in fields:
                    fields["href"] = href.strip()
        post_id = fields.get("id", "")
        title = " ".join(fields.get("title", "").split())[:_MAX_TITLE_LENGTH]
        summary = _extract_markdown_text(fields.get("content", ""))
        url = fields.get("href", "") or post_id
        published = _parse_timestamp(fields.get("published") or fields.get("updated"))
        if not post_id.startswith("t3_") or not title or not url.startswith("https://"):
            continue
        entries.append(
            AtomEntry(
                post_id=post_id[:40],
                title=title,
                summary=summary,
                author=(fields.get("author") or "")[:120],
                url=url[:500],
                published_at=published if published > 0 else int(time.time()),
            )
        )
    return tuple(entries)


def matches_keywords(title: str, keywords: Sequence[str] = DEFAULT_KEYWORDS) -> bool:
    """Case-insensitive substring match over the configured keyword list."""

    lowered = title.casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def feed_url(subreddit: str, limit: int = FEED_ENTRY_LIMIT) -> str:
    return _FEED_URL_TEMPLATE.format(subreddit=quote(subreddit, safe=""), limit=int(limit))


def format_community_report(posts: Sequence[Mapping[str, Any]]) -> str:
    """Render labelled community posts; never claims official confirmation."""

    if not posts:
        return (
            "📢 【社区爆料】Reddit\n──────────\n"
            "暂无新的社区爆料（已播报过的不会重复）。\n"
            "ℹ️ 数据来自 Reddit 社区帖，非官方确认。"
        )
    lines = ["📢 【社区爆料】Reddit", "──────────"]
    for index, post in enumerate(posts, 1):
        author = str(post.get("author") or "").strip().removeprefix("/u/")
        by_line = f" by /u/{author}" if author else ""
        lines.append(f"{index}. {str(post['title'])}{by_line}")
        summary = str(post.get("summary") or "").strip()
        if summary:
            lines.append(f"   {summary}")
        lines.append(f"   来源：r/{str(post['subreddit'])}｜{str(post['url'])}")
    lines.append("──────────")
    lines.append("⚠️ 注意：社区爆料，非官方确认")
    return "\n".join(lines)


class RedditNewsService:
    """On-demand feed sync with per-subreddit rate limiting and dedupe."""

    def __init__(
        self,
        repository: RedditPostRepository,
        *,
        fetcher: TextFetcher = _default_fetch,
        wall_clock: Callable[[], float] = time.time,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        subreddits: tuple[str, ...] | None = None,
    ) -> None:
        self._repository = repository
        self._fetcher = fetcher
        self._wall_clock = wall_clock
        self._poll_interval = max(60, int(poll_interval))
        self._subreddits = subreddits
        self._last_fetch: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._warmup_task: asyncio.Task[None] | None = None

    async def sync_feeds(self, *, max_sources: int = 0) -> int:
        """Fetch stale subreddits sequentially and ingest filtered posts.

        ``max_sources`` bounds how many stale sources one call may touch;
        the background warmup uses one per tick so unauthenticated Reddit
        rate limiting never sees bursts.
        """

        breaker = get_circuit_breaker(_CIRCUIT_KEY)
        try:
            await breaker.acquire()
        except CircuitOpenError as exc:
            raise RedditNewsUnavailable("reddit feed circuit is open") from exc
        ingested = 0
        attempted = 0
        fetch_failures = 0
        subreddits = self._subreddits or _subreddits()
        try:
            for index, subreddit in enumerate(subreddits):
                now = self._wall_clock()
                if now - self._last_fetch.get(subreddit, 0.0) < self._poll_interval:
                    continue
                if max_sources > 0 and attempted >= max_sources:
                    break
                attempted += 1
                try:
                    body = await self._fetcher(feed_url(subreddit))
                except Exception as exc:
                    fetch_failures += 1
                    await breaker.fail()
                    logger.warning(
                        "Reddit feed fetch failed subreddit=%s error=%s",
                        subreddit,
                        type(exc).__name__,
                    )
                    # Back the source off a full poll interval even on failure
                    # so a dead feed is not retried on every scheduler tick.
                    self._last_fetch[subreddit] = self._wall_clock()
                    continue
                entries = [
                    entry
                    for entry in parse_atom_entries(body, subreddit)
                    if matches_keywords(entry.title)
                ]
                if entries:
                    ingested += await self._repository.insert_posts(
                        [
                            RedditPost(
                                post_id=entry.post_id,
                                subreddit=subreddit,
                                title=entry.title,
                                summary=entry.summary,
                                author=entry.author,
                                url=entry.url,
                                published_at=entry.published_at,
                            )
                            for entry in entries
                        ],
                        now=int(self._wall_clock()),
                    )
                self._last_fetch[subreddit] = self._wall_clock()
                # The inter-source pause only matters before the next request;
                # sleeping after the final source just delays the reply.
                if index < len(subreddits) - 1:
                    await asyncio.sleep(2.0)
            await self._repository.cleanup_before(int(self._wall_clock()) - RETENTION_SECONDS)
        except Exception as exc:
            await breaker.fail()
            logger.warning("Reddit feed sync failed error=%s", type(exc).__name__)
            raise RedditNewsUnavailable("reddit feed sync failed") from exc
        if attempted > 0 and fetch_failures == attempted:
            # Every source failed this round; keep the failure window so the
            # breaker can open instead of resetting it via succeed().
            raise RedditNewsUnavailable("all reddit feeds failed")
        await breaker.succeed()
        return ingested

    def schedule_sync(self) -> None:
        """Start a single-flight background feed sync; never blocks callers."""

        task = self._warmup_task
        if task is not None and not task.done():
            return
        self._warmup_task = asyncio.create_task(self._sync_in_background())

    async def _sync_in_background(self) -> None:
        try:
            await self.sync_feeds(max_sources=1)
        except RedditNewsUnavailable:
            pass
        except Exception as exc:
            logger.warning("Background reddit sync failed error=%s", type(exc).__name__)

    async def community_report(self) -> str:
        """Render undelivered posts from the database, syncing only when cold."""

        posts = await self._repository.undelivered_posts(MAX_REPORT_POSTS)
        if not posts:
            # Syncing can take tens of seconds; keep it outside the claim
            # critical section so concurrent commands never serialize behind
            # a full feed refresh.
            await self.sync_feeds()
            posts = await self._repository.undelivered_posts(MAX_REPORT_POSTS)
        if not posts:
            return format_community_report([])
        async with self._lock:
            # Re-read under the claim lock: only one command may deliver and
            # mark a given batch, even when several arrive together.
            fresh = await self._repository.undelivered_posts(MAX_REPORT_POSTS)
            if not fresh:
                return format_community_report([])
            rendered = format_community_report(
                [
                    {
                        "subreddit": post.subreddit,
                        "title": post.title,
                        "summary": post.summary,
                        "author": post.author,
                        "url": post.url,
                    }
                    for post in fresh
                ]
            )
            await self._repository.mark_delivered([post.post_id for post in fresh])
        self.schedule_sync()
        return rendered


_shared_service: RedditNewsService | None = None


def reddit_news_service() -> RedditNewsService:
    """Lazily build the process-wide service bound to the runtime repository."""

    global _shared_service
    if _shared_service is None:
        from src.state.runtime import reddit_posts

        _shared_service = RedditNewsService(reddit_posts)
    return _shared_service


__all__ = [
    "AtomEntry",
    "DEFAULT_KEYWORDS",
    "DEFAULT_SUBREDDITS",
    "RedditNewsService",
    "RedditNewsUnavailable",
    "feed_url",
    "format_community_report",
    "matches_keywords",
    "parse_atom_entries",
    "reddit_news_service",
]

"""Bounded web-search adapter for the synchronous DDGS client."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ddgs import DDGS

from .config import bounded_env_float, bounded_env_int

logger = logging.getLogger("taffy.services.search")

_SEARCH_TIMEOUT = bounded_env_float("WEB_SEARCH_TIMEOUT", 12.0, 3.0, 30.0)
_SEARCH_SEMAPHORE = asyncio.Semaphore(bounded_env_int("WEB_SEARCH_MAX_CONCURRENCY", 2, 1, 8))


def clean_search_field(value: object, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One bounded search-engine result before its page is fetched."""

    title: str
    snippet: str
    url: str

    def render(self, index: int) -> str:
        return f"[{index}] {self.title}\n{self.snippet}\n来源: {self.url}"


class SearchService:
    """Run blocking DDGS calls off-loop with inner and outer deadlines."""

    def __init__(
        self,
        *,
        timeout: float = _SEARCH_TIMEOUT,
        semaphore: asyncio.Semaphore = _SEARCH_SEMAPHORE,
    ) -> None:
        self.timeout = min(30.0, max(3.0, float(timeout)))
        self.semaphore = semaphore

    async def results(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Return structured, bounded search hits without trusting their snippets."""

        clean_query = str(query).strip()[:500]
        if not clean_query:
            return []
        bounded_results = max(1, min(int(max_results), 5))

        def run() -> list[dict[str, Any]]:
            # The inner timeout bounds the worker thread even after the outer
            # coroutine has been cancelled by asyncio.wait_for.
            with DDGS(timeout=max(1, int(self.timeout - 1))) as ddgs:
                return list(ddgs.text(clean_query, max_results=bounded_results))

        try:
            async with self.semaphore:
                # On timeout the coroutine abandons the worker while its thread
                # keeps running until DDGS's own timeout fires, so the
                # semaphore slot frees before the thread dies and a burst of
                # timeouts can briefly exceed the intended concurrency.
                # Accepted: each thread is still hard-bounded by the inner
                # timeout above, and releasing from a completion callback
                # would risk leaking slots permanently if it never ran.
                results = await asyncio.wait_for(
                    asyncio.to_thread(run),
                    timeout=self.timeout,
                )
        except Exception as exc:
            logger.warning("Web search failed error=%s", type(exc).__name__)
            return []

        structured: list[SearchResult] = []
        for result in results[:5]:
            if not isinstance(result, dict):
                continue
            title = clean_search_field(result.get("title"), 180)
            snippet = clean_search_field(result.get("body"), 500)
            href = clean_search_field(result.get("href"), 500)
            if title or snippet:
                structured.append(SearchResult(title=title, snippet=snippet, url=href))
        return structured

    async def search(self, query: str, max_results: int = 5) -> str:
        """Keep the historical text interface for non-factual chat enrichment."""

        results = await self.results(query, max_results)
        return "\n\n".join(result.render(index) for index, result in enumerate(results, 1))


search_service = SearchService()


async def web_search(query: str, max_results: int = 5) -> str:
    return await search_service.search(query, max_results)


async def web_search_results(query: str, max_results: int = 5) -> list[SearchResult]:
    """Return structured hits for callers that will fetch and verify page bodies."""

    return await search_service.results(query, max_results)


__all__ = [
    "SearchResult",
    "SearchService",
    "clean_search_field",
    "search_service",
    "web_search",
    "web_search_results",
]

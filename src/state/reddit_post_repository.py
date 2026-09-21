"""SQLite repository for deduplicated Reddit community posts.

Rows are inserted once per Reddit post id (``t3_...``).  A post becomes
"delivered" the first time it appears in a command reply; subsequent
commands only surface undelivered posts, so the same leak never repeats.
Retention is bounded by ``cleanup_before`` from the ingest path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from src.core.database import StateDatabase, Transaction

logger = logging.getLogger("taffy.state.reddit_posts")

_MAX_POST_ROWS = 100
#: Rows per multi-row INSERT in :meth:`insert_posts`.
_INSERT_CHUNK = 50
#: Upper bound per DELETE statement in :meth:`cleanup_before`.
_CLEANUP_BATCH = 10_000


class RedditPostError(RuntimeError):
    """Base class for reddit-post persistence failures."""


@dataclass(frozen=True, slots=True)
class RedditPost:
    post_id: str
    subreddit: str
    title: str
    summary: str
    author: str
    url: str
    published_at: int


@dataclass(frozen=True, slots=True)
class StoredRedditPost:
    post_id: str
    subreddit: str
    title: str
    summary: str
    author: str
    url: str
    published_at: int
    first_seen_at: int


def _clean_text(value: object, name: str, *, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise RedditPostError(f"{name} must be a string")
    cleaned = " ".join(value.split())
    if not minimum <= len(cleaned) <= maximum:
        raise RedditPostError(f"{name} length is out of range")
    return cleaned


def _optional_text(value: object, name: str, *, maximum: int) -> str:
    """Like ``_clean_text`` but allows an empty string (schema default)."""

    if not isinstance(value, str):
        raise RedditPostError(f"{name} must be a string")
    cleaned = " ".join(value.split())
    if len(cleaned) > maximum:
        raise RedditPostError(f"{name} length is out of range")
    return cleaned


def _validated_post(post: RedditPost) -> tuple[str, str, str, str, str, str, int]:
    # Bounds mirror the reddit_posts CHECK constraints so invalid rows fail
    # validation (RedditPostError) instead of aborting the whole insert batch
    # with a mid-transaction IntegrityError.
    post_id = _clean_text(post.post_id, "post_id", minimum=3, maximum=40)
    subreddit = _clean_text(post.subreddit, "subreddit", minimum=2, maximum=40)
    title = _clean_text(post.title, "title", maximum=500)
    summary = _optional_text(post.summary or "", "summary", maximum=300)
    author = _optional_text(post.author or "", "author", maximum=120)
    url = _clean_text(post.url, "url", minimum=8, maximum=500)
    if not url.startswith(("http://", "https://")):
        raise RedditPostError("url must be an absolute http(s) URL")
    published_at = int(post.published_at)
    if published_at <= 0:
        raise RedditPostError("published_at must be a positive epoch second")
    return post_id, subreddit, title, summary, author, url, published_at


class RedditPostRepository:
    """Insert-once ledger with delivery bookkeeping for community posts."""

    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    async def insert_posts(
        self,
        posts: list[RedditPost],
        *,
        now: int | None = None,
    ) -> int:
        """Insert new posts; existing ids are ignored. Returns inserted count."""

        timestamp = int(time.time() if now is None else now)
        rows = [_validated_post(post) for post in posts[:_MAX_POST_ROWS]]
        if not rows:
            return 0

        def insert(transaction: Transaction) -> int:
            inserted = 0
            # Chunked multi-row INSERT keeps statement count flat in batch size.
            for chunk_start in range(0, len(rows), _INSERT_CHUNK):
                chunk = rows[chunk_start : chunk_start + _INSERT_CHUNK]
                value_rows = ",".join("(?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk)
                parameters: list[object] = []
                for row in chunk:
                    parameters.extend((*row, timestamp))
                result = transaction.write(
                    f"""
                    INSERT INTO reddit_posts (
                        post_id, subreddit, title, summary, author, url,
                        published_at, first_seen_at
                    ) VALUES {value_rows}
                    ON CONFLICT(post_id) DO NOTHING
                    """,  # noqa: S608 - placeholder scaffolding is a fixed constant
                    tuple(parameters),
                )
                inserted += max(result.rowcount, 0)
            return inserted

        return await self.database.transaction(insert)

    async def undelivered_posts(
        self,
        limit: int,
        *,
        subreddit: str | None = None,
    ) -> tuple[StoredRedditPost, ...]:
        """Return newest undelivered posts, optionally scoped to one subreddit."""

        bounded = max(1, min(int(limit), 20))
        if subreddit is not None:
            rows = await self.database.read(
                """
                SELECT post_id, subreddit, title, summary, author, url,
                       published_at, first_seen_at
                FROM reddit_posts
                WHERE delivered_at IS NULL AND subreddit = ?
                ORDER BY published_at DESC, post_id
                LIMIT ?
                """,
                (subreddit, bounded),
            )
        else:
            rows = await self.database.read(
                """
                SELECT post_id, subreddit, title, summary, author, url,
                       published_at, first_seen_at
                FROM reddit_posts
                WHERE delivered_at IS NULL
                ORDER BY published_at DESC, post_id
                LIMIT ?
                """,
                (bounded,),
            )
        return tuple(
            StoredRedditPost(
                post_id=str(row["post_id"]),
                subreddit=str(row["subreddit"]),
                title=str(row["title"]),
                summary=str(row["summary"] or ""),
                author=str(row["author"] or ""),
                url=str(row["url"]),
                published_at=int(row["published_at"]),
                first_seen_at=int(row["first_seen_at"]),
            )
            for row in rows
        )

    async def mark_delivered(
        self,
        post_ids: list[str],
        *,
        now: int | None = None,
    ) -> int:
        """Flag posts as delivered so they never repeat in later replies."""

        cleaned = [post_id.strip() for post_id in post_ids if 3 <= len(post_id.strip()) <= 40]
        if not cleaned:
            return 0
        if len(post_ids) > _MAX_POST_ROWS:
            logger.warning(
                "mark_delivered received %s post ids; only the first %s are considered",
                len(post_ids),
                _MAX_POST_ROWS,
            )
        timestamp = int(time.time() if now is None else now)
        placeholders = ",".join("?" for _ in cleaned)

        def update(transaction: Transaction) -> int:
            # ``delivered_at IS NULL`` keeps the first delivery timestamp
            # authoritative instead of letting a duplicate mark overwrite it.
            result = transaction.write(
                f"""
                UPDATE reddit_posts
                SET delivered_at = ?
                WHERE post_id IN ({placeholders}) AND delivered_at IS NULL
                """,  # noqa: S608 - placeholder scaffolding is a fixed constant
                (timestamp, *cleaned),
            )
            return max(result.rowcount, 0)

        return await self.database.transaction(update)

    async def cleanup_before(self, cutoff: int) -> int:
        """Delete rows first seen before ``cutoff`` (retention bound).

        Bounded batches keep any single write transaction short.
        """

        def delete(transaction: Transaction) -> int:
            removed_total = 0
            while True:
                result = transaction.write(
                    """
                    DELETE FROM reddit_posts
                    WHERE post_id IN (
                        SELECT post_id FROM reddit_posts
                        WHERE first_seen_at < ? LIMIT ?
                    )
                    """,
                    (int(cutoff), _CLEANUP_BATCH),
                )
                removed = max(result.rowcount, 0)
                removed_total += removed
                if removed < _CLEANUP_BATCH:
                    return removed_total

        return await self.database.transaction(delete)

    async def total_count(self) -> int:
        rows: list[dict[str, Any]] = await self.database.read(
            "SELECT COUNT(*) AS total FROM reddit_posts"
        )
        return int(rows[0]["total"]) if rows else 0


__all__ = ["RedditPost", "RedditPostError", "RedditPostRepository", "StoredRedditPost"]

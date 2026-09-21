"""Tests for the Reddit community-news pipeline."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from src.core.database import SCHEMA_VERSION, StateDatabase
from src.services.reddit_news import (
    RedditNewsService,
    RedditNewsUnavailable,
    feed_url,
    format_community_report,
    matches_keywords,
    parse_atom_entries,
)
from src.state.reddit_post_repository import RedditPost, RedditPostRepository

_ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>new links from r/cs2</title>
  <entry>
    <id>t3_1abc001</id>
    <title>New case leak: upcoming skin prices discussed</title>
    <updated>2026-08-17T01:00:00Z</updated>
    <link href="https://www.reddit.com/r/cs2/comments/1abc001/leak/" />
    <author><name>leak_hunter</name></author>
    <content type="html">&lt;div class="md"&gt;&lt;p&gt;Valve added a new case with rare skins and stickers inside.&lt;/p&gt;&lt;/div&gt;</content>
  </entry>
  <entry>
    <id>t3_1abc002</id>
    <title>Just hit global, ask me anything</title>
    <published>2026-08-17T02:00:00Z</published>
    <link href="https://www.reddit.com/r/cs2/comments/1abc002/ama/" />
    <author><name>random_player</name></author>
  </entry>
  <entry>
    <id>not-a-t3-id</id>
    <title>Invalid entry should be skipped</title>
    <link href="https://www.reddit.com/r/cs2/comments/x/" />
  </entry>
</feed>
"""


def test_parse_atom_entries_extracts_valid_fields() -> None:
    from datetime import datetime, timezone

    entries = parse_atom_entries(_ATOM_FEED, "cs2")
    assert [entry.post_id for entry in entries] == ["t3_1abc001", "t3_1abc002"]
    assert entries[0].title == "New case leak: upcoming skin prices discussed"
    assert entries[0].author == "leak_hunter"
    assert entries[0].url == "https://www.reddit.com/r/cs2/comments/1abc001/leak/"
    assert entries[0].summary == "Valve added a new case with rare skins and stickers inside."
    assert entries[1].summary == ""
    expected = int(datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc).timestamp())
    assert entries[0].published_at == expected
    assert "Invalid entry" not in " ".join(entry.title for entry in entries)


def test_summary_extraction_strips_html_and_bounds_length() -> None:
    from src.services.reddit_news import _extract_markdown_text

    html_body = (
        '<div class="md"><p>First paragraph &amp; entities.</p>'
        "<p>Second <strong>bold</strong> text.</p></div>"
    )
    assert _extract_markdown_text(html_body) == ("First paragraph & entities. Second bold text.")
    assert _extract_markdown_text("") == ""
    long_text = "<p>" + "word " * 200 + "</p>"
    assert len(_extract_markdown_text(long_text)) == 150


def test_parse_atom_entries_rejects_invalid_xml() -> None:
    with pytest.raises(ValueError, match="not valid XML"):
        parse_atom_entries("<feed><entry>", "cs2")


def test_matches_keywords_covers_english_and_chinese() -> None:
    assert matches_keywords("New Case Reveal incoming")
    assert matches_keywords("饰品价格波动讨论")
    assert matches_keywords("market is heating up")
    assert not matches_keywords("Just hit global, ask me anything")


def test_feed_url_quotes_subreddit() -> None:
    assert feed_url("cs2", 25) == "https://www.reddit.com/r/cs2/new/.rss?limit=25"


def test_format_community_report_labels_sources_and_disclaimer() -> None:
    empty = format_community_report([])
    assert "暂无新的社区爆料" in empty and "非官方确认" in empty

    filled = format_community_report(
        [
            {
                "subreddit": "cs2",
                "title": "New case leak",
                "summary": "A new case with rare skins is coming.",
                "author": "/u/hunter",
                "url": "https://www.reddit.com/r/cs2/comments/1/",
            }
        ]
    )
    assert "【社区爆料】" in filled
    assert "by /u/hunter" in filled
    assert "A new case with rare skins is coming." in filled
    assert "来源：r/cs2｜https://www.reddit.com/r/cs2/comments/1/" in filled
    assert "社区爆料，非官方确认" in filled


async def _make_service(
    tmp_path: Path,
    *,
    fetcher,
    clock=None,
    poll_interval: int = 60,
) -> tuple[RedditNewsService, RedditPostRepository]:
    database = StateDatabase(
        tmp_path / "state.db",
        allow_schema_migrations=True,
    )
    await database.initialize()
    repository = RedditPostRepository(database)
    now = {"value": 1_800_000_000.0}
    service = RedditNewsService(
        repository,
        fetcher=fetcher,
        wall_clock=clock or (lambda: now["value"]),
        poll_interval=poll_interval,
        subreddits=("cs2",),
    )
    return service, repository


@pytest.mark.asyncio
async def test_sync_ingests_filters_and_deduplicates(tmp_path: Path) -> None:
    calls = {"count": 0}

    async def fetcher(url: str) -> str:
        calls["count"] += 1
        return _ATOM_FEED

    service, repository = await _make_service(tmp_path, fetcher=fetcher)
    assert await service.sync_feeds() == 1  # only the leak entry passes keywords
    assert calls["count"] == 1
    assert await repository.total_count() == 1

    # Re-sync inside the poll interval must not refetch.
    assert await service.sync_feeds() == 0
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_community_report_delivers_once_then_reports_empty(
    tmp_path: Path,
) -> None:
    async def fetcher(url: str) -> str:
        return _ATOM_FEED

    service, repository = await _make_service(tmp_path, fetcher=fetcher)
    first = await service.community_report()
    assert "New case leak" in first and "非官方确认" in first
    assert await repository.undelivered_posts(10) == ()

    second = await service.community_report()
    assert "暂无新的社区爆料" in second


@pytest.mark.asyncio
async def test_fetch_failure_fails_closed_and_trips_breaker(tmp_path: Path) -> None:
    async def fetcher(url: str) -> str:
        raise RuntimeError("429 too many requests")

    service, _ = await _make_service(tmp_path, fetcher=fetcher)
    with pytest.raises(RedditNewsUnavailable):
        await service.community_report()


@pytest.mark.asyncio
async def test_repository_cleanup_bounds_retention(tmp_path: Path) -> None:
    async def fetcher(url: str) -> str:
        return _ATOM_FEED

    service, repository = await _make_service(tmp_path, fetcher=fetcher)
    await service.sync_feeds()
    assert await repository.total_count() == 1
    removed = await repository.cleanup_before(1_800_000_000 + 1)
    assert removed == 1
    assert await repository.total_count() == 0


@pytest.mark.asyncio
async def test_fresh_database_reaches_current_schema_version(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "fresh.db", allow_schema_migrations=True)
    await database.initialize()
    rows = await database.read("PRAGMA user_version")
    assert rows[0]["user_version"] == SCHEMA_VERSION
    columns = await database.read("SELECT name FROM pragma_table_info('reddit_posts')")
    assert {row["name"] for row in columns} >= {
        "post_id",
        "subreddit",
        "title",
        "author",
        "url",
        "published_at",
        "first_seen_at",
        "delivered_at",
    }


@pytest.mark.asyncio
async def test_v10_database_migrates_to_v11(tmp_path: Path) -> None:
    database_path = tmp_path / "v10.db"
    database = StateDatabase(database_path, allow_schema_migrations=True)
    await database.initialize()

    import sqlite3

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 10")
    connection.execute("DROP TABLE reddit_posts")
    connection.commit()
    connection.close()

    upgraded = StateDatabase(database_path, allow_schema_migrations=True)
    await upgraded.initialize()
    rows = await upgraded.read("PRAGMA user_version")
    assert rows[0]["user_version"] == SCHEMA_VERSION
    await upgraded.read("SELECT COUNT(*) AS total FROM reddit_posts")


@pytest.mark.asyncio
async def test_repository_rejects_invalid_posts(tmp_path: Path) -> None:
    from src.state.reddit_post_repository import RedditPostError

    database = StateDatabase(tmp_path / "state.db", allow_schema_migrations=True)
    await database.initialize()
    repository = RedditPostRepository(database)
    bad = RedditPost(
        post_id="t3_1",
        subreddit="cs2",
        title="",
        summary="",
        author="a",
        url="https://www.reddit.com/x",
        published_at=int(time.time()),
    )
    with pytest.raises(RedditPostError):
        await repository.insert_posts([bad])


@pytest.mark.asyncio
async def test_repository_validation_matches_schema_bounds(tmp_path: Path) -> None:
    """A11 regression: schema bounds fail validation, not mid-transaction."""

    from src.state.reddit_post_repository import RedditPostError

    database = StateDatabase(tmp_path / "state-bounds.db", allow_schema_migrations=True)
    await database.initialize()
    repository = RedditPostRepository(database)

    def post(
        *, post_id: str = "t3_abc123", subreddit: str = "cs2", summary: str = ""
    ) -> RedditPost:
        return RedditPost(
            post_id=post_id,
            subreddit=subreddit,
            title="标题",
            summary=summary,
            author="作者",
            url="https://www.reddit.com/r/cs2/1/",
            published_at=1_800_000_000,
        )

    # 2-char post_id and 1-char subreddit violate the table CHECK bounds; they
    # must raise the validation error instead of aborting the insert batch.
    with pytest.raises(RedditPostError):
        await repository.insert_posts([post(post_id="ab")])
    with pytest.raises(RedditPostError):
        await repository.insert_posts([post(subreddit="c")])

    # The schema allows an empty summary; validation must accept it too.
    assert await repository.insert_posts([post(summary="")]) == 1
    stored = await repository.undelivered_posts(5)
    assert stored[0].summary == ""


# ─── 插件层：命令规则与 fail-closed ────────────────────────


def test_plugin_rule_matches_exact_commands_only() -> None:
    try:
        import nonebot
    except ValueError:
        import nonebot  # noqa: F811

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=())

    from nonebot.adapters.onebot.v11 import PrivateMessageEvent

    from src.plugins.reddit_news import _is_reddit_news_request

    def event_with(text: str) -> PrivateMessageEvent:
        return PrivateMessageEvent(
            time=1,
            self_id=2,
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=3,
            user_id=101,
            message=text,
            raw_message=text,
            font=0,
            sender={"user_id": 101, "nickname": "u", "sex": "unknown", "age": 0},
        )

    assert _is_reddit_news_request(event_with("CS2爆料"))
    assert _is_reddit_news_request(event_with("社区资讯"))
    assert not _is_reddit_news_request(event_with("CS2爆料 吧"))
    assert not _is_reddit_news_request(event_with("随便聊聊"))


# ─── 修复后行为：代理发现 / 部分失败 / DB 优先 / 预热 ────


def test_env_proxy_url_reads_runtime_settings_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import http_client

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setattr(
        http_client,
        "runtime_setting",
        lambda name: "http://127.0.0.1:7890" if name == "HTTPS_PROXY" else None,
    )
    assert http_client.env_proxy_url() == "http://127.0.0.1:7890"
    monkeypatch.setattr(http_client, "runtime_setting", lambda name: None)
    assert http_client.env_proxy_url() is None


@pytest.mark.asyncio
async def test_sync_continues_when_one_subreddit_fails(tmp_path: Path) -> None:
    from src.core.database import StateDatabase

    database = StateDatabase(tmp_path / "state2.db", allow_schema_migrations=True)
    await database.initialize()
    repository = RedditPostRepository(database)

    good_feed = _ATOM_FEED.replace("t3_1abc001", "t3_1abc009")

    async def fetcher(url: str) -> str:
        if "csgomarketforum" in url:
            raise RuntimeError("HTTP 429")
        return good_feed

    now = {"value": 1_800_000_000.0}
    service = RedditNewsService(
        repository,
        fetcher=fetcher,
        wall_clock=lambda: now["value"],
        poll_interval=60,
        subreddits=("cs2", "csgomarketforum"),
    )
    ingested = await service.sync_feeds()
    assert ingested == 1
    assert await repository.total_count() == 1


@pytest.mark.asyncio
async def test_community_report_reads_database_first_without_fetch(
    tmp_path: Path,
) -> None:
    async def fetcher(url: str) -> str:
        raise AssertionError("warm database must not hit the network")

    service, repository = await _make_service(tmp_path, fetcher=fetcher)
    await repository.insert_posts(
        [
            RedditPost(
                post_id="t3_warm001",
                subreddit="cs2",
                title="Warm case leak",
                summary="A warm post with a summary.",
                author="hunter",
                url="https://www.reddit.com/r/cs2/comments/warm001/",
                published_at=1_800_000_000,
            )
        ],
        now=1_800_000_000,
    )

    report = await asyncio.wait_for(service.community_report(), timeout=2.0)
    assert "Warm case leak" in report
    assert "A warm post with a summary." in report
    assert await repository.undelivered_posts(10) == ()


@pytest.mark.asyncio
async def test_schedule_sync_is_single_flight(tmp_path: Path) -> None:
    started = asyncio.Event()

    release = asyncio.Event()

    async def fetcher(url: str) -> str:
        started.set()
        await release.wait()
        return _ATOM_FEED

    service, _ = await _make_service(tmp_path, fetcher=fetcher)
    service.schedule_sync()
    await started.wait()
    service.schedule_sync()  # must not spawn a second task
    task = service._warmup_task
    release.set()
    await asyncio.wait_for(task, timeout=8.0)
    assert service._warmup_task is task


@pytest.mark.asyncio
async def test_background_warmup_touches_one_source_per_call(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fetcher(url: str) -> str:
        calls.append(url.split("/r/")[1].split("/")[0])
        return _ATOM_FEED

    from src.core.database import StateDatabase

    database = StateDatabase(tmp_path / "state3.db", allow_schema_migrations=True)
    await database.initialize()
    service = RedditNewsService(
        RedditPostRepository(database),
        fetcher=fetcher,
        wall_clock=lambda: 1_800_000_000.0,
        poll_interval=60,
        subreddits=("GlobalOffensive", "cs2", "csgomarketforum"),
    )
    assert await service.sync_feeds(max_sources=1) == 1
    assert calls == ["GlobalOffensive"]
    assert await service.sync_feeds(max_sources=1) == 0  # same fixture id dedupes
    assert calls == ["GlobalOffensive", "cs2"]
    # Unbounded calls still sweep everything stale; the feed repeats the same
    # post id, so ingest is deduped to zero while the source is still fetched.
    assert await service.sync_feeds() == 0
    assert calls == ["GlobalOffensive", "cs2", "csgomarketforum"]


@pytest.mark.asyncio
async def test_all_sources_failed_keeps_failure_window_and_opens_breaker(
    tmp_path: Path,
) -> None:
    """A1 regression: all-failed rounds must not reset the circuit breaker."""

    from src.services.resilience import get_circuit_breaker

    async def fetcher(url: str) -> str:
        raise RuntimeError("reddit is down")

    clock = {"value": 1_800_000_000.0}
    database = StateDatabase(tmp_path / "state-breaker.db", allow_schema_migrations=True)
    await database.initialize()
    service = RedditNewsService(
        RedditPostRepository(database),
        fetcher=fetcher,
        wall_clock=lambda: clock["value"],
        poll_interval=60,
        subreddits=("cs2",),
    )
    breaker = get_circuit_breaker("reddit-rss")
    try:
        for _ in range(5):
            clock["value"] += 120.0  # step past the poll interval between rounds
            with pytest.raises(RedditNewsUnavailable, match="all reddit feeds failed"):
                await service.sync_feeds()
        snapshot = await breaker.snapshot()
        assert snapshot.state == "open"
    finally:
        # Reset the shared breaker so other tests start clean.
        await breaker.succeed()

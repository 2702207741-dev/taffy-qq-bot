"""Durable daily snapshot of the full CS2 market-name catalog."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote

from .config import bounded_env_int, env_bool, project_path_setting
from .http_client import request_text
from .market import st_get
from .market_catalog import CatalogEntry
from .resource_governor import resource_governor

logger = logging.getLogger("taffy.services.market_catalog_store")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "market_catalog.db"
CATALOG_PATH = "/open/cs2/v1/base"
CATALOG_REFRESH_SECONDS = 24 * 60 * 60 + 5 * 60
CATALOG_QUOTA_RETRY_SECONDS = 8 * 60 * 60
CATALOG_FAILURE_RETRY_SECONDS = 30 * 60
MIN_FULL_CATALOG_ENTRIES = bounded_env_int("TAFFY_MARKET_CATALOG_MIN_ENTRIES", 1_000, 100, 100_000)
MAX_CATALOG_DROP_RATIO = 0.10

SITEMAP_URL = "https://www.steamdt.com/sitemap.xml"
SITEMAP_REFRESH_SECONDS = 7 * 24 * 60 * 60
SITEMAP_FAILURE_RETRY_SECONDS = 6 * 60 * 60
SITEMAP_SYNC_ENABLED = env_bool("TAFFY_SITEMAP_SYNC_ENABLED", True)
SITEMAP_MIN_ENTRIES = bounded_env_int("TAFFY_SITEMAP_MIN_ENTRIES", 5_000, 100, 100_000)
SITEMAP_MIN_DROP_RATIO = 0.60
SITEMAP_MAX_ITEMS = 60_000
SITEMAP_ITEM_PREFIX = "/cs2/"
# Feature pages that share the /cs2/ URL prefix without being items.
_SITEMAP_NON_ITEM_PAGES = frozenset({"tracker", "null"})
_SITEMAP_FETCH_TIMEOUT = 60.0
# The sitemap is ~3.4 MB, above the shared client's 2 MB default cap.
_SITEMAP_MAX_BYTES = 8_000_000

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS catalog (
    market_name TEXT PRIMARY KEY NOT NULL,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL,
    catalog_source TEXT NOT NULL CHECK (catalog_source IN ('seed', 'open_api', 'live_search', 'sitemap')),
    catalog_updated_at INTEGER NOT NULL CHECK (catalog_updated_at > 0),
    last_requested_at INTEGER NOT NULL DEFAULT 0 CHECK (last_requested_at >= 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS collection_state (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL CHECK (updated_at > 0)
) WITHOUT ROWID;
"""

_T = TypeVar("_T")
MarketGet = Callable[[str, dict[str, Any] | None, float | None], Awaitable[dict[str, Any]]]
SitemapFetch = Callable[[str], Awaitable[str]]


async def _default_sitemap_fetch(url: str) -> str:
    return await request_text(
        "GET",
        url,
        attempts=2,
        total_timeout=_SITEMAP_FETCH_TIMEOUT,
        max_bytes=_SITEMAP_MAX_BYTES,
    )


def parse_sitemap_names(xml_text: str) -> tuple[str, ...]:
    """Extract deduplicated CS2 market names from the SteamDT sitemap."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError("sitemap is not valid XML") from exc
    names: list[str] = []
    seen: set[str] = set()
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        location = element.text.strip() if tag == "loc" and element.text else ""
        if not location:
            continue
        marker = location.find(SITEMAP_ITEM_PREFIX)
        if marker < 0:
            continue
        encoded = location[marker + len(SITEMAP_ITEM_PREFIX) :].rstrip("/")
        if not encoded or "/" in encoded or "?" in encoded or "#" in encoded:
            continue
        name = unquote(encoded).strip()
        if (
            not name
            or len(name) > 240
            or name in seen
            or name.casefold() in _SITEMAP_NON_ITEM_PAGES
        ):
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= SITEMAP_MAX_ITEMS:
            break
    return tuple(names)


def _database_path() -> Path:
    return project_path_setting("TAFFY_MARKET_CATALOG_DB", DEFAULT_DATABASE_PATH)


def _parse_catalog(response: Mapping[str, Any], now: int) -> list[tuple[str, str, int]]:
    raw_items = response.get("data") if response.get("success") else None
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return []
    rows: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        market_name = str(raw.get("marketHashName", "")).strip()
        display_name = str(raw.get("name", "")).strip()
        if (
            not market_name
            or not display_name
            or market_name in seen
            or len(market_name) > 240
            or len(display_name) > 300
        ):
            continue
        seen.add(market_name)
        rows.append((market_name, display_name, now))
    return rows


class MarketCatalogStore:
    """Refresh one quota-limited full catalog while preserving the last success."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        market_get: MarketGet = st_get,
        wall_clock: Callable[[], float] = time.time,
        min_full_entries: int = MIN_FULL_CATALOG_ENTRIES,
        sitemap_fetch: SitemapFetch = _default_sitemap_fetch,
        sitemap_url: str | None = None,
        min_sitemap_entries: int = SITEMAP_MIN_ENTRIES,
    ) -> None:
        self.path = (path or _database_path()).resolve()
        self._market_get = market_get
        self._wall_clock = wall_clock
        self._min_full_entries = max(1, int(min_full_entries))
        self._sitemap_fetch = sitemap_fetch
        self._sitemap_url = (
            sitemap_url or os.environ.get("TAFFY_SITEMAP_URL", "").strip() or SITEMAP_URL
        )
        self._min_sitemap_entries = max(1, int(min_sitemap_entries))
        self._db_lock = asyncio.Lock()
        self._initialization_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._sitemap_lock = asyncio.Lock()
        self._initialized = False
        self._entries_cache: tuple[CatalogEntry, ...] | None = None
        # Sampling reads the whole catalog and hashes every name; cache the
        # result per limit and invalidate on any catalog write (see the
        # ``_sample_cache.clear()`` call sites).
        self._sample_cache: dict[int, tuple[CatalogEntry, ...]] = {}
        self._refresh_task: asyncio.Task[int] | None = None
        self._sitemap_sync_task: asyncio.Task[int] | None = None

    def _connect(self) -> sqlite3.Connection:
        if self.path.exists() and self.path.is_symlink():
            raise OSError("refusing to open a symlinked market catalog")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def _database_call(self, callback: Callable[[sqlite3.Connection], _T]) -> _T:
        def run() -> _T:
            connection = self._connect()
            try:
                result = callback(connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._db_lock:
            return await asyncio.to_thread(run)

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return

            def create(connection: sqlite3.Connection) -> None:
                connection.executescript(_SCHEMA)
                schema_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'catalog'"
                ).fetchone()
                schema_sql = str(schema_row[0]) if schema_row is not None else ""
                if "live_search" not in schema_sql:
                    connection.executescript(
                        """
                        ALTER TABLE catalog RENAME TO catalog_v1;
                        CREATE TABLE catalog (
                            market_name TEXT PRIMARY KEY NOT NULL,
                            display_name TEXT NOT NULL,
                            category TEXT NOT NULL,
                            catalog_source TEXT NOT NULL
                                CHECK (catalog_source IN ('seed', 'open_api', 'live_search')),
                            catalog_updated_at INTEGER NOT NULL CHECK (catalog_updated_at > 0),
                            last_requested_at INTEGER NOT NULL DEFAULT 0
                                CHECK (last_requested_at >= 0)
                        ) WITHOUT ROWID;
                        INSERT INTO catalog SELECT * FROM catalog_v1;
                        DROP TABLE catalog_v1;
                        """
                    )
                # v3 adds the 'sitemap' source injected from the SteamDT sitemap.
                schema_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'catalog'"
                ).fetchone()
                schema_sql = str(schema_row[0]) if schema_row is not None else ""
                if "sitemap" not in schema_sql:
                    connection.executescript(
                        """
                        ALTER TABLE catalog RENAME TO catalog_v2;
                        CREATE TABLE catalog (
                            market_name TEXT PRIMARY KEY NOT NULL,
                            display_name TEXT NOT NULL,
                            category TEXT NOT NULL,
                            catalog_source TEXT NOT NULL
                                CHECK (catalog_source IN ('seed', 'open_api', 'live_search', 'sitemap')),
                            catalog_updated_at INTEGER NOT NULL CHECK (catalog_updated_at > 0),
                            last_requested_at INTEGER NOT NULL DEFAULT 0
                                CHECK (last_requested_at >= 0)
                        ) WITHOUT ROWID;
                        INSERT INTO catalog SELECT * FROM catalog_v2;
                        DROP TABLE catalog_v2;
                        """
                    )
                # The retired report collector stored price snapshots in this
                # database. They are not used for name resolution.
                connection.execute("DROP TABLE IF EXISTS quote_snapshots")

            await self._database_call(create)
            self._initialized = True

    async def _state_value(self, key: str) -> str | None:
        await self.initialize()

        def read(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(
                "SELECT value FROM collection_state WHERE key = ?", (key,)
            ).fetchone()
            return str(row[0]) if row is not None else None

        return await self._database_call(read)

    async def _set_state_value(self, key: str, value: str, now: int) -> None:
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO collection_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

        await self._database_call(write)

    async def refresh_if_due(self) -> int:
        await self.initialize()
        if resource_governor.game_running:
            return 0
        async with self._refresh_lock:
            if resource_governor.game_running:
                return 0
            now = int(self._wall_clock())
            raw_next_attempt = await self._state_value("catalog_next_attempt_at")
            try:
                next_attempt = int(raw_next_attempt or 0)
            except ValueError:
                next_attempt = 0
            if next_attempt > now:
                return 0

            await self._set_state_value(
                "catalog_next_attempt_at",
                str(now + CATALOG_FAILURE_RETRY_SECONDS),
                now,
            )
            try:
                response = await self._market_get(CATALOG_PATH, None, 60 * 60)
            except Exception as exc:
                logger.warning("Full market catalog refresh failed error=%s", type(exc).__name__)
                return 0
            rows = _parse_catalog(response, now)
            current_full_count = await self.full_count()
            minimum_acceptable = max(
                self._min_full_entries,
                int(current_full_count * (1.0 - MAX_CATALOG_DROP_RATIO) + 0.9999),
            )
            if len(rows) < minimum_acceptable:
                retry = (
                    CATALOG_QUOTA_RETRY_SECONDS
                    if str(response.get("errorCode", "")) == "4005"
                    else CATALOG_FAILURE_RETRY_SECONDS
                )
                await self._set_state_value("catalog_next_attempt_at", str(now + retry), now)
                await self._set_state_value(
                    "catalog_last_error",
                    f"integrity_gate:{len(rows)}<{minimum_acceptable}",
                    now,
                )
                logger.warning(
                    "Full catalog rejected by integrity gate entries=%d required=%d",
                    len(rows),
                    minimum_acceptable,
                )
                return 0

            def store(connection: sqlite3.Connection) -> None:
                connection.execute("DELETE FROM catalog WHERE catalog_source = 'open_api'")
                connection.executemany(
                    """
                    INSERT INTO catalog (
                        market_name, display_name, category, catalog_source,
                        catalog_updated_at, last_requested_at
                    ) VALUES (?, ?, '', 'open_api', ?, 0)
                    ON CONFLICT(market_name) DO UPDATE SET
                        display_name = excluded.display_name,
                        catalog_source = 'open_api',
                        catalog_updated_at = excluded.catalog_updated_at
                    """,
                    rows,
                )

            await self._database_call(store)
            self._entries_cache = None
            self._sample_cache.clear()
            await self._set_state_value("catalog_last_success_at", str(now), now)
            await self._set_state_value("catalog_last_success_count", str(len(rows)), now)
            await self._set_state_value("catalog_last_error", "", now)
            await self._set_state_value(
                "catalog_next_attempt_at",
                str(now + CATALOG_REFRESH_SECONDS),
                now,
            )
            return len(rows)

    async def full_count(self) -> int:
        await self.initialize()

        def read(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT COUNT(*) FROM catalog WHERE catalog_source = 'open_api'"
            ).fetchone()
            return int(row[0]) if row is not None else 0

        return await self._database_call(read)

    async def sitemap_count(self) -> int:
        await self.initialize()

        def read(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT COUNT(*) FROM catalog WHERE catalog_source = 'sitemap'"
            ).fetchone()
            return int(row[0]) if row is not None else 0

        return await self._database_call(read)

    async def sync_sitemap_if_due(self) -> int:
        """Replace the sitemap-sourced names while preserving richer sources."""

        if not SITEMAP_SYNC_ENABLED:
            return 0
        await self.initialize()
        if resource_governor.game_running:
            return 0
        async with self._sitemap_lock:
            if resource_governor.game_running:
                return 0
            now = int(self._wall_clock())
            raw_next_attempt = await self._state_value("sitemap_next_attempt_at")
            try:
                next_attempt = int(raw_next_attempt or 0)
            except ValueError:
                next_attempt = 0
            if next_attempt > now:
                return 0

            await self._set_state_value(
                "sitemap_next_attempt_at",
                str(now + SITEMAP_FAILURE_RETRY_SECONDS),
                now,
            )
            try:
                xml_text = await self._sitemap_fetch(self._sitemap_url)
                names = parse_sitemap_names(xml_text)
            except Exception as exc:
                logger.warning("Sitemap catalog sync failed error=%s", type(exc).__name__)
                await self._set_state_value("sitemap_last_error", type(exc).__name__, now)
                return 0

            current_sitemap_count = await self.sitemap_count()
            minimum_acceptable = max(
                self._min_sitemap_entries,
                int(current_sitemap_count * SITEMAP_MIN_DROP_RATIO + 0.9999),
            )
            if len(names) < minimum_acceptable:
                await self._set_state_value(
                    "sitemap_last_error",
                    f"integrity_gate:{len(names)}<{minimum_acceptable}",
                    now,
                )
                logger.warning(
                    "Sitemap rejected by integrity gate entries=%d required=%d",
                    len(names),
                    minimum_acceptable,
                )
                return 0

            def store(connection: sqlite3.Connection) -> None:
                connection.execute("DELETE FROM catalog WHERE catalog_source = 'sitemap'")
                connection.executemany(
                    """
                    INSERT INTO catalog (
                        market_name, display_name, category, catalog_source,
                        catalog_updated_at, last_requested_at
                    ) VALUES (?, ?, '', 'sitemap', ?, 0)
                    ON CONFLICT(market_name) DO NOTHING
                    """,
                    [(name, name, now) for name in names],
                )

            await self._database_call(store)
            self._entries_cache = None
            self._sample_cache.clear()
            await self._set_state_value("sitemap_last_success_at", str(now), now)
            await self._set_state_value("sitemap_last_success_count", str(len(names)), now)
            await self._set_state_value("sitemap_last_error", "", now)
            await self._set_state_value(
                "sitemap_next_attempt_at",
                str(now + SITEMAP_REFRESH_SECONDS),
                now,
            )
            return len(names)

    async def sample_entries(self, limit: int) -> tuple[CatalogEntry, ...]:
        """Return a stable bounded sample, prioritising requested items.

        Name resolution still uses the full catalog. Historical collection
        deliberately uses a smaller denominator so the free batch quota can
        produce comparable observations inside one hour.  Results are cached
        per limit until the next catalog write.
        """

        await self.initialize()
        bounded_limit = max(1, min(int(limit), 5_000))
        cached = self._sample_cache.get(bounded_limit)
        if cached is not None:
            return cached

        def read(connection: sqlite3.Connection) -> tuple[CatalogEntry, ...]:
            rows = connection.execute(
                """
                SELECT market_name, display_name, last_requested_at
                FROM catalog
                WHERE catalog_source IN ('open_api', 'live_search', 'sitemap')
                """
            ).fetchall()

            def ordering(row: sqlite3.Row) -> tuple[int, int, bytes]:
                requested_at = int(row[2])
                digest = hashlib.sha256(str(row[0]).encode("utf-8")).digest()
                return (0 if requested_at > 0 else 1, -requested_at, digest)

            ordered = sorted(rows, key=ordering)[:bounded_limit]
            return tuple(CatalogEntry(str(row[0]), str(row[1])) for row in ordered)

        sampled = await self._database_call(read)
        self._sample_cache[bounded_limit] = sampled
        return sampled

    async def mark_requested(self, market_name: str) -> bool:
        """Prioritise a catalog item after a real user lookup."""

        clean_name = market_name.strip()
        if not clean_name or len(clean_name) > 240:
            return False
        await self.initialize()
        now = int(self._wall_clock())

        def write(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "UPDATE catalog SET last_requested_at = ? WHERE market_name = ?",
                (now, clean_name),
            )
            return cursor.rowcount > 0

        updated = await self._database_call(write)
        if updated:
            # Requested items jump to the front of the sample ordering.
            self._sample_cache.clear()
        return updated

    async def persist_search_entries(self, entries: Sequence[CatalogEntry]) -> int:
        """Durably add valid on-demand results without pretending they are a full catalog."""

        await self.initialize()
        now = int(self._wall_clock())
        rows = [
            (entry.market_name.strip(), entry.display_name.strip(), now, now)
            for entry in entries
            if entry.market_name.strip()
            and entry.display_name.strip()
            and len(entry.market_name.strip()) <= 240
            and len(entry.display_name.strip()) <= 300
        ]
        if not rows:
            return 0

        def store(connection: sqlite3.Connection) -> None:
            connection.executemany(
                """
                INSERT INTO catalog (
                    market_name, display_name, category, catalog_source,
                    catalog_updated_at, last_requested_at
                ) VALUES (?, ?, '', 'live_search', ?, ?)
                ON CONFLICT(market_name) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_requested_at = excluded.last_requested_at,
                    catalog_source = CASE
                        WHEN catalog.catalog_source = 'open_api' THEN 'open_api'
                        ELSE 'live_search'
                    END
                """,
                rows,
            )

        await self._database_call(store)
        self._entries_cache = None
        self._sample_cache.clear()
        return len(rows)

    def schedule_refresh_if_due(self) -> None:
        """Start a due refresh in the background; never block name resolution."""

        task = self._refresh_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self.refresh_if_due(), name="market-catalog-refresh")
        self._refresh_task = task

        def completed(finished: asyncio.Task[int]) -> None:
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Background catalog refresh failed error=%s", type(exc).__name__)

        task.add_done_callback(completed)

    def schedule_sitemap_sync_if_due(self) -> None:
        """Start a due sitemap sync in the background; never block callers."""

        if not SITEMAP_SYNC_ENABLED:
            return
        task = self._sitemap_sync_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self.sync_sitemap_if_due(), name="market-catalog-sitemap-sync")
        self._sitemap_sync_task = task

        def completed(finished: asyncio.Task[int]) -> None:
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Background sitemap sync failed error=%s", type(exc).__name__)

        task.add_done_callback(completed)

    async def close(self) -> None:
        """Cancel and reap background tasks during application shutdown."""

        refresh_task, self._refresh_task = self._refresh_task, None
        sitemap_task, self._sitemap_sync_task = self._sitemap_sync_task, None
        pending = [task for task in (refresh_task, sitemap_task) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def entries(self) -> tuple[CatalogEntry, ...]:
        """Return local entries immediately and refresh a stale catalog in background."""

        await self.initialize()
        if self._entries_cache is not None:
            self.schedule_refresh_if_due()
            return self._entries_cache

        def read(connection: sqlite3.Connection) -> tuple[CatalogEntry, ...]:
            return tuple(
                CatalogEntry(str(row[0]), str(row[1]))
                for row in connection.execute(
                    """
                    SELECT market_name, display_name FROM catalog
                    WHERE catalog_source IN ('open_api', 'live_search', 'sitemap')
                    ORDER BY market_name
                    """
                ).fetchall()
            )

        self._entries_cache = await self._database_call(read)
        self.schedule_refresh_if_due()
        return self._entries_cache

    async def status(self) -> dict[str, int | str | None]:
        await self.initialize()

        def read(connection: sqlite3.Connection) -> dict[str, int | str | None]:
            counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT catalog_source, COUNT(*) FROM catalog GROUP BY catalog_source"
                ).fetchall()
            }
            state = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key, value FROM collection_state").fetchall()
            }
            return {
                "full_count": counts.get("open_api", 0),
                "sitemap_count": counts.get("sitemap", 0),
                "live_count": counts.get("live_search", 0),
                "last_success_at": int(state["catalog_last_success_at"])
                if state.get("catalog_last_success_at", "").isdigit()
                else None,
                "next_attempt_at": int(state["catalog_next_attempt_at"])
                if state.get("catalog_next_attempt_at", "").isdigit()
                else None,
                "last_error": state.get("catalog_last_error") or None,
            }

        return await self._database_call(read)


market_catalog_store = MarketCatalogStore()


async def get_market_catalog_entries() -> tuple[CatalogEntry, ...]:
    return await market_catalog_store.entries()


async def close_market_catalog_store() -> None:
    await market_catalog_store.close()


__all__ = [
    "CATALOG_QUOTA_RETRY_SECONDS",
    "MIN_FULL_CATALOG_ENTRIES",
    "SITEMAP_REFRESH_SECONDS",
    "SITEMAP_SYNC_ENABLED",
    "SITEMAP_URL",
    "MarketCatalogStore",
    "close_market_catalog_store",
    "get_market_catalog_entries",
    "market_catalog_store",
    "parse_sitemap_names",
]

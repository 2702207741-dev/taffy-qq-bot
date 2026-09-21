"""CS2 case metadata cache: ByMykel/CSGO-API pools + Valve official odds.

Sources (both free, both mirrored through jsDelivr for mainland reachability):

* ``ByMykel/CSGO-API`` (en + zh-CN ``crates.json``) enumerates every crate,
  its Chinese/English names, the tradable ``market_hash_name`` and the item
  pool (``contains`` per rarity tier plus the ``contains_rare`` special
  pool).  The zh-CN file carries the tradable English hash names, the en
  file carries the English item names used for pricing.
* ``jonese1234/Csgo-Case-Data`` is fetched as a cross-check that the pool
  enumeration is sane; the drop probabilities themselves are Valve's
  officially published per-tier odds (Mil-Spec 79.92% / Restricted 15.98% /
  Classified 3.2% / Covert 0.64% / Rare Special 0.26%), which that project
  also adopts.  Within a tier the contained skins are treated as equally
  likely; within one skin the available wear variants split its odds
  evenly.

Everything lands in a dedicated SQLite cache (``data/csgo_meta.db``) with
its fetch timestamp so replies can honestly state their data age.  Sync is
lazy (first query after the TTL expires), single-flight and fail-closed: a
failed sync falls back to the previous snapshot, and with no snapshot at
all the EV command refuses instead of guessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from .config import bounded_base_url, bounded_env_int, bounded_env_text, env_bool, runtime_setting

logger = logging.getLogger("taffy.services.case_meta")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_META_BASE = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api"
_DEFAULT_CASEDATA_URL = (
    "https://cdn.jsdelivr.net/gh/jonese1234/Csgo-Case-Data@master/CaseContents.json"
)
_TTL_HOURS = bounded_env_int("TAFFY_CASE_META_TTL_HOURS", 168, 6, 24 * 30)
#: Overall background-sync deadline; a full sync pulls ~17 MB across mirrors.
_SYNC_TIMEOUT = bounded_env_int("TAFFY_CASE_META_TIMEOUT_SECONDS", 900, 60, 1800)
_META_BASE_URL = bounded_base_url("TAFFY_CASE_META_BASE_URL", _DEFAULT_META_BASE)
_CASEDATA_URL = bounded_env_text("TAFFY_CASEDATA_URL", _DEFAULT_CASEDATA_URL, maximum=500)

#: Valve's officially published per-tier odds (2023 disclosure).
TIER_ODDS: dict[str, float] = {
    "milspec": 0.7992,
    "restricted": 0.1598,
    "classified": 0.0320,
    "covert": 0.0064,
}
SPECIAL_ODDS = 0.0026
TIER_LABELS: dict[str, str] = {
    "milspec": "军规",
    "restricted": "受限",
    "classified": "保密",
    "covert": "隐秘",
}
#: crates.json rarity ids/names → internal tiers (name match is the fallback).
_TIER_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("milspec", ("rarity_rare_weapon", "rarity_rare", "mil-spec")),
    ("restricted", ("rarity_mythical_weapon", "rarity_mythical", "restricted")),
    ("classified", ("rarity_legendary_weapon", "rarity_legendary", "classified")),
    ("covert", ("rarity_ancient_weapon", "rarity_ancient", "covert")),
)

_REQUEST_MAX_BYTES = 16_000_000
_T = TypeVar("_T")


def case_meta_db_path() -> Path:
    raw = runtime_setting("TAFFY_CSGO_META_DB")
    configured = raw.strip() if raw else ""
    path = Path(configured) if configured else PROJECT_ROOT / "data" / "csgo_meta.db"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def case_ev_enabled() -> bool:
    return env_bool("TAFFY_CASE_EV_ENABLED", True)


def _validate_public_url(url: str) -> bool:
    """Reject any non-http(s), loopback, private or reserved endpoint host."""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if host == "localhost":
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_reserved
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


def rarity_to_tier(rarity: object) -> str | None:
    """Map one crates.json rarity onto an odds tier; None when unknown."""

    if not isinstance(rarity, dict):
        return None
    identifier = str(rarity.get("id") or "")
    name = str(rarity.get("name") or "").casefold()
    for tier, markers in _TIER_MATCHERS:
        for marker in markers:
            if identifier == marker or (marker in name):
                return tier
    return None


@dataclass(frozen=True, slots=True)
class CrateInfo:
    crate_id: str
    name_en: str
    name_zh: str | None
    market_hash_name: str
    crate_type: str | None
    first_sale_date: str | None
    rare_count: int
    unknown_count: int
    fetched_at: int


@dataclass(frozen=True, slots=True)
class CrateItem:
    item_name: str
    item_name_zh: str | None
    tier: str


def _parse_crates(en_text: str, zh_text: str | None) -> dict[str, dict[str, object]]:
    """Merge the en pool enumeration with the zh names by crate id."""

    en = json.loads(en_text)
    if not isinstance(en, list):
        raise ValueError("crates.json root is not a list")
    zh_index: dict[str, dict[str, object]] = {}
    if zh_text:
        zh = json.loads(zh_text)
        if isinstance(zh, list):
            zh_index = {
                str(entry.get("id")): entry
                for entry in zh
                if isinstance(entry, dict) and entry.get("id")
            }
    parsed: dict[str, dict[str, object]] = {}
    for entry in en:
        if not isinstance(entry, dict) or entry.get("type") != "Case":
            continue
        crate_id = str(entry.get("id") or "").strip()
        name_en = str(entry.get("name") or "").strip()
        if not crate_id or not name_en:
            continue
        zh_entry = zh_index.get(crate_id)
        parsed[crate_id] = {
            "crate_id": crate_id,
            "name_en": name_en,
            "name_zh": (
                str(zh_entry.get("name")).strip() or None
                if isinstance(zh_entry, dict) and zh_entry.get("name")
                else None
            ),
            "market_hash_name": (
                str(zh_entry.get("market_hash_name")).strip()
                if isinstance(zh_entry, dict) and zh_entry.get("market_hash_name")
                else name_en
            ),
            "first_sale_date": (
                str(entry.get("first_sale_date")).strip() if entry.get("first_sale_date") else None
            ),
            "contains": entry.get("contains") or [],
            "contains_rare": entry.get("contains_rare") or [],
        }
    return parsed


async def _fetch_file(url: str) -> str:
    """Streaming GET of one large metadata file with a hard size cap.

    The shared client is used directly because the crates.json payloads
    (~8.5 MB) exceed what the bounded ``request_text`` convenience wrapper
    is designed for, and its 120 s deadline ceiling is too tight for slow
    mainland links.
    """

    from .http_client import lease_http_client

    async with lease_http_client() as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            buffer = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                buffer.extend(chunk)
                if len(buffer) > _REQUEST_MAX_BYTES:
                    raise ValueError("upstream response is too large")
            if not buffer:
                raise ValueError("upstream text response is empty")
            return buffer.decode("utf-8")


def _mirror_candidates(url: str) -> list[str]:
    """Primary URL first, then the fastly mirror when the host is jsDelivr."""

    mirror = "https://fastly.jsdelivr.net"
    primary_host = "https://cdn.jsdelivr.net"
    candidates = [url]
    if url.startswith(primary_host):
        candidates.append(mirror + url[len(primary_host) :])
    return candidates


async def _fetch_with_mirror(url: str) -> str:
    """Try the primary URL, then its mirror; raise only after both fail."""

    last_error: Exception | None = None
    for candidate in _mirror_candidates(url):
        if not _validate_public_url(candidate):
            logger.error("Metadata URL rejected by host validation: %s", candidate)
            continue
        try:
            return await _fetch_file(candidate)
        except Exception as exc:
            last_error = exc
            logger.warning("Metadata fetch failed url=%s error=%s", candidate, type(exc).__name__)
    assert last_error is not None
    raise last_error


def _parse_pool_items(
    crate: dict[str, object],
) -> tuple[tuple[CrateItem, ...], int, int]:
    """Project one crate's pool into typed items, rare-pool size, unknown count."""

    items: list[CrateItem] = []
    unknown = 0
    contains = crate.get("contains")
    contains_list = contains if isinstance(contains, list) else []
    for raw in contains_list:
        if not isinstance(raw, dict):
            unknown += 1
            continue
        name = str(raw.get("name") or "").strip()
        tier = rarity_to_tier(raw.get("rarity"))
        if not name or tier is None:
            unknown += 1
            continue
        items.append(CrateItem(name, None, tier))
    rare_raw = crate.get("contains_rare")
    rare_count = len(rare_raw) if isinstance(rare_raw, list) else 0
    return tuple(items), rare_count, unknown


class CaseMetaStore:
    """Dedicated SQLite cache for crate metadata with lazy TTL sync."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or case_meta_db_path()
        self._lock = asyncio.Lock()
        self._syncing = False
        self._sync_task: "asyncio.Task[None] | None" = None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    async def _database_call(self, callback: Callable[[sqlite3.Connection], _T]) -> _T:
        def run() -> _T:
            connection = self._connect()
            try:
                return callback(connection)
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    async def initialize(self) -> None:
        def create(connection: sqlite3.Connection) -> None:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS crates (
                    crate_id TEXT PRIMARY KEY,
                    name_en TEXT NOT NULL,
                    name_zh TEXT,
                    market_hash_name TEXT NOT NULL,
                    first_sale_date TEXT,
                    rare_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    fetched_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS crate_items (
                    crate_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    item_name_zh TEXT,
                    tier TEXT NOT NULL,
                    UNIQUE (crate_id, item_name)
                );
                CREATE INDEX IF NOT EXISTS idx_crate_items_crate
                    ON crate_items (crate_id);
                CREATE TABLE IF NOT EXISTS sync_state (
                    source TEXT PRIMARY KEY,
                    fetched_at INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ev_cache (
                    market_hash_name TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    computed_at INTEGER NOT NULL
                );
                """
            )
            connection.commit()

        await self._database_call(create)

    async def last_sync(self) -> int | None:
        def read(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                "SELECT MAX(fetched_at) AS latest FROM sync_state WHERE status = 'ok'"
            ).fetchone()
            value = int(row["latest"]) if row and row["latest"] is not None else None
            return value

        try:
            return await self._database_call(read)
        except Exception as exc:
            logger.warning("Case meta sync-state read failed error=%s", type(exc).__name__)
            return None

    async def sync_if_due(
        self, *, now: float | None = None, force: bool = False, background: bool = False
    ) -> bool:
        """Ensure usable metadata; ``background`` never blocks the caller.

        Blocking mode (tests, operators) downloads inline.  Background mode —
        used by the QQ command — spawns a single-flight task and returns
        immediately: ``True`` means a usable snapshot already exists (fresh
        or stale), ``False`` means the caller should honestly report that a
        first sync is still running.
        """

        if not case_ev_enabled():
            return False
        if not _META_BASE_URL or not _CASEDATA_URL:
            return False
        if not _validate_public_url(_META_BASE_URL) or not _validate_public_url(_CASEDATA_URL):
            logger.error("Case metadata endpoint rejected by host validation")
            return False
        current = int(time.time() if now is None else now)
        async with self._lock:
            if self._syncing:
                return await self._has_any_snapshot()
            last = await self.last_sync()
            if not force and last is not None and current - last < _TTL_HOURS * 3600:
                return True
            if last is None and await self._has_any_snapshot():
                # Partially synced earlier (crates exist, marker missing):
                # treat the snapshot as usable instead of re-downloading.
                return True
            self._syncing = True
            if background:
                task = asyncio.create_task(self._run_sync(current))
                self._sync_task = task
                task.add_done_callback(self._sync_done)
                usable = await self._has_any_snapshot()
                return usable
            try:
                await self._run_sync(current)
                return True
            except Exception as exc:
                logger.warning("Case meta sync failed error=%s", type(exc).__name__)
                return await self._has_any_snapshot()
            finally:
                self._syncing = False

    def _sync_done(self, task: "asyncio.Task[None]") -> None:
        """Clear the single-flight flag once a background sync settles."""

        self._syncing = False
        self._sync_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "Background case meta sync failed error=%s",
                type(task.exception()).__name__,
            )

    async def _run_sync(self, current: int) -> None:
        """Single-flight sync body; exceptions propagate to the caller."""

        try:
            await self._fetch_and_store(current)
        finally:
            self._syncing = False

    async def _has_any_snapshot(self) -> bool:
        def read(connection: sqlite3.Connection) -> bool:
            row = connection.execute("SELECT 1 AS present FROM crates LIMIT 1").fetchone()
            return row is not None

        try:
            return await self._database_call(read)
        except Exception:
            return False

    async def _fetch_and_store(self, current: int) -> None:
        # Sequential downloads with one overall deadline: the two crates.json
        # files are ~8.5 MB each and jsDelivr throttles parallel large files
        # hard enough that concurrent fetches can each outlive the window on
        # mainland links.  Optional files (zh labels, cross-check) never kill
        # the sync — English pool data alone stays usable.
        async with asyncio.timeout(_SYNC_TIMEOUT):
            en_text = await _fetch_with_mirror(f"{_META_BASE_URL}/en/crates.json")
            try:
                zh_text: str | None = await _fetch_with_mirror(
                    f"{_META_BASE_URL}/zh-CN/crates.json"
                )
            except Exception as exc:
                logger.warning("zh-CN crates fetch failed error=%s", type(exc).__name__)
                zh_text = None  # English names keep the sync usable without zh labels
            try:
                casetext = await _fetch_with_mirror(_CASEDATA_URL)
            except Exception as exc:
                logger.warning("CaseContents cross-check fetch failed error=%s", type(exc).__name__)
                casetext = json.dumps({"Cases": {}})  # cross-check reports "skipped"
        parsed = _parse_crates(en_text, zh_text)
        crosscheck = _crosscheck_pools(parsed, casetext)
        if not parsed:
            raise ValueError("no Case-type crates parsed from upstream")

        def store(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM crates")
            connection.execute("DELETE FROM crate_items")
            for crate in parsed.values():
                items, rare_count, unknown_count = _parse_pool_items(crate)
                connection.execute(
                    """
                    INSERT INTO crates (
                        crate_id, name_en, name_zh, market_hash_name,
                        first_sale_date, rare_count, unknown_count, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(crate["crate_id"]),
                        str(crate["name_en"]),
                        crate["name_zh"],
                        str(crate["market_hash_name"]),
                        crate["first_sale_date"],
                        rare_count,
                        unknown_count,
                        current,
                    ),
                )
                for item in items:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO crate_items (crate_id, item_name, item_name_zh, tier)
                        VALUES (?, ?, ?, ?)
                        """,
                        (str(crate["crate_id"]), item.item_name, item.item_name_zh, item.tier),
                    )
            connection.execute(
                "INSERT OR REPLACE INTO sync_state (source, fetched_at, status) VALUES (?, ?, 'ok')",
                ("bymykel_crates", current),
            )
            connection.execute(
                "INSERT OR REPLACE INTO sync_state (source, fetched_at, status) VALUES (?, ?, ?)",
                ("casedata_crosscheck", current, crosscheck),
            )
            connection.commit()

        await self._database_call(store)
        logger.info("Case metadata synced crates=%d crosscheck=%s", len(parsed), crosscheck)

    async def find_crate(self, query: str) -> CrateInfo | None:
        """Resolve one case by exact or unique-substring zh/en name."""

        clean = query.strip().casefold()
        if not clean:
            return None

        def like_pattern(text: str) -> str:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return f"%{escaped}%"

        def read(connection: sqlite3.Connection) -> CrateInfo | None:
            rows = connection.execute(
                "SELECT * FROM crates WHERE lower(name_en) = ? OR lower(market_hash_name) = ? "
                "OR name_zh = ?",
                (clean, clean, query.strip()),
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    "SELECT * FROM crates WHERE lower(name_en) LIKE ? ESCAPE '\\' "
                    "OR lower(market_hash_name) LIKE ? ESCAPE '\\' "
                    "OR name_zh LIKE ? ESCAPE '\\' ORDER BY fetched_at DESC",
                    (
                        like_pattern(clean),
                        like_pattern(clean),
                        like_pattern(query.strip()),
                    ),
                ).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                return None
            row = rows[0]
            return CrateInfo(
                crate_id=str(row["crate_id"]),
                name_en=str(row["name_en"]),
                name_zh=row["name_zh"],
                market_hash_name=str(row["market_hash_name"]),
                crate_type="Case",
                first_sale_date=row["first_sale_date"],
                rare_count=int(row["rare_count"]),
                unknown_count=int(row["unknown_count"]),
                fetched_at=int(row["fetched_at"]),
            )

        try:
            return await self._database_call(read)
        except Exception as exc:
            logger.warning("Case lookup failed error=%s", type(exc).__name__)
            return None

    async def crate_items(self, crate_id: str) -> tuple[CrateItem, ...]:
        def read(connection: sqlite3.Connection) -> tuple[CrateItem, ...]:
            rows = connection.execute(
                "SELECT item_name, item_name_zh, tier FROM crate_items WHERE crate_id = ?",
                (crate_id,),
            ).fetchall()
            return tuple(
                CrateItem(str(row["item_name"]), row["item_name_zh"], str(row["tier"]))
                for row in rows
            )

        try:
            return await self._database_call(read)
        except Exception as exc:
            logger.warning("Case pool read failed error=%s", type(exc).__name__)
            return ()

    async def cached_ev(self, market_hash_name: str, *, ttl_seconds: int) -> dict | None:
        def read(connection: sqlite3.Connection) -> dict | None:
            row = connection.execute(
                "SELECT payload_json, computed_at FROM ev_cache WHERE market_hash_name = ?",
                (market_hash_name,),
            ).fetchone()
            if row is None:
                return None
            if int(time.time()) - int(row["computed_at"]) > ttl_seconds:
                return None
            try:
                return json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                return None

        try:
            return await self._database_call(read)
        except Exception:
            return None

    async def store_ev(self, market_hash_name: str, payload: dict) -> None:
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO ev_cache (market_hash_name, payload_json, computed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(market_hash_name) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    computed_at = excluded.computed_at
                """,
                (
                    market_hash_name,
                    json.dumps(payload, ensure_ascii=False)[:60_000],
                    int(time.time()),
                ),
            )
            # Bounded cache: keep the freshest 64 computations.
            connection.execute(
                """
                DELETE FROM ev_cache WHERE market_hash_name IN (
                    SELECT market_hash_name FROM ev_cache
                    ORDER BY computed_at DESC LIMIT -1 OFFSET 64
                )
                """
            )
            connection.commit()

        try:
            await self._database_call(write)
        except Exception as exc:
            logger.warning("EV cache store failed error=%s", type(exc).__name__)


def _crosscheck_pools(parsed: dict[str, dict[str, object]], casetext: str) -> str:
    """Compare pool sizes against jonese1234's aggregate; report, never fail.

    Audit note (2026-08-30, window-B): the live database reports 32/35
    consistent.  The three differences are all upstream data-quality
    artefacts, not local bugs:

    * ``Chroma Case`` — jonese1234 stores ``AWP | Man-o&#39-war`` with an
      HTML-entity apostrophe while ByMykel uses the plain ``AWP |
      Man-o'war``; same item, different escaping.
    * ``Glove Case`` — jonese1234 additionally lists ``MP9 | Sonar`` (an
      item that only entered the game later; ByMykel's pool is current).
    * ``Spectrum 2 Case`` — jonese1234 aggregates the knife pool under the
      single entry ``Spectrum 2 Case Knives`` instead of enumerating each
      knife; ByMykel lists the knives individually.

    The cross-check deliberately reports rather than fails: ByMykel stays
    the enumeration source of truth and jonese1234 remains an aggregate
    sanity check.
    """

    try:
        data = json.loads(casetext)
        cases = data.get("Cases") if isinstance(data, dict) else None
        if not isinstance(cases, dict):
            return "skipped"
        matched = 0
        consistent = 0
        for crate in parsed.values():
            reference = cases.get(str(crate["name_en"]))
            if not isinstance(reference, dict):
                continue
            matched += 1
            reference_items = {str(name) for name in reference.get("Items") or []}
            ours, _rare, _unknown = _parse_pool_items(crate)
            if reference_items == {item.item_name for item in ours}:
                consistent += 1
        return f"{consistent}/{matched} consistent"
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "skipped"


case_meta_store = CaseMetaStore()


__all__ = [
    "SPECIAL_ODDS",
    "TIER_LABELS",
    "TIER_ODDS",
    "CaseMetaStore",
    "CrateInfo",
    "CrateItem",
    "case_ev_enabled",
    "case_meta_store",
    "rarity_to_tier",
]

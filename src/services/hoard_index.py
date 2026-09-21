"""挂刀指数 (hoard ratio) adapter for SteamTradingSiteTracker dumps.

Source: ``SteamTradingSiteTracker-Data`` (EricZhu-42), distributed from
``api.iflow.work`` (Cloudflare R2 redirect, free, mainland reachable).
The ``priority_archive`` holds one JSONL dump every 12 hours (00:15/12:15
UTC+8); each line is one tracked item with per-platform price ratios against
Steam, plus the upstream ``weighted_ratio`` (a blended hoard-ratio proxy).
The adapter stores the validated, derived snapshot locally in
``data/hoard_index.db`` with its fetch time so every reply can state the
real data age — including the honest note that the upstream archive has
stopped producing new dumps (latest file sits at 2026-05-20 as of the V0.5
window-B integration).

Fail-closed contract (mirrors ``csqaq`` and ``case_meta``):

* one dedicated circuit breaker ``hoard-index`` and one dedicated rate
  gate (list API + one download, so the whole sync is ~2 requests);
* every upstream URL passes the public-host validation used by
  ``case_meta`` (no http, no loopback/private/reserved hosts);
* the sync is single-flight and lazy (TTL), runs in the background from
  QQ commands, never blocks a reply; a failed sync keeps the previous
  snapshot; with no snapshot at all the command replies "正在同步";
* failed responses are never cached, and a snapshot older than
  ``MAX_SNAPSHOT_AGE_SECONDS`` is reported as stale data instead of being
  passed off as current.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from .config import bounded_base_url, bounded_env_float, bounded_env_int, env_bool, runtime_setting
from .http_client import lease_http_client
from .resilience import CircuitOpenError, get_circuit_breaker
from .telemetry import telemetry

logger = logging.getLogger("taffy.services.hoard_index")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_LIST_BASE = "https://api.iflow.work/export"
_DEFAULT_DOWNLOAD_BASE = "https://api.iflow.work/export"
_TTL_HOURS = bounded_env_int("TAFFY_HOARD_INDEX_TTL_HOURS", 12, 3, 24 * 30)
_MAX_FILE_AGE_SECONDS = bounded_env_int("TAFFY_HOARD_INDEX_MAX_AGE_SECONDS", 120, 6, 24 * 60)
_SYNC_TIMEOUT = bounded_env_int("TAFFY_HOARD_INDEX_TIMEOUT_SECONDS", 120, 30, 600)
_MIN_SAMPLE_RATIO = bounded_env_float("TAFFY_HOARD_INDEX_MIN_SAMPLE_RATIO", 0.5, 0.05, 1.0)
#: ``bounded_base_url`` rejects query strings, so the endpoint hosts live in
#: two base settings and the query suffix is appended at request time.
_LIST_BASE = bounded_base_url("TAFFY_HOARD_INDEX_LIST_BASE", _DEFAULT_LIST_BASE)
_DOWNLOAD_BASE = bounded_base_url("TAFFY_HOARD_INDEX_DOWNLOAD_BASE", _DEFAULT_DOWNLOAD_BASE)
_LIST_QUERY = "list?dir_name=priority_archive"
_DOWNLOAD_QUERY = "download?dir_name=priority_archive"

#: Steam app ids that count as "CS2 items" for the market ratio (730 = CS2;
#: 570 = Dota 2, excluded).  A dump that contains no CS2 rows is unusable.
_CS2_APPID = 730
_REQUEST_MAX_BYTES = 32_000_000  # dumps are ~8 MB JSON inside a ~1.5 MB zip
_MAX_DOWNLOAD_BYTES = 64_000_000  # safety cap for the streamed zip
_REDIRECT_LOCATIONS = 4

_T = TypeVar("_T")


def hoard_index_enabled() -> bool:
    return env_bool("TAFFY_HOARD_INDEX_ENABLED", False)


def hoard_index_db_path() -> Path:
    raw = runtime_setting("TAFFY_HOARD_INDEX_DB")
    configured = raw.strip() if raw else ""
    path = Path(configured) if configured else PROJECT_ROOT / "data" / "hoard_index.db"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


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


def _opt_ratio(value: object) -> float | None:
    """One finite ratio in (0, 1]; 255 is the upstream "no data" sentinel."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number >= 255:
        return None
    if not 0 < number <= 1.0:
        return None
    return number


def _opt_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maximum:
        return None
    return text


@dataclass(frozen=True, slots=True)
class HoardSnapshot:
    """One derived, validated snapshot of the whole tracked set."""

    fetched_at: int
    dump_name: str
    dump_time: int | None
    item_count: int
    cs2_count: int
    median_ratio: float | None
    p25_ratio: float | None
    p75_ratio: float | None
    best_ratio: float | None

    @property
    def stale(self) -> bool:
        return int(time.time()) - self.fetched_at > _MAX_FILE_AGE_SECONDS * 3600


def _parse_dump(text: str) -> list[dict[str, Any]]:
    """Parse one JSONL dump into a list of item dicts; empty when malformed."""

    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            items.append(entry)
    return items


def _item_ratio(entry: dict[str, Any]) -> float | None:
    """The hoard-ratio proxy for one item: upstream weighted ratio, else BUFF.

    ``weighted_ratio`` is the blended cross-platform Steam ratio published
    by the tracker; BUFF is the largest CNY platform so its ratio is the
    fallback when the blended number is missing.
    """

    weighted = _opt_ratio(entry.get("weighted_ratio"))
    if weighted is not None:
        return weighted
    metrics = entry.get("metrics")
    if isinstance(metrics, dict):
        buff_median = _opt_ratio(metrics.get("buff_median_sale_ratio"))
        if buff_median is not None:
            return buff_median
    return None


def derive_snapshot(
    items: Sequence[dict[str, Any]],
    *,
    fetched_at: int | None = None,
    dump_name: str = "",
) -> HoardSnapshot:
    """Project one parsed dump onto the median/percentile snapshot.

    Only CS2 rows (appid 730) with a usable ratio count; the median and the
    p25/p75 percentiles use the nearest-rank method over the ascending
    sorted ratios.  ``None`` medians mean the sample fell below the honest
    reporting floor — callers then reply "积累中" instead of inventing a
    number.
    """

    current = int(time.time() if fetched_at is None else fetched_at)
    ratios = [
        ratio
        for entry in items
        if int(entry.get("appid") or 0) == _CS2_APPID and (ratio := _item_ratio(entry)) is not None
    ]
    cs2_count = sum(1 for entry in items if int(entry.get("appid") or 0) == _CS2_APPID)
    if not ratios:
        return HoardSnapshot(
            current, dump_name, None, len(items), cs2_count, None, None, None, None
        )
    ratios.sort()
    dump_time: int | None = None
    first = items[0] if items else {}
    raw_time = first.get("update_time")
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        dump_time = int(raw_time)
    return HoardSnapshot(
        fetched_at=current,
        dump_name=dump_name,
        dump_time=dump_time,
        item_count=len(items),
        cs2_count=cs2_count,
        median_ratio=ratios[len(ratios) // 2],
        p25_ratio=ratios[max(0, (len(ratios) - 1) // 4)],
        p75_ratio=ratios[min(len(ratios) - 1, (3 * len(ratios)) // 4)],
        best_ratio=ratios[0],
    )


def _percentile_text(ratio: float | None) -> str:
    return f"{ratio * 100:.1f}" if ratio is not None else "—"


def format_hoard_summary(snapshot: HoardSnapshot) -> str | None:
    """One honest 挂刀 line; ``None`` when the sample is unusable.

    The upstream archive stopped producing files after 2026-05-20; any
    snapshot is therefore labelled with its real dump age so the reader can
    see it is an archive point, not a live reading.  Past 90 days the line
    additionally states history-only reference — a stale hoard ratio must
    never read like today's market.
    """

    if snapshot.median_ratio is None:
        return None
    days = 0
    if snapshot.dump_time is not None:
        days = max(0, int((int(time.time()) - snapshot.dump_time) // 86400))
    label = "存档中位" if days > 0 else "今日中位"
    age = f"（存档 {days} 天前）" if days > 0 else ""
    tail = "，仅供历史参考" if days >= 90 else ""
    return (
        f"挂刀：{label} {snapshot.median_ratio * 100:.1f}%"
        f"｜分位 {_percentile_text(snapshot.p25_ratio)}~{_percentile_text(snapshot.p75_ratio)}{age}{tail}"
    )


class HoardIndexStore:
    """Dedicated SQLite cache for the derived hoard snapshot + item ratios."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or hoard_index_db_path()
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
                CREATE TABLE IF NOT EXISTS snapshots (
                    fetched_at INTEGER PRIMARY KEY,
                    dump_name TEXT NOT NULL,
                    dump_time INTEGER,
                    item_count INTEGER NOT NULL,
                    cs2_count INTEGER NOT NULL,
                    median_ratio REAL,
                    p25_ratio REAL,
                    p75_ratio REAL,
                    best_ratio REAL
                );
                CREATE TABLE IF NOT EXISTS item_ratios (
                    hash_name TEXT PRIMARY KEY,
                    ratio REAL NOT NULL CHECK (ratio > 0 AND ratio <= 1),
                    fetched_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_item_ratios_fetched
                    ON item_ratios (fetched_at);
                CREATE TABLE IF NOT EXISTS sync_state (
                    source TEXT PRIMARY KEY,
                    fetched_at INTEGER NOT NULL,
                    status TEXT NOT NULL
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
            logger.warning("Hoard index sync-state read failed error=%s", type(exc).__name__)
            return None

    async def _has_any_snapshot(self) -> bool:
        def read(connection: sqlite3.Connection) -> bool:
            row = connection.execute("SELECT 1 AS present FROM snapshots LIMIT 1").fetchone()
            return row is not None

        try:
            return await self._database_call(read)
        except Exception:
            return False

    async def sync_if_due(
        self, *, now: float | None = None, force: bool = False, background: bool = False
    ) -> bool:
        """Ensure a usable snapshot; ``background`` never blocks the caller.

        ``True`` means a usable snapshot already exists (fresh or stale);
        ``False`` means the caller should honestly report that a first sync
        is still running / the source is not configured.
        """

        if not hoard_index_enabled():
            return False
        if not _LIST_BASE or not _DOWNLOAD_BASE:
            return False
        list_url = f"{_LIST_BASE}/{_LIST_QUERY}"
        download_url = f"{_DOWNLOAD_BASE}/{_DOWNLOAD_QUERY}"
        if not _validate_public_url(list_url) or not _validate_public_url(download_url):
            logger.error("Hoard index endpoint rejected by host validation")
            return False
        current = int(time.time() if now is None else now)
        async with self._lock:
            if self._syncing:
                return await self._has_any_snapshot()
            last = await self.last_sync()
            if not force and last is not None and current - last < _TTL_HOURS * 3600:
                return True
            self._syncing = True
            if background:
                task = asyncio.create_task(self._run_sync(current))
                self._sync_task = task
                task.add_done_callback(self._sync_done)
                return await self._has_any_snapshot()
            try:
                await self._run_sync(current)
                return True
            except Exception as exc:
                logger.warning("Hoard index sync failed error=%s", type(exc).__name__)
                return await self._has_any_snapshot()
            finally:
                self._syncing = False

    def _sync_done(self, task: "asyncio.Task[None]") -> None:
        self._syncing = False
        self._sync_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "Background hoard index sync failed error=%s",
                type(task.exception()).__name__,
            )

    async def _run_sync(self, current: int) -> None:
        try:
            await self._fetch_and_store(current)
        finally:
            self._syncing = False

    async def _fetch_and_store(self, current: int) -> None:
        breaker = get_circuit_breaker("hoard-index")
        started = time.perf_counter()
        try:
            try:
                await breaker.acquire()
            except CircuitOpenError:
                await _record_telemetry("provider.hoard_index", "refused", started)
                raise RuntimeError("hoard-index circuit open") from None
            try:
                dump_name = await self._latest_dump_name()
                if not dump_name:
                    raise RuntimeError("no hoard dump available upstream")
                text = await _download_dump(dump_name)
                items = _parse_dump(text)
                if not items:
                    raise ValueError("hoard dump is empty")
                snapshot = derive_snapshot(items, fetched_at=current, dump_name=dump_name)
                await breaker.succeed()
                await _record_telemetry("provider.hoard_index", "success", started)
            except Exception:
                await breaker.fail()
                await _record_telemetry("provider.hoard_index", "error", started)
                raise
        except Exception as exc:
            logger.warning("Hoard index sync failed error=%s", type(exc).__name__)
            raise

        def store(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO snapshots (
                    fetched_at, dump_name, dump_time, item_count, cs2_count,
                    median_ratio, p25_ratio, p75_ratio, best_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.fetched_at,
                    snapshot.dump_name,
                    snapshot.dump_time,
                    snapshot.item_count,
                    snapshot.cs2_count,
                    snapshot.median_ratio,
                    snapshot.p25_ratio,
                    snapshot.p75_ratio,
                    snapshot.best_ratio,
                ),
            )
            connection.execute("DELETE FROM item_ratios")
            rows = [
                (str(entry.get("hash_name")), ratio, snapshot.fetched_at)
                for entry in items
                if int(entry.get("appid") or 0) == _CS2_APPID
                and (ratio := _item_ratio(entry)) is not None
                and (str(entry.get("hash_name") or "")).strip()
            ]
            connection.executemany(
                "INSERT OR REPLACE INTO item_ratios (hash_name, ratio, fetched_at) VALUES (?, ?, ?)",
                rows,
            )
            connection.execute(
                "INSERT OR REPLACE INTO sync_state (source, fetched_at, status) VALUES (?, ?, 'ok')",
                ("iflow_priority_archive", snapshot.fetched_at),
            )
            connection.commit()

        await self._database_call(store)
        logger.info(
            "Hoard index synced dump=%s items=%d cs2=%d median=%s",
            snapshot.dump_name,
            snapshot.item_count,
            snapshot.cs2_count,
            _percentile_text(snapshot.median_ratio),
        )

    async def _latest_dump_name(self) -> str:
        """List the archive and return the newest ``*.zip`` file name."""

        payload = await _request_json_with_deadline(f"{_LIST_BASE}/{_LIST_QUERY}")
        files = payload.get("files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            raise ValueError("hoard archive list is malformed")
        names = [
            str(name) for name in files if isinstance(name, str) and name.strip().endswith(".zip")
        ]
        if not names:
            raise ValueError("hoard archive is empty")
        names.sort()
        return names[-1]

    async def latest_snapshot(self) -> HoardSnapshot | None:
        def read(connection: sqlite3.Connection) -> HoardSnapshot | None:
            row = connection.execute(
                "SELECT * FROM snapshots ORDER BY fetched_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return HoardSnapshot(
                fetched_at=int(row["fetched_at"]),
                dump_name=str(row["dump_name"]),
                dump_time=row["dump_time"],
                item_count=int(row["item_count"]),
                cs2_count=int(row["cs2_count"]),
                median_ratio=row["median_ratio"],
                p25_ratio=row["p25_ratio"],
                p75_ratio=row["p75_ratio"],
                best_ratio=row["best_ratio"],
            )

        try:
            return await self._database_call(read)
        except Exception as exc:
            logger.warning("Hoard snapshot read failed error=%s", type(exc).__name__)
            return None

    async def item_ratio(self, market_hash_name: str) -> float | None:
        """One item's hoard ratio from the latest snapshot; None = not tracked."""

        name = market_hash_name.strip()
        if not name:
            return None

        def read(connection: sqlite3.Connection) -> float | None:
            row = connection.execute(
                "SELECT ratio FROM item_ratios WHERE hash_name = ?", (name,)
            ).fetchone()
            return float(row["ratio"]) if row is not None else None

        try:
            return await self._database_call(read)
        except Exception as exc:
            logger.warning("Hoard item ratio read failed error=%s", type(exc).__name__)
            return None

    async def close(self) -> None:
        async with self._lock:
            task = self._sync_task
            self._sync_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _request_json_with_deadline(url: str) -> dict[str, Any]:
    """One JSON GET with a total deadline and public-host validation."""

    if not _validate_public_url(url):
        raise ValueError(f"hoard index URL rejected by host validation: {url}")
    from .http_client import request_json

    async with asyncio.timeout(_SYNC_TIMEOUT):
        return await request_json("GET", url, attempts=2, total_timeout=_SYNC_TIMEOUT)


async def _download_dump(dump_name: str) -> str:
    """Stream one dump zip through the shared client, following the R2 redirect.

    The shared client is configured with ``follow_redirects=False``, so the
    Cloudflare R2 301 is followed manually (bounded hops) exactly like the
    upstream docs request.  The zip is streamed to a bounded buffer, the
    single JSON member is decompressed in memory, and the result is decoded
    as UTF-8.
    """

    url = f"{_DOWNLOAD_BASE}/{_DOWNLOAD_QUERY}&file_name={urllib_quote(dump_name)}"
    if not _validate_public_url(url):
        raise ValueError(f"hoard download URL rejected by host validation: {url}")
    async with lease_http_client() as client:
        async with asyncio.timeout(_SYNC_TIMEOUT):
            target = url
            for _hop in range(_REDIRECT_LOCATIONS):
                async with client.stream("GET", target) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location or not _validate_public_url(location):
                            raise ValueError("hoard dump redirect is invalid")
                        target = location
                        continue
                    response.raise_for_status()
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        buffer.extend(chunk)
                        if len(buffer) > _MAX_DOWNLOAD_BYTES:
                            raise ValueError("hoard dump is too large")
                    if not buffer:
                        raise ValueError("hoard dump is empty")
                    return _extract_zip_text(buffer)
            raise ValueError("hoard dump redirect limit exceeded")


def _extract_zip_text(buffer: bytearray) -> str:
    """Return the single JSON member's decoded text from one zip in memory."""

    try:
        with zipfile.ZipFile(io.BytesIO(bytes(buffer))) as archive:
            members = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and (name.endswith(".json") or True)
            ]
            if not members:
                raise ValueError("hoard dump zip is empty")
            member = members[0]
            info = archive.getinfo(member)
            if info.file_size > _REQUEST_MAX_BYTES:
                raise ValueError("hoard dump member is too large")
            return archive.read(member).decode("utf-8")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"hoard dump zip is malformed: {type(exc).__name__}") from exc


def urllib_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


async def _record_telemetry(operation: str, outcome: str, started: float) -> None:
    try:
        await telemetry.record(operation, outcome, (time.perf_counter() - started) * 1000)
    except Exception as exc:
        logger.warning("Hoard index telemetry unavailable error=%s", type(exc).__name__)


hoard_index_store = HoardIndexStore()


async def hoard_summary_line() -> str | None:
    """One daily-report 挂刀 line; None when the snapshot is unusable."""

    try:
        snapshot = await hoard_index_store.latest_snapshot()
    except Exception as exc:
        logger.warning("Hoard summary line failed error=%s", type(exc).__name__)
        return None
    if snapshot is None:
        return None
    return format_hoard_summary(snapshot)


__all__ = [
    "HoardSnapshot",
    "HoardIndexStore",
    "derive_snapshot",
    "format_hoard_summary",
    "hoard_index_db_path",
    "hoard_index_enabled",
    "hoard_index_store",
    "hoard_summary_line",
]

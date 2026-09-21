"""Fail-closed CS2 inspect-link wear resolution behind a replaceable provider.

The whole feature is opt-in: when ``INVENTORY_FLOAT_API_BASE`` is unset the
default builder returns ``None`` and the inventory monitor must run exactly as
before (anchor columns stay NULL).  Any upstream answer that is private,
truncated, rate limited or malformed is skipped — never guessed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from .config import runtime_setting

logger = logging.getLogger("taffy.services.float_resolver")

FLOAT_API_BASE_ENV = "INVENTORY_FLOAT_API_BASE"
FLOAT_HOURLY_LIMIT_ENV = "INVENTORY_FLOAT_HOURLY_LIMIT"
DEFAULT_HOURLY_LIMIT = 60

_MAX_URL_LENGTH = 600
_MAX_STICKERS = 8
_MAX_STICKER_LABEL = 120
#: Only these transports may carry inspect links; everything else is dropped.
_ALLOWED_HTTP_HOSTS = frozenset({"api.steampowered.com"})

#: Transport seam for tests: ``callable(url, payload) -> Mapping``.
FloatTransport = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ItemFloatAnchors:
    """Validated wear anchors for one inspect link."""

    paintwear: float
    market_hash_name: str | None = None
    paint_index: int | None = None
    pattern_index: int | None = None
    stickers: tuple[str, ...] = ()


def _setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip()
    raw = runtime_setting(name)
    return str(raw).strip() if raw else None


def safe_inspect_url(raw: object) -> str | None:
    """Return the inspect link only when it passes the transport whitelist.

    Anything else — private hosts, localhost, arbitrary domains, oversized or
    non-string values — is rejected instead of sanitized (fail-closed).
    """

    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > _MAX_URL_LENGTH:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme == "steam":
        # A bare "steam://" carries no connect payload; require a body.
        return text if parsed.path or parsed.netloc else None
    if scheme in {"http", "https"} and parsed.hostname in _ALLOWED_HTTP_HOSTS:
        return text
    return None


def extract_inspect_url(record: Mapping[str, Any]) -> str | None:
    """Extract the first whitelisted inspect link from actions[] of an asset."""

    actions = record.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        return None
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        for key in ("link", "fallback_link", "url"):
            candidate = safe_inspect_url(action.get(key))
            if candidate is not None:
                return candidate
    return None


def normalize_paintwear(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _bounded_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_STICKER_LABEL:
        return None
    return text


def parse_sticker_labels(value: object) -> tuple[str, ...]:
    """Read bounded sticker labels from ``[{name|localizedName}]`` shapes."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    labels: list[str] = []
    for sticker in value[:_MAX_STICKERS]:
        candidate: object = sticker
        if isinstance(sticker, Mapping):
            candidate = sticker.get("name") or sticker.get("localizedName")
        label = _bounded_label(candidate)
        if label:
            labels.append(label)
    return tuple(labels)


def parse_float_payload(
    payload: Mapping[str, Any],
    urls: Sequence[str],
) -> dict[str, ItemFloatAnchors]:
    """Validate one resolver response body against the requested links.

    Per-item problems skip only that item; a structurally broken body skips all.
    """

    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return {}
    wanted = set(urls)
    results: dict[str, ItemFloatAnchors] = {}
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        url = raw_item.get("inspect_url")
        if not isinstance(url, str) or url not in wanted or url in results:
            continue
        paintwear = normalize_paintwear(raw_item.get("paintwear"))
        if paintwear is None:
            continue
        name = raw_item.get("market_hash_name")
        market_hash_name = name.strip()[:256] if isinstance(name, str) else ""
        results[url] = ItemFloatAnchors(
            paintwear=paintwear,
            market_hash_name=market_hash_name or None,
            paint_index=_optional_nonnegative_int(raw_item.get("paint_index")),
            pattern_index=_optional_nonnegative_int(raw_item.get("pattern_index")),
            stickers=parse_sticker_labels(raw_item.get("stickers")),
        )
    return results


class FloatCacheStore(Protocol):
    """Minimal persistence seam implemented by InventoryMonitorRepository."""

    async def load_float_cache(self, hashes: Sequence[str]) -> Mapping[str, ItemFloatAnchors]: ...

    async def save_float_cache(
        self, entries: Mapping[str, tuple[ItemFloatAnchors, int]]
    ) -> int: ...


def hash_inspect_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class NullFloatResolver:
    """Disabled-mode resolver: every lookup misses so anchors stay NULL."""

    async def resolve(self, urls: Sequence[str]) -> dict[str, ItemFloatAnchors]:
        return {}


class HttpFloatResolver:
    """Configurable third-party client with an hourly quota and durable cache."""

    def __init__(
        self,
        cache: FloatCacheStore,
        *,
        base_url: str,
        transport: FloatTransport | None = None,
        hourly_limit: int = DEFAULT_HOURLY_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        base = base_url.rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError("float api base must be an http(s) URL")
        self._cache = cache
        self._endpoint = f"{base}/resolve"
        self._hourly_limit = max(1, int(hourly_limit))
        self._transport = transport or self._http_transport
        self._clock = clock
        self._quota_window: list[float] = []

    @staticmethod
    async def _http_transport(url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        from .http_client import request_json

        return await request_json(
            "POST",
            url,
            json=dict(payload),
            attempts=2,
            total_timeout=8.0,
        )

    def _consume_quota(self) -> bool:
        now = float(self._clock())
        horizon = now - 3_600.0
        self._quota_window = [stamp for stamp in self._quota_window if stamp > horizon]
        if len(self._quota_window) >= self._hourly_limit:
            logger.info(
                "Float resolver quota exhausted calls=%s limit=%s",
                len(self._quota_window),
                self._hourly_limit,
            )
            return False
        self._quota_window.append(now)
        return True

    async def resolve(self, urls: Sequence[str]) -> dict[str, ItemFloatAnchors]:
        pending = [url for url in urls if safe_inspect_url(url)]
        if not pending:
            return {}
        by_hash = {hash_inspect_url(url): url for url in pending}
        cached = await self._cache.load_float_cache(tuple(by_hash.keys()))
        results: dict[str, ItemFloatAnchors] = {
            by_hash[digest]: anchors for digest, anchors in cached.items() if digest in by_hash
        }
        todo = [url for url in pending if url not in results]
        if todo and self._consume_quota():
            fresh = await self._fetch(todo)
            if fresh:
                await self._save(todo, fresh)
                results.update(fresh)
        return results

    async def _fetch(self, urls: list[str]) -> dict[str, ItemFloatAnchors]:
        try:
            body = await self._transport(self._endpoint, {"urls": urls})
        except Exception as exc:
            # Timeout / 429 / 5xx / malformed body: degrade silently, the
            # monitor keeps running without floats this round.
            logger.warning(
                "Float resolver unavailable error=%s count=%d",
                type(exc).__name__,
                len(urls),
            )
            return {}
        if not isinstance(body, Mapping):
            return {}
        return parse_float_payload(body, urls)

    async def _save(
        self,
        urls: list[str],
        fetched: Mapping[str, ItemFloatAnchors],
    ) -> None:
        stamp = int(time.time())
        wanted = set(urls)
        entries: dict[str, tuple[ItemFloatAnchors, int]] = {
            hash_inspect_url(url): (anchors, stamp)
            for url, anchors in fetched.items()
            if url in wanted
        }
        try:
            await self._cache.save_float_cache(entries)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Float cache write failed error=%s", type(exc).__name__)


def build_default_resolver(cache: FloatCacheStore) -> HttpFloatResolver | None:
    """Build the env-configured resolver; ``None`` means the feature is off."""

    base = _setting(FLOAT_API_BASE_ENV)
    if not base:
        return None
    limit_text = _setting(FLOAT_HOURLY_LIMIT_ENV)
    limit = DEFAULT_HOURLY_LIMIT
    if limit_text and limit_text.isascii() and limit_text.isdecimal():
        limit = min(max(int(limit_text), 1), 10_000)
    try:
        return HttpFloatResolver(cache, base_url=base, hourly_limit=limit)
    except ValueError:
        logger.warning("Ignoring invalid %s configuration", FLOAT_API_BASE_ENV)
        return None


def serialize_stickers(stickers: Sequence[str]) -> str:
    """Compact deterministic JSON summary for history/cache storage."""

    return json.dumps(list(stickers[:_MAX_STICKERS]), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "DEFAULT_HOURLY_LIMIT",
    "FLOAT_API_BASE_ENV",
    "FLOAT_HOURLY_LIMIT_ENV",
    "FloatCacheStore",
    "HttpFloatResolver",
    "ItemFloatAnchors",
    "NullFloatResolver",
    "build_default_resolver",
    "extract_inspect_url",
    "hash_inspect_url",
    "normalize_paintwear",
    "parse_float_payload",
    "parse_sticker_labels",
    "safe_inspect_url",
    "serialize_stickers",
]

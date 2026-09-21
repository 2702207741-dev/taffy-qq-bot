"""Isolated, cached local K-line screenshot service for broad and item charts."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import psutil

from .config import PROJECT_ROOT, bounded_env_float, bounded_env_int, runtime_setting
from .http_client import request_json
from .kline_fast_renderer import render_kline_png
from .market import st_post
from .market_special_style import SPECIAL_STYLE_LABELS, validated_special_style
from .steam_inventory import checksum_timestamp

logger = logging.getLogger("taffy.services.steamdt_kline")

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_MIN_PNG_BYTES = 1_024
_MIN_WIDTH = 1_200
_MIN_HEIGHT = 800
_MAX_DIMENSION = 8_192
_WINDOWS_IDLE_PRIORITY: int = cast(int, getattr(psutil, "IDLE_PRIORITY_CLASS", 0))
_WORKER = PROJECT_ROOT / "src" / "services" / "steamdt_kline_worker.py"
_BROAD_KLINE_PATH = "/open/cs2/broad/v1/kline"
_ITEM_KLINE_PATH = "/open/cs2/item/v1/kline"
#: Item K-line platform filter (doc: ALL/BUFF/YOUPIN/C5/STEAM/HALOSKINS).
KLINE_PLATFORMS = ("ALL", "BUFF", "YOUPIN", "C5", "STEAM", "HALOSKINS")
KLINE_PLATFORM_LABELS = {
    "ALL": "全平台",
    "BUFF": "BUFF",
    "YOUPIN": "悠悠",
    "C5": "C5",
    "STEAM": "Steam",
    "HALOSKINS": "HALO",
}
_KLINE_PLATFORM_ALIASES = {
    "all": "ALL",
    "全平台": "ALL",
    "buff": "BUFF",
    "youpin": "YOUPIN",
    "悠悠": "YOUPIN",
    "c5": "C5",
    "steam": "STEAM",
    "halo": "HALOSKINS",
    "haloskins": "HALOSKINS",
}
#: Broad backtest lookback bound (days).
_MAX_BACKTEST_DAYS = 90
_MAX_ITEM_CACHE = 64
_FAILURE_CACHE_SECONDS = 60.0
_KLINE_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
_FUTURE_TOLERANCE_SECONDS = 5 * 60
_OHLC_ENVELOPE_TOLERANCE_RATIO = 0.0025
_WEB_BASE = "https://www.steamdt.com/api"
_WEB_HEADERS = {
    "x-device-id": "",
    "x-device": "1",
    "x-app-version": "1.0.0",
    "access-token": "",
    "x-currency": "CNY",
    "language": "zh_CN",
}
_BROWSER_RENDER_SEMAPHORE = asyncio.Semaphore(1)

_CHILD_ENV_ALLOWLIST = (
    "ALL_PROXY",
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)


class KlineScreenshotError(RuntimeError):
    """Raised when a complete, validated K-line image cannot be produced."""


@dataclass(frozen=True, slots=True)
class KlineScreenshotConfig:
    timeout_seconds: float
    terminate_grace_seconds: float
    cache_seconds: float
    max_bytes: int


Capture = Callable[[], Awaitable[bytes]]


class ItemCapture(Protocol):
    def __call__(
        self,
        market_name: str,
        config: KlineScreenshotConfig | None = None,
        *,
        special_style: str | None = None,
    ) -> Awaitable[bytes]: ...


KlineFetcher = Callable[[], Awaitable[Mapping[str, Any]]]
MarketPoster = Callable[
    [str, dict[str, Any] | None],
    Awaitable[Mapping[str, Any]],
]


def load_kline_screenshot_config() -> KlineScreenshotConfig:
    """Load one bounded configuration snapshot for screenshot capture."""

    return KlineScreenshotConfig(
        timeout_seconds=bounded_env_float(
            "TAFFY_KLINE_SCREENSHOT_TIMEOUT_SECONDS",
            45.0,
            10.0,
            120.0,
        ),
        terminate_grace_seconds=bounded_env_float(
            "TAFFY_KLINE_SCREENSHOT_TERMINATE_GRACE_SECONDS",
            5.0,
            0.5,
            15.0,
        ),
        cache_seconds=bounded_env_float(
            "TAFFY_KLINE_SCREENSHOT_CACHE_SECONDS",
            60.0,
            5.0,
            600.0,
        ),
        max_bytes=bounded_env_int(
            "TAFFY_KLINE_SCREENSHOT_MAX_BYTES",
            5 * 1024 * 1024,
            64 * 1024,
            10 * 1024 * 1024,
        ),
    )


def _worker_environment() -> dict[str, str]:
    """Build a minimal browser-worker environment without bot credentials."""

    environment = {
        key: value for key in _CHILD_ENV_ALLOWLIST if (value := os.environ.get(key)) is not None
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    if browser_path := runtime_setting("PLAYWRIGHT_BROWSERS_PATH"):
        environment["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
    return environment


def validate_kline_png(data: bytes, *, max_bytes: int) -> tuple[int, int]:
    """Validate the PNG envelope and return its dimensions."""

    if len(data) < _MIN_PNG_BYTES or len(data) > max_bytes:
        raise KlineScreenshotError("invalid screenshot size")
    if not data.startswith(_PNG_SIGNATURE):
        raise KlineScreenshotError("screenshot is not PNG")
    if not data.endswith(_PNG_IEND):
        raise KlineScreenshotError("screenshot PNG is incomplete")
    if data[12:16] != b"IHDR" or int.from_bytes(data[8:12], "big") != 13:
        raise KlineScreenshotError("screenshot PNG header is invalid")

    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if (
        width < _MIN_WIDTH
        or height < _MIN_HEIGHT
        or width > _MAX_DIMENSION
        or height > _MAX_DIMENSION
    ):
        raise KlineScreenshotError("screenshot dimensions are incomplete")
    return width, height


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _parse_kline_points(
    response: Mapping[str, Any],
    *,
    now: float,
) -> list[list[float]]:
    raw_points = response.get("data") if response.get("success") else None
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
        raise KlineScreenshotError("K-line data is unavailable")
    parsed: list[tuple[float, float, float, float, float]] = []
    previous_timestamp = 0.0
    possible_orders = {"OCHL", "OHLC"}
    for raw in raw_points:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 5:
            raise KlineScreenshotError("K-line row is invalid")
        values = [_finite_positive(value) for value in raw[:5]]
        if any(value is None for value in values):
            raise KlineScreenshotError("K-line value is invalid")
        timestamp, first, second, third, fourth = (
            float(value) for value in values if value is not None
        )
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        if timestamp <= previous_timestamp:
            raise KlineScreenshotError("K-line timestamps are not strictly ordered")
        # Both array layouts exist across the broad/item endpoints. Determine
        # one layout for the complete response, then normalize it to O,C,H,L.
        row_orders: set[str] = set()
        tolerance = max(first, second, third, fourth) * _OHLC_ENVELOPE_TOLERANCE_RATIO
        if third + tolerance >= max(first, second, fourth) and fourth - tolerance <= min(
            first, second, third
        ):
            row_orders.add("OCHL")
        if second + tolerance >= max(first, third, fourth) and third - tolerance <= min(
            first, second, fourth
        ):
            row_orders.add("OHLC")
        possible_orders &= row_orders
        if not possible_orders:
            raise KlineScreenshotError("K-line OHLC relationship is invalid")
        previous_timestamp = timestamp
        parsed.append((timestamp, first, second, third, fourth))
    order = "OCHL" if "OCHL" in possible_orders else "OHLC"
    points: list[list[float]] = []
    for timestamp, first, second, third, fourth in parsed:
        if order == "OCHL":
            open_price, close_price, high_price, low_price = first, second, third, fourth
        else:
            open_price, close_price, high_price, low_price = first, fourth, second, third
        # Some daily rows round the declared high/low a few cents differently
        # from open/close. The tolerance check above bounds that discrepancy;
        # normalization merely restores the OHLC invariant for drawing.
        points.append(
            [
                timestamp,
                open_price,
                close_price,
                max(high_price, open_price, close_price),
                min(low_price, open_price, close_price),
            ]
        )
    if len(points) < 30:
        raise KlineScreenshotError("K-line history is insufficient")
    latest = points[-1][0]
    if latest > now + _FUTURE_TOLERANCE_SECONDS or now - latest > _KLINE_MAX_AGE_SECONDS:
        raise KlineScreenshotError("K-line history is stale")
    return points[-120:]


async def fetch_broad_kline(max_time: int | None = None) -> Mapping[str, Any]:
    primary = await st_post(
        _BROAD_KLINE_PATH,
        {"type": 2, **({"maxTime": max_time} if max_time is not None else {})},
    )
    if primary.get("success"):
        return primary
    # Historical (maxTime) requests never fall back: the web fallback only
    # serves the current 90-day series and cannot honour a past cutoff, so
    # returning it here would paint current data as a history backtest.
    if max_time is not None:
        return primary
    # The open endpoint has a tight quota. This read-only fallback obtains the
    # same 90-day OHLC series used by the public chart and still renders it
    # locally. It never calls a refresh or mutation route.
    config = await request_json(
        "GET",
        f"{_WEB_BASE}/user/system-config/v1/default-config",
        headers=_WEB_HEADERS,
        attempts=2,
        total_timeout=8.0,
    )
    config_data = config.get("data") if config.get("success") else None
    if not isinstance(config_data, Mapping):
        return primary
    timestamp = checksum_timestamp(config_data.get("systemTime"))
    fallback = await request_json(
        "GET",
        f"{_WEB_BASE}/user/statistics/v1/kline",
        headers=_WEB_HEADERS,
        params={"type": 2, "timestamp": timestamp},
        attempts=2,
        total_timeout=12.0,
    )
    return fallback if fallback.get("success") else primary


async def _stop_worker(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        logger.warning("K-line screenshot worker ignored terminate; escalating to kill")

    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        logger.error("K-line screenshot worker did not exit after kill")


async def _capture_kline_png(
    title: str,
    config: KlineScreenshotConfig | None = None,
    *,
    fetcher: KlineFetcher,
    wall_clock: Callable[[], float] = time.time,
    high_definition: bool = False,
    cutoff: int | None = None,
) -> bytes:
    """Fetch validated OHLC data and render it in an isolated local worker.

    ``cutoff`` is the single historical deadline (epoch seconds) for
    backtest charts: it is the ``now`` the parsed data is judged against,
    and data whose last point still lies after it is rejected before any
    renderer runs.  ``None`` keeps the current-quote behaviour unchanged.
    """

    current = config or load_kline_screenshot_config()
    clean_title = title.strip()
    if not clean_title or len(clean_title) > 100:
        raise KlineScreenshotError("K-line chart title is invalid")
    if not _WORKER.is_file():
        raise KlineScreenshotError("K-line screenshot worker is unavailable")

    try:
        response = await fetcher()
        points = _parse_kline_points(response, now=cutoff if cutoff is not None else wall_clock())
    except KlineScreenshotError:
        raise
    except Exception as exc:
        raise KlineScreenshotError("unable to fetch K-line data") from exc
    # Historical deadline is strict: one second past the cutoff must never
    # reach the renderers (no 5-minute future tolerance for backtests).
    if cutoff is not None and points[-1][0] > cutoff:
        raise KlineScreenshotError("K-line history is stale")

    if not high_definition:
        try:
            png = await asyncio.to_thread(render_kline_png, clean_title, points)
            validate_kline_png(png, max_bytes=current.max_bytes)
            return png
        except Exception as exc:
            logger.warning(
                "Fast K-line renderer failed; using browser fallback error=%s", type(exc).__name__
            )

    await _BROWSER_RENDER_SEMAPHORE.acquire()
    output: Path | None = None
    data_file: Path | None = None
    process: asyncio.subprocess.Process | None = None
    try:
        descriptor, output_name = tempfile.mkstemp(prefix="taffy_kline_", suffix=".png")
        os.close(descriptor)
        output = Path(output_name)
        data_descriptor, data_name = tempfile.mkstemp(prefix="taffy_kline_", suffix=".json")
        os.close(data_descriptor)
        data_file = Path(data_name)
        try:
            await asyncio.to_thread(
                data_file.write_text,
                json.dumps(
                    {"title": clean_title, "points": points},
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except OSError as exc:
            raise KlineScreenshotError("unable to prepare K-line render data") from exc
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(_WORKER),
                str(output),
                str(data_file),
                cwd=str(PROJECT_ROOT),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=_worker_environment(),
            )
            await asyncio.to_thread(_lower_process_priority, process.pid)
        except (OSError, ValueError) as exc:
            raise KlineScreenshotError("unable to start K-line screenshot worker") from exc

        try:
            await asyncio.wait_for(process.wait(), timeout=current.timeout_seconds)
        except TimeoutError as exc:
            await _stop_worker(process, grace_seconds=current.terminate_grace_seconds)
            raise KlineScreenshotError("K-line screenshot timed out") from exc
        except asyncio.CancelledError:
            await asyncio.shield(
                _stop_worker(process, grace_seconds=current.terminate_grace_seconds)
            )
            raise
        except BaseException:
            await _stop_worker(process, grace_seconds=current.terminate_grace_seconds)
            raise

        if process.returncode != 0:
            raise KlineScreenshotError("K-line screenshot worker failed")
        try:
            assert output is not None
            png = await asyncio.to_thread(output.read_bytes)
        except OSError as exc:
            raise KlineScreenshotError("K-line screenshot output is unavailable") from exc
        validate_kline_png(png, max_bytes=current.max_bytes)
        return png
    finally:
        try:
            if output is not None:
                output.unlink(missing_ok=True)
            if data_file is not None:
                data_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("Unable to delete temporary K-line render files")
        _BROWSER_RENDER_SEMAPHORE.release()


def _lower_process_priority(process_id: int) -> None:
    try:
        process = psutil.Process(process_id)
        if os.name == "nt":
            process.nice(_WINDOWS_IDLE_PRIORITY)
        else:
            process.nice(10)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
        logger.debug("Unable to lower K-line worker priority")


def validated_kline_platform(value: str | None) -> str | None:
    """Normalize a platform token to a doc code; ``None`` when absent/invalid."""

    if value is None:
        return None
    return _KLINE_PLATFORM_ALIASES.get(value.strip().lower())


def parse_kline_platform_suffix(query: str) -> tuple[str, str | None]:
    """Split a trailing platform token; ``(clean_query, platform|None)``."""

    head_tail = query.strip().rsplit(None, 1)
    if len(head_tail) != 2:
        return query.strip(), None
    head, tail = head_tail
    code = _KLINE_PLATFORM_ALIASES.get(tail.strip().lower())
    if code is None or not head:
        return query.strip(), None
    return head, code


async def capture_broad_kline_png(
    config: KlineScreenshotConfig | None = None,
    *,
    fetcher: KlineFetcher = fetch_broad_kline,
    wall_clock: Callable[[], float] = time.time,
) -> bytes:
    return await _capture_kline_png(
        "塔菲 · CS2 市场日K",
        config,
        fetcher=fetcher,
        wall_clock=wall_clock,
    )


async def capture_broad_kline_hd_png(
    config: KlineScreenshotConfig | None = None,
    *,
    fetcher: KlineFetcher = fetch_broad_kline,
    wall_clock: Callable[[], float] = time.time,
) -> bytes:
    return await _capture_kline_png(
        "塔菲 · CS2 市场日K",
        config,
        fetcher=fetcher,
        wall_clock=wall_clock,
        high_definition=True,
    )


async def capture_broad_backtest_png(
    max_days_ago: int,
    *,
    high_definition: bool = False,
    wall_clock: Callable[[], float] = time.time,
) -> bytes:
    """Broad chart ending ``max_days_ago`` days ago (maxTime lookback).

    Never cached: each lookback is a distinct upstream query.  Bounds are
    1–90 days; violations raise like any other invalid request.
    """

    if not 1 <= max_days_ago <= _MAX_BACKTEST_DAYS:
        raise KlineScreenshotError("broad K-line lookback is invalid")
    cutoff = int(wall_clock()) - max_days_ago * 86400

    async def fetcher() -> Mapping[str, Any]:
        return await fetch_broad_kline(max_time=cutoff)

    return await _capture_kline_png(
        f"塔菲 · CS2 市场日K（{max_days_ago}天前截止）",
        None,
        fetcher=fetcher,
        wall_clock=wall_clock,
        high_definition=high_definition,
        cutoff=cutoff,
    )


async def capture_item_kline_png(
    market_name: str,
    config: KlineScreenshotConfig | None = None,
    *,
    special_style: str | None = None,
    platform: str | None = None,
    market_post: MarketPoster = st_post,
    wall_clock: Callable[[], float] = time.time,
) -> bytes:
    clean_name = market_name.strip()
    if not clean_name or len(clean_name) > 200:
        raise KlineScreenshotError("item K-line name is invalid")
    style = validated_special_style(special_style)
    if special_style is not None and style is None:
        raise KlineScreenshotError("item K-line special style is invalid")
    venue = validated_kline_platform(platform)
    if platform is not None and venue is None:
        raise KlineScreenshotError("item K-line platform is invalid")

    async def fetcher() -> Mapping[str, Any]:
        body: dict[str, Any] = {"marketHashName": clean_name, "type": 2}
        if style is not None:
            body["specialStyle"] = style
        if venue is not None and venue != "ALL":
            body["platform"] = venue
        return await market_post(
            _ITEM_KLINE_PATH,
            body,
        )

    title = f"塔菲 · {clean_name}"
    if style is not None:
        title += f" · {SPECIAL_STYLE_LABELS[style]}"
    if venue is not None and venue != "ALL":
        title += f" · {KLINE_PLATFORM_LABELS[venue]}"
    if len(title) > 100:
        title = title[:99] + "…"
    return await _capture_kline_png(
        title,
        config,
        fetcher=fetcher,
        wall_clock=wall_clock,
    )


async def capture_item_kline_hd_png(
    market_name: str,
    config: KlineScreenshotConfig | None = None,
    *,
    special_style: str | None = None,
    platform: str | None = None,
    market_post: MarketPoster = st_post,
    wall_clock: Callable[[], float] = time.time,
) -> bytes:
    clean_name = market_name.strip()
    if not clean_name or len(clean_name) > 200:
        raise KlineScreenshotError("item K-line name is invalid")
    style = validated_special_style(special_style)
    if special_style is not None and style is None:
        raise KlineScreenshotError("item K-line special style is invalid")
    venue = validated_kline_platform(platform)
    if platform is not None and venue is None:
        raise KlineScreenshotError("item K-line platform is invalid")

    async def fetcher() -> Mapping[str, Any]:
        body: dict[str, Any] = {"marketHashName": clean_name, "type": 2}
        if style is not None:
            body["specialStyle"] = style
        if venue is not None and venue != "ALL":
            body["platform"] = venue
        return await market_post(
            _ITEM_KLINE_PATH,
            body,
        )

    title = f"塔菲 · {clean_name}"
    if style is not None:
        title += f" · {SPECIAL_STYLE_LABELS[style]}"
    if venue is not None and venue != "ALL":
        title += f" · {KLINE_PLATFORM_LABELS[venue]}"
    if len(title) > 100:
        title = title[:99] + "…"
    return await _capture_kline_png(
        title,
        config,
        fetcher=fetcher,
        wall_clock=wall_clock,
        high_definition=True,
    )


class SteamDTKlineScreenshotService:
    """Single-flight, bounded caches around isolated screenshot captures."""

    def __init__(
        self,
        *,
        capture: Capture = capture_broad_kline_png,
        item_capture: ItemCapture = capture_item_kline_png,
        hd_capture: Capture = capture_broad_kline_hd_png,
        hd_item_capture: ItemCapture = capture_item_kline_hd_png,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float | None = None,
        max_bytes: int | None = None,
        max_item_cache: int = _MAX_ITEM_CACHE,
    ) -> None:
        config = load_kline_screenshot_config()
        self._capture = capture
        self._item_capture = item_capture
        self._hd_capture = hd_capture
        self._hd_item_capture = hd_item_capture
        self._clock = clock
        self._cache_seconds = config.cache_seconds if cache_seconds is None else cache_seconds
        self._max_bytes = config.max_bytes if max_bytes is None else max_bytes
        self._max_item_cache = max(1, min(max_item_cache, 256))
        self._cache: bytes | None = None
        self._cache_time = 0.0
        self._inflight: asyncio.Task[bytes] | None = None
        self._item_cache: dict[tuple[str, str | None, str | None], tuple[float, bytes]] = {}
        self._item_inflight: dict[tuple[str, str | None, str | None], asyncio.Task[bytes]] = {}
        # Short-lived failure markers: while an upstream/Chromium outage is
        # active, repeat requests fail fast instead of paying the full capture.
        self._item_failures: dict[tuple[str, str | None, str | None], float] = {}
        self._hd_cache: bytes | None = None
        self._hd_cache_time = 0.0
        self._hd_inflight: asyncio.Task[bytes] | None = None
        self._hd_item_cache: dict[tuple[str, str | None, str | None], tuple[float, bytes]] = {}
        self._hd_item_inflight: dict[tuple[str, str | None, str | None], asyncio.Task[bytes]] = {}
        self._lock = asyncio.Lock()

    async def _capture_and_cache(self) -> bytes:
        png = bytes(await self._capture())
        validate_kline_png(png, max_bytes=self._max_bytes)
        async with self._lock:
            self._cache = png
            self._cache_time = self._clock()
        return png

    async def _capture_item_and_cache(
        self,
        market_name: str,
        special_style: str | None,
        platform: str | None = None,
    ) -> bytes:
        key = (market_name, special_style, platform)
        current_task = asyncio.current_task()
        try:
            extra: dict[str, Any] = {}
            if special_style is not None:
                extra["special_style"] = special_style
            if platform is not None:
                extra["platform"] = platform
            request = self._item_capture(market_name, **extra)
            png = bytes(await request)
            validate_kline_png(png, max_bytes=self._max_bytes)
            async with self._lock:
                if len(self._item_cache) >= self._max_item_cache and key not in self._item_cache:
                    oldest = min(
                        self._item_cache,
                        key=lambda key: self._item_cache[key][0],
                    )
                    self._item_cache.pop(oldest, None)
                self._item_cache[key] = (self._clock(), png)
                self._item_failures.pop(key, None)
            return png
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                self._item_failures[key] = self._clock()
            raise
        finally:
            async with self._lock:
                if self._item_inflight.get(key) is current_task:
                    self._item_inflight.pop(key, None)

    async def _capture_hd_and_cache(self) -> bytes:
        png = bytes(await self._hd_capture())
        validate_kline_png(png, max_bytes=self._max_bytes)
        async with self._lock:
            self._hd_cache = png
            self._hd_cache_time = self._clock()
        return png

    async def _capture_hd_item_and_cache(
        self,
        market_name: str,
        special_style: str | None,
        platform: str | None = None,
    ) -> bytes:
        key = (market_name, special_style, platform)
        current_task = asyncio.current_task()
        try:
            extra: dict[str, Any] = {}
            if special_style is not None:
                extra["special_style"] = special_style
            if platform is not None:
                extra["platform"] = platform
            request = self._hd_item_capture(market_name, **extra)
            png = bytes(await request)
            validate_kline_png(png, max_bytes=self._max_bytes)
            async with self._lock:
                if (
                    len(self._hd_item_cache) >= self._max_item_cache
                    and key not in self._hd_item_cache
                ):
                    oldest = min(self._hd_item_cache, key=lambda key: self._hd_item_cache[key][0])
                    self._hd_item_cache.pop(oldest, None)
                self._hd_item_cache[key] = (self._clock(), png)
            return png
        finally:
            async with self._lock:
                if self._hd_item_inflight.get(key) is current_task:
                    self._hd_item_inflight.pop(key, None)

    async def get_broad_png(self) -> bytes:
        """Return a recent full-screen chart, sharing one refresh among callers."""

        async with self._lock:
            now = self._clock()
            if self._cache is not None and now - self._cache_time < self._cache_seconds:
                return self._cache
            if self._inflight is None or self._inflight.done():
                self._inflight = asyncio.create_task(self._capture_and_cache())
            task = self._inflight
        return await asyncio.shield(task)

    async def get_item_png(
        self,
        market_name: str,
        *,
        special_style: str | None = None,
        platform: str | None = None,
    ) -> bytes:
        """Return one resolved item's recent chart with per-item single-flight."""

        clean_name = market_name.strip()
        if not clean_name or len(clean_name) > 200:
            raise KlineScreenshotError("item K-line name is invalid")
        style = validated_special_style(special_style)
        if special_style is not None and style is None:
            raise KlineScreenshotError("item K-line special style is invalid")
        venue = validated_kline_platform(platform)
        if platform is not None and venue is None:
            raise KlineScreenshotError("item K-line platform is invalid")
        key = (clean_name, style, venue)
        async with self._lock:
            now = self._clock()
            cached = self._item_cache.get(key)
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]
            failed_at = self._item_failures.get(key)
            if failed_at is not None and now - failed_at < _FAILURE_CACHE_SECONDS:
                # Negative cache: a fresh failure keeps failing fast so an
                # outage does not route every message through the renderer.
                raise KlineScreenshotError("item K-line is temporarily unavailable")
            task = self._item_inflight.get(key)
            if task is None or task.done():
                task = asyncio.create_task(self._capture_item_and_cache(clean_name, style, venue))
                self._item_inflight[key] = task
        return await asyncio.shield(task)

    async def get_broad_hd_png(self) -> bytes:
        """Force the isolated Playwright renderer while retaining cache/single-flight."""

        async with self._lock:
            now = self._clock()
            if self._hd_cache is not None and now - self._hd_cache_time < self._cache_seconds:
                return self._hd_cache
            if self._hd_inflight is None or self._hd_inflight.done():
                self._hd_inflight = asyncio.create_task(self._capture_hd_and_cache())
            task = self._hd_inflight
        return await asyncio.shield(task)

    async def get_item_hd_png(
        self,
        market_name: str,
        *,
        special_style: str | None = None,
        platform: str | None = None,
    ) -> bytes:
        clean_name = market_name.strip()
        if not clean_name or len(clean_name) > 200:
            raise KlineScreenshotError("item K-line name is invalid")
        style = validated_special_style(special_style)
        if special_style is not None and style is None:
            raise KlineScreenshotError("item K-line special style is invalid")
        venue = validated_kline_platform(platform)
        if platform is not None and venue is None:
            raise KlineScreenshotError("item K-line platform is invalid")
        key = (clean_name, style, venue)
        async with self._lock:
            now = self._clock()
            cached = self._hd_item_cache.get(key)
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]
            task = self._hd_item_inflight.get(key)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._capture_hd_item_and_cache(clean_name, style, venue)
                )
                self._hd_item_inflight[key] = task
        return await asyncio.shield(task)

    async def close(self) -> None:
        """Cancel and reap an in-progress browser worker during shutdown."""

        async with self._lock:
            tasks = [
                task
                for task in (
                    self._inflight,
                    self._hd_inflight,
                    *self._item_inflight.values(),
                    *self._hd_item_inflight.values(),
                )
                if task is not None and not task.done()
            ]
            self._inflight = None
            self._cache = None
            self._cache_time = 0.0
            self._item_cache.clear()
            self._item_inflight.clear()
            self._hd_inflight = None
            self._hd_cache = None
            self._hd_cache_time = 0.0
            self._hd_item_cache.clear()
            self._hd_item_inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


steamdt_kline_screenshots = SteamDTKlineScreenshotService()


async def close_steamdt_kline_service() -> None:
    await steamdt_kline_screenshots.close()


__all__ = [
    "KLINE_PLATFORMS",
    "KLINE_PLATFORM_LABELS",
    "KlineScreenshotConfig",
    "KlineScreenshotError",
    "SteamDTKlineScreenshotService",
    "capture_broad_backtest_png",
    "capture_broad_kline_png",
    "capture_broad_kline_hd_png",
    "capture_item_kline_png",
    "capture_item_kline_hd_png",
    "close_steamdt_kline_service",
    "load_kline_screenshot_config",
    "steamdt_kline_screenshots",
    "fetch_broad_kline",
    "parse_kline_platform_suffix",
    "validate_kline_png",
    "validated_kline_platform",
]

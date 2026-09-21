"""Explainable broad-market temperature derived from validated daily closes."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from .http_client import request_json

logger = logging.getLogger("taffy.services.market_temperature")

_DAILY_CHART_URL = "https://www.steamdt.com/api/user/statistics/v2/chart?type=2&dateType=2&maxTime"
_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_BROAD_MAX_AGE_SECONDS = 15 * 60
_FUTURE_TOLERANCE_SECONDS = 5 * 60
_FRESH_CACHE_SECONDS = 30 * 60
_STALE_CACHE_SECONDS = 6 * 60 * 60


@dataclass(frozen=True, slots=True)
class MarketTemperature:
    """A complete temperature result calculated from real index observations."""

    score: float
    change_1d: float
    change_3d: float
    change_7d: float
    deviation_7d: float
    current_state: str
    detail: str
    temperature_state: str
    liquidity_ratio: float | None = None
    liquidity_note: str | None = None


@dataclass(frozen=True, slots=True)
class TemperatureUnavailable:
    """A fail-closed result that never fabricates a temperature."""

    reason: str = "历史样本不足"


TemperatureResult = MarketTemperature | TemperatureUnavailable
DailyCloses = dict[date, float]
DailyChartFetcher = Callable[[], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class _HistoryCache:
    closes: DailyCloses
    latest_timestamp: float
    stored_at: float


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) and result > 0 else None


def _parse_timestamp(value: object) -> float | None:
    timestamp = _finite_positive(value)
    if timestamp is None:
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return timestamp


def _broad_timestamp_is_fresh(timestamp: float, now: float) -> bool:
    return (
        timestamp <= now + _FUTURE_TOLERANCE_SECONDS and now - timestamp <= _BROAD_MAX_AGE_SECONDS
    )


def parse_daily_closes(
    payload: Mapping[str, Any],
    *,
    timezone: tzinfo = _MARKET_TIMEZONE,
) -> DailyCloses:
    """Parse a strictly ordered daily chart response into local-date closes."""

    rows = payload.get("data") if payload.get("success") else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise ValueError("daily chart data is unavailable")

    closes: DailyCloses = {}
    previous_timestamp = 0.0
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) != 2:
            raise ValueError("daily chart row is invalid")
        timestamp = _parse_timestamp(row[0])
        close = _finite_positive(row[1])
        if timestamp is None or close is None:
            raise ValueError("daily chart value is invalid")
        if timestamp <= previous_timestamp:
            raise ValueError("daily chart timestamps are not strictly ordered")
        previous_timestamp = timestamp
        try:
            local_date = datetime.fromtimestamp(timestamp, timezone).date()
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError("daily chart timestamp is invalid") from exc
        if local_date in closes:
            raise ValueError("daily chart contains a duplicate date")
        closes[local_date] = close
    return closes


def _clip(value: float) -> float:
    return min(1.0, max(-1.0, value))


def _round_score(value: float) -> float:
    score = float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return 0.0 if score == 0 else score


def classify_temperature(score: float) -> tuple[str, str]:
    """Map an already calculated score to its market and temperature labels."""

    if score <= -70:
        return "极端下行", "极冷"
    if score <= -35:
        return "弱势下行", "明显偏冷"
    if score <= -10:
        return "低位承压", "偏冷"
    if score < 10:
        return "多空平衡", "中性"
    if score < 35:
        return "温和回暖", "偏热"
    if score < 70:
        return "强势上行", "明显偏热"
    return "高位过热", "极热"


#: Factor weights per path.  The entry count is the single source of truth for
#: how many contribution slots ``_temperature_detail`` may be asked to name:
#: adding a sixth factor without extending ``_FACTOR_DESCRIPTION_PAIRS`` is then
#: caught by tests/test_market_temperature.py instead of crashing in production.
#: The four-factor allocation is unchanged; the five-factor one rescales
#: proportionally so the weights still sum to exactly 1.0.
_FOUR_FACTOR_WEIGHTS: tuple[float, ...] = (0.35, 0.25, 0.25, 0.15)
_FIVE_FACTOR_WEIGHTS: tuple[float, ...] = (0.28, 0.20, 0.20, 0.12, 0.20)

#: One ``(descending, ascending)`` label per contribution slot.  Must stay the
#: same length as ``_FIVE_FACTOR_WEIGHTS`` — slots 0..3 are the momentum
#: factors (whose wording is frozen), slot 4 is the liquidity proxy.
_FACTOR_DESCRIPTION_PAIRS: tuple[tuple[str, str], ...] = (
    ("日内下行主导", "日内上行主导"),
    ("3日动量承压", "3日动量增强"),
    ("7日趋势压制", "7日趋势抬升"),
    ("低于7日均值", "高于7日均值"),
    ("流动性萎缩主导", "流动性放大主导"),
)


def _temperature_detail(
    change_1d: float,
    change_3d: float,
    change_7d: float,
    contributions: Sequence[float],
) -> str:
    changes = (change_1d, change_3d, change_7d)
    if all(change < 0 for change in changes):
        return "1/3/7日动量同步偏弱"
    if all(change > 0 for change in changes):
        return "1/3/7日动量同步偏强"
    if change_1d > 0 and change_7d < 0:
        return "短线反弹，7日趋势仍弱"
    if change_1d < 0 and change_7d > 0:
        return "短线回撤，7日趋势仍强"

    factor = max(range(len(contributions)), key=lambda index: abs(contributions[index]))
    positive = contributions[factor] >= 0
    return _FACTOR_DESCRIPTION_PAIRS[factor][int(positive)]


def calculate_market_temperature(
    *,
    current_index: object,
    current_timestamp: object,
    daily_closes: Mapping[date, float],
    now: float | None = None,
    timezone: tzinfo = _MARKET_TIMEZONE,
    liquidity_ratio: float | None = None,
) -> TemperatureResult:
    """Calculate the fixed temperature formula, failing closed.

    ``liquidity_ratio`` is the optional fifth factor — a whole-market
    liquidity-surge detector (structural-event factor).  Until cs2.sh
    delivers real volume, the proxy is the hourly depth ratio (sum of
    sell+bid counts) from the local hour buckets; a value far above 1.0
    flags a sudden liquidity surge (e.g. the 2025-07 trade-hold and
    2025-10 contract-change regime breaks showed as one-day -60% moves).
    When the proxy is unavailable the factor is honestly skipped and the
    weights stay at the original four-factor allocation; when present the
    five weights are rescaled proportionally (0.28/0.20/0.20/0.12/0.20)
    so Σ stays exactly 1.0.

    Heuristic disclosure (METHODOLOGY.md §2): the four divisors (3/6/10/5),
    the ±1 clip and all weights above are hand-set experience constants —
    never fit by estimation.  The score is a momentum composite for
    bucketing/display, not a structural market-temperature estimate.
    """

    index = _finite_positive(current_index)
    timestamp = _parse_timestamp(current_timestamp)
    current_time = time.time() if now is None else now
    if (
        index is None
        or timestamp is None
        or timestamp > current_time + _FUTURE_TOLERANCE_SECONDS
        or current_time - timestamp > _BROAD_MAX_AGE_SECONDS
    ):
        return TemperatureUnavailable()

    try:
        current_date = datetime.fromtimestamp(timestamp, timezone).date()
    except (OSError, OverflowError, ValueError):
        return TemperatureUnavailable()

    required_dates = [current_date - timedelta(days=days) for days in range(1, 8)]
    closes: list[float] = []
    for required_date in required_dates:
        close = _finite_positive(daily_closes.get(required_date))
        if close is None:
            return TemperatureUnavailable()
        closes.append(close)

    change_1d = (index / closes[0] - 1) * 100
    change_3d = (index / closes[2] - 1) * 100
    change_7d = (index / closes[6] - 1) * 100
    average_7d = (index + sum(closes[:6])) / 7
    deviation_7d = (index / average_7d - 1) * 100
    factors = (
        _clip(change_1d / 3),
        _clip(change_3d / 6),
        _clip(change_7d / 10),
        _clip(deviation_7d / 5),
    )
    if liquidity_ratio is None:
        weights: tuple[float, ...] = _FOUR_FACTOR_WEIGHTS
        contributions: tuple[float, ...] = (
            factors[0] * weights[0],
            factors[1] * weights[1],
            factors[2] * weights[2],
            factors[3] * weights[3],
        )
        liquidity_note = None
    else:
        # 流动性突变：挂单深度骤变（替代口径）→ 全市场成交骤变的代理
        # （cs2.sh 真成交量接入后升级）。比例 >1 表示深度放大，<1 表示
        # 萎缩；极端值（≥2 或 ≤0.5）视为结构性事件信号。
        liquidity_factor = _clip((liquidity_ratio - 1.0) / 0.5)
        weights = _FIVE_FACTOR_WEIGHTS
        contributions = (
            factors[0] * weights[0],
            factors[1] * weights[1],
            factors[2] * weights[2],
            factors[3] * weights[3],
            liquidity_factor * weights[4],
        )
        liquidity_note = f"流动性突变 {liquidity_ratio:.2f}×（挂单深度代理，真实成交量待接）"
    raw_score = min(100.0, max(-100.0, sum(contributions) * 100))
    score = _round_score(raw_score)

    current_state, temperature_state = classify_temperature(score)
    return MarketTemperature(
        score=score,
        change_1d=change_1d,
        change_3d=change_3d,
        change_7d=change_7d,
        deviation_7d=deviation_7d,
        current_state=current_state,
        detail=_temperature_detail(change_1d, change_3d, change_7d, contributions),
        temperature_state=temperature_state,
        liquidity_ratio=liquidity_ratio,
        liquidity_note=liquidity_note,
    )


async def fetch_daily_chart() -> Mapping[str, Any]:
    """Fetch the fixed public daily-index chart within a short deadline."""

    return await request_json(
        "GET",
        _DAILY_CHART_URL,
        attempts=2,
        timeout=httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=3.0),
        total_timeout=8.0,
    )


async def _liquidity_depth_ratio() -> float | None:
    """Whole-market depth ratio proxy for the liquidity-surge factor.

    The proxy is the latest hourly bucket's summed sell+bid depth divided
    by the previous hour's sum (read from the local quote-snapshot store,
    no network).  Returns None when the buckets are missing, stale or the
    previous depth is zero — the factor is then honestly skipped.
    """

    try:
        from .market_history import market_history

        store = market_history
        await store.initialize()
    except Exception as exc:
        logger.warning("Liquidity depth store unavailable error=%s", type(exc).__name__)
        return None

    def read() -> float | None:
        connection = store._connect()
        try:
            latest_row = connection.execute("SELECT MAX(bucket_at) FROM quote_snapshots").fetchone()
            latest = int(latest_row[0]) if latest_row and latest_row[0] is not None else None
            if latest is None:
                return None
            current_bucket = int(time.time()) // 3600 * 3600
            if latest > current_bucket or current_bucket - latest > 3600:
                return None
            previous = latest - 3600
            rows = connection.execute(
                """
                SELECT bucket_at, SUM(COALESCE(sell_count, 0)) + SUM(COALESCE(bid_count, 0))
                FROM quote_snapshots
                WHERE bucket_at IN (?, ?)
                GROUP BY bucket_at
                """,
                (latest, previous),
            ).fetchall()
        finally:
            connection.close()
        depths = {int(row[0]): float(row[1]) for row in rows}
        current = depths.get(latest)
        prior = depths.get(previous)
        if current is None or prior is None or prior <= 0 or current <= 0:
            return None
        return current / prior

    try:
        return await asyncio.to_thread(read)
    except Exception as exc:
        logger.warning("Liquidity depth read failed error=%s", type(exc).__name__)
        return None


class MarketTemperatureService:
    """Thirty-minute daily-history cache with single-flight stale fallback."""

    def __init__(
        self,
        *,
        fetcher: DailyChartFetcher = fetch_daily_chart,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        fresh_seconds: float = _FRESH_CACHE_SECONDS,
        stale_seconds: float = _STALE_CACHE_SECONDS,
    ) -> None:
        if not 0 < fresh_seconds <= stale_seconds:
            raise ValueError("temperature cache durations are invalid")
        self._fetcher = fetcher
        self._clock = clock
        self._wall_clock = wall_clock
        self._fresh_seconds = fresh_seconds
        self._stale_seconds = stale_seconds
        self._cache: _HistoryCache | None = None
        self._inflight: asyncio.Task[_HistoryCache | None] | None = None
        self._lock = asyncio.Lock()

    async def _refresh(self) -> _HistoryCache | None:
        try:
            payload = await self._fetcher()
            closes = parse_daily_closes(payload)
            rows = payload.get("data")
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise ValueError("daily chart data is unavailable")
            latest_timestamp = _parse_timestamp(rows[-1][0])
            if latest_timestamp is None:
                raise ValueError("daily chart timestamp is invalid")
        except Exception as exc:
            logger.warning("Market temperature history refresh failed error=%s", type(exc).__name__)
            return None
        cache = _HistoryCache(dict(closes), latest_timestamp, self._clock())
        async with self._lock:
            self._cache = cache
        return cache

    async def _get_history(self, *, require_current: bool = False) -> _HistoryCache | None:
        async with self._lock:
            now = self._clock()
            cache_is_current = self._cache is not None and _broad_timestamp_is_fresh(
                self._cache.latest_timestamp,
                self._wall_clock(),
            )
            if (
                self._cache is not None
                and now - self._cache.stored_at < self._fresh_seconds
                and (not require_current or cache_is_current)
            ):
                return self._cache
            if self._inflight is None or self._inflight.done():
                self._inflight = asyncio.create_task(self._refresh())
            task = self._inflight

        refreshed = await asyncio.shield(task)
        if refreshed is not None:
            return refreshed

        async with self._lock:
            now = self._clock()
            if self._cache is not None and now - self._cache.stored_at <= self._stale_seconds:
                return self._cache
        return None

    async def broad_market_fallback(self) -> dict[str, Any]:
        """Build a fresh, minimal broad response from the validated daily chart."""

        history = await self._get_history(require_current=True)
        if history is None:
            return {"success": False, "errorMsg": "大盘备用数据暂不可用"}
        now = self._wall_clock()
        timestamp = history.latest_timestamp
        if not _broad_timestamp_is_fresh(timestamp, now):
            return {"success": False, "errorMsg": "大盘备用数据已过期"}
        try:
            current_date = datetime.fromtimestamp(timestamp, _MARKET_TIMEZONE).date()
            wall_date = datetime.fromtimestamp(now, _MARKET_TIMEZONE).date()
        except (OSError, OverflowError, ValueError):
            return {"success": False, "errorMsg": "大盘备用数据时间无效"}
        if current_date != wall_date:
            return {"success": False, "errorMsg": "大盘备用数据日期无效"}

        current_index = _finite_positive(history.closes.get(current_date))
        previous_close = _finite_positive(history.closes.get(current_date - timedelta(days=1)))
        if current_index is None or previous_close is None:
            return {"success": False, "errorMsg": "大盘备用数据样本不足"}
        difference = current_index - previous_close
        return {
            "success": True,
            "data": {
                "broadMarketIndex": current_index,
                "diffYesterday": difference,
                "diffYesterdayRatio": difference / previous_close * 100,
                "historyMarketIndexList": [],
                "updateTime": timestamp,
            },
        }

    async def evaluate(self, broad_response: Mapping[str, Any]) -> TemperatureResult:
        """Return a temperature for one validated broad response."""

        market = broad_response.get("data") if broad_response.get("success") else None
        if not isinstance(market, Mapping):
            return TemperatureUnavailable()
        current_index = _finite_positive(market.get("broadMarketIndex"))
        current_timestamp = _parse_timestamp(market.get("updateTime"))
        now = self._wall_clock()
        if (
            current_index is None
            or current_timestamp is None
            or current_timestamp > now + _FUTURE_TOLERANCE_SECONDS
            or now - current_timestamp > _BROAD_MAX_AGE_SECONDS
        ):
            return TemperatureUnavailable()
        history = await self._get_history()
        if history is None:
            return TemperatureUnavailable()
        liquidity_ratio = await _liquidity_depth_ratio()
        return calculate_market_temperature(
            current_index=current_index,
            current_timestamp=current_timestamp,
            daily_closes=history.closes,
            now=now,
            liquidity_ratio=liquidity_ratio,
        )

    async def close(self) -> None:
        """Cancel an in-progress refresh and release cached history."""

        async with self._lock:
            task = self._inflight
            self._inflight = None
            self._cache = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


market_temperature_service = MarketTemperatureService()


async def get_market_temperature(broad_response: Mapping[str, Any]) -> TemperatureResult:
    return await market_temperature_service.evaluate(broad_response)


async def get_fallback_broad_market() -> dict[str, Any]:
    return await market_temperature_service.broad_market_fallback()


async def close_market_temperature_service() -> None:
    await market_temperature_service.close()


__all__ = [
    "MarketTemperature",
    "MarketTemperatureService",
    "TemperatureResult",
    "TemperatureUnavailable",
    "calculate_market_temperature",
    "classify_temperature",
    "close_market_temperature_service",
    "fetch_daily_chart",
    "get_fallback_broad_market",
    "get_market_temperature",
    "market_temperature_service",
    "parse_daily_closes",
]

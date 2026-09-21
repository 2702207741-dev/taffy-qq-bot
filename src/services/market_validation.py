"""Shared fail-closed validation for market quotes and broad-index snapshots.

Every consumer must use this module before presenting, comparing, persisting,
or acting on market facts.  A successful HTTP response is not evidence that
the contained facts are current.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

PRICE_MAX_AGE_SECONDS = 10 * 60
BROAD_MAX_AGE_SECONDS = 15 * 60
FUTURE_TOLERANCE_SECONDS = 5 * 60
_TIMESTAMP_KEYS = ("updateTime", "updatedAt", "dataTime", "timestamp", "time")


class MarketDataError(ValueError):
    """A market response cannot safely be used as a current fact."""

    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class ValidatedQuote:
    platform: str
    sell_price: float
    timestamp: float
    sell_count: int | None = None
    bid_price: float | None = None
    bid_count: int | None = None


@dataclass(frozen=True, slots=True)
class BroadMarketSnapshot:
    index: float
    change_percent: float
    timestamp: float
    change_value: float | None = None
    payload: Mapping[str, Any] | None = None


def parse_timestamp(value: object) -> float | None:
    """Parse seconds, milliseconds, numeric strings, or ISO-8601 timestamps."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        try:
            result = float(stripped)
        except ValueError:
            try:
                result = datetime.fromisoformat(stripped.replace("Z", "+00:00")).timestamp()
            except (ValueError, OSError, OverflowError):
                return None
    else:
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    if result > 10_000_000_000:
        result /= 1000
    return result


def mapping_timestamp(data: Mapping[str, Any]) -> float | None:
    """Return the oldest valid timestamp directly owned by one mapping."""

    candidates = [
        timestamp
        for key in _TIMESTAMP_KEYS
        if (timestamp := parse_timestamp(data.get(key))) is not None
    ]
    return min(candidates) if candidates else None


def response_data_timestamp(data: Mapping[str, Any]) -> float | None:
    """Return the oldest timestamp applying anywhere in a market response."""

    candidates: list[float] = []

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            if timestamp := mapping_timestamp(value):
                candidates.append(timestamp)

    collect(data)
    payload = data.get("data")
    collect(payload)
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            collect(item)
    return min(candidates) if candidates else None


def format_data_time(timestamp: float) -> str:
    """Render one data timestamp in the market timezone (Asia/Shanghai)."""

    return datetime.fromtimestamp(timestamp, tz=_MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S")


def freshness_error(
    timestamp: float | None,
    *,
    now: float,
    max_age: float,
    subject: str,
) -> str | None:
    if timestamp is None:
        return f"⚠️ {subject}缺少可核验的数据时间，本次拒绝展示。"
    if timestamp > now + FUTURE_TOLERANCE_SECONDS:
        return f"⚠️ {subject}时间异常（{format_data_time(timestamp)}），本次拒绝展示。"
    if now - timestamp > max_age:
        return f"⚠️ {subject}已过期（数据时间: {format_data_time(timestamp)}），本次拒绝展示。"
    return None


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def validate_quote_response(
    response: Mapping[str, Any],
    *,
    now: float | None = None,
) -> tuple[ValidatedQuote, ...]:
    """Return only fresh positive quote rows, or fail closed with one reason."""

    if not response.get("success"):
        raise MarketDataError("quote_request_failed", "⚠️ 市场报价获取失败，请稍后重试。")
    items = response.get("data")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        raise MarketDataError("quote_rows_missing", "⚠️ 上游市场未返回有效报价。")

    current = time.time() if now is None else now
    container_timestamp = mapping_timestamp(response)
    valid: list[ValidatedQuote] = []
    time_errors: list[tuple[float | None, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        sell_price = _finite_positive(item.get("sellPrice"))
        if sell_price is None:
            continue
        platform = str(item.get("platform") or "未知")
        timestamp = mapping_timestamp(item) or container_timestamp
        if error := freshness_error(
            timestamp,
            now=current,
            max_age=PRICE_MAX_AGE_SECONDS,
            subject="市场实时报价",
        ):
            time_errors.append((timestamp, error))
            continue
        assert timestamp is not None

        sell_count = item.get("sellCount")
        if isinstance(sell_count, bool) or not isinstance(sell_count, int) or sell_count < 0:
            sell_count = None
        raw_bid = _finite_positive(item.get("biddingPrice"))
        bid_price = raw_bid if raw_bid is not None and raw_bid <= sell_price * 1.05 else None
        bid_count = item.get("biddingCount")
        if isinstance(bid_count, bool) or not isinstance(bid_count, int) or bid_count <= 0:
            bid_count = None
        if bid_price is None:
            bid_count = None
        valid.append(
            ValidatedQuote(
                platform=platform,
                sell_price=sell_price,
                timestamp=timestamp,
                sell_count=sell_count,
                bid_price=bid_price,
                bid_count=bid_count,
            )
        )

    if valid:
        return tuple(valid)
    if time_errors:
        stale_timestamps = [
            timestamp
            for timestamp, _ in time_errors
            if timestamp is not None and current - timestamp > PRICE_MAX_AGE_SECONDS
        ]
        if len(stale_timestamps) == len(time_errors):
            latest = max(stale_timestamps)
            raise MarketDataError(
                "quote_stale",
                "⚠️ 各平台实时报价均已过期"
                f"（最新数据时间: {format_data_time(latest)}），本次不展示即时价格。",
            )
        raise MarketDataError("quote_stale", time_errors[0][1])
    raise MarketDataError("quote_values_invalid", "⚠️ 上游市场未返回可核验的正数报价。")


def preferred_quote(quotes: Sequence[ValidatedQuote]) -> ValidatedQuote:
    """Pick one deterministic alert price after all rows passed validation."""

    for platform in ("YOUPIN", "BUFF"):
        if quote := next((item for item in quotes if item.platform == platform), None):
            return quote
    if not quotes:
        raise MarketDataError("quote_rows_missing", "⚠️ 上游市场未返回有效报价。")
    return min(quotes, key=lambda item: item.sell_price)


def validate_broad_market_response(
    response: Mapping[str, Any],
    *,
    now: float | None = None,
) -> BroadMarketSnapshot:
    """Validate the shared timestamp and numeric broad-market facts."""

    if not response.get("success") or not isinstance(response.get("data"), Mapping):
        raise MarketDataError("broad_request_failed", "⚠️ 大盘数据获取失败，请稍后重试。")
    payload = response["data"]
    timestamp = parse_timestamp(payload.get("updateTime"))
    current = time.time() if now is None else now
    if error := freshness_error(
        timestamp,
        now=current,
        max_age=BROAD_MAX_AGE_SECONDS,
        subject="大盘数据",
    ):
        raise MarketDataError("broad_stale", error)

    index = _finite_positive(payload.get("broadMarketIndex"))
    change_percent = _finite_number(payload.get("diffYesterdayRatio"))
    change_value = _finite_number(payload.get("diffYesterday"))
    if index is None or change_percent is None:
        raise MarketDataError("broad_values_invalid", "⚠️ 大盘字段无效，本次拒绝展示。")
    assert timestamp is not None
    return BroadMarketSnapshot(
        index=index,
        change_percent=change_percent,
        timestamp=timestamp,
        change_value=change_value,
        payload=payload,
    )


__all__ = [
    "BROAD_MAX_AGE_SECONDS",
    "PRICE_MAX_AGE_SECONDS",
    "BroadMarketSnapshot",
    "MarketDataError",
    "ValidatedQuote",
    "format_data_time",
    "freshness_error",
    "mapping_timestamp",
    "parse_timestamp",
    "preferred_quote",
    "response_data_timestamp",
    "validate_broad_market_response",
    "validate_quote_response",
]

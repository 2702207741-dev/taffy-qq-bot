"""Single source of truth for market-timezone handling.

The whole project treats Asia/Shanghai (+08:00, DST-free since 1991) as the
market and user-facing timezone.  Windows images may not bundle the IANA
database, so every conversion goes through this module's portable fallbacks
instead of constructing ``ZoneInfo`` objects directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MARKET_TIMEZONE_NAME = "Asia/Shanghai"

# Shanghai has had a stable +08:00 offset since 1991, so the fallback is safe.
_MARKET_UTC_OFFSET = timedelta(hours=8)


def configured_timezone(timezone_name: str = MARKET_TIMEZONE_NAME) -> tzinfo:
    """Load an IANA zone, with portable fallbacks for repository defaults."""

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == MARKET_TIMEZONE_NAME:
            return timezone(_MARKET_UTC_OFFSET, name=MARKET_TIMEZONE_NAME)
        if timezone_name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        raise


def to_market_tz(
    now: datetime | None = None,
    timezone_name: str = MARKET_TIMEZONE_NAME,
) -> datetime:
    """Convert *now* (or the current time) into the configured local timezone."""

    configured = configured_timezone(timezone_name)
    if now is None:
        return datetime.now(configured)
    if now.tzinfo is None:
        # Injected naive clocks are wall-clock values in the configured zone.
        return now.replace(tzinfo=configured)
    return now.astimezone(configured)


def market_now(timezone_name: str = MARKET_TIMEZONE_NAME) -> datetime:
    """Current wall-clock time in the market timezone."""

    return to_market_tz(None, timezone_name)


__all__ = [
    "MARKET_TIMEZONE_NAME",
    "configured_timezone",
    "market_now",
    "to_market_tz",
]

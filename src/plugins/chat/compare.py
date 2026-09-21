"""Cross-platform price comparison backed by SteamDT plus CSQAQ.

SteamDT stays the real-time source (10-minute freshness window); CSQAQ adds
the platforms SteamDT does not cover (IGXE/ECO), rent yields and the global
supply count.  Every number shown is validated first; any missing side is
reported as unavailable instead of being guessed around.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.services.csqaq import CsqaqGoodDetail, csqaq_service
from src.services.csqaq_history import BASIS_COMPARE, csqaq_history_store
from src.services.market_validation import (
    MarketDataError,
    ValidatedQuote,
    format_data_time,
    validate_quote_response,
)

logger = logging.getLogger("taffy.plugins.chat.compare")

#: CSQAQ platform keys → display names (SteamDT codes live in market.py).
CSQAQ_PLATFORM_NAMES = {
    "steam": "Steam",
    "buff": "BUFF",
    "yyyp": "悠悠",
    "c5": "C5",
    "igxe": "IGXE",
    "eco": "ECO",
}

#: SteamDT platform codes → CSQAQ platform keys; unmapped codes share nothing.
_STEAMDT_TO_CSQAQ = {
    "BUFF": "buff",
    "YOUPIN": "yyyp",
    "C5": "c5",
    "STEAM": "steam",
}

_COMMA_PLATFORMS = ("buff", "yyyp", "c5", "igxe", "eco", "steam")
_COMPARISON_NOTE = "ℹ️ 两源采集时间可能不同步，价差仅供参考。"
_UNCONFIGURED_REPLY = "⚠️ 比价数据源未配置（CSQAQ_API_KEY），本次无法比价。"


async def fetch_csqaq_side(query: str, market_name: str) -> dict[str, Any]:
    """Resolve-and-verify one item on the CSQAQ side; never raises."""

    try:
        return await csqaq_service.verified_detail(query, market_hash_name=market_name)
    except Exception as exc:
        logger.warning("CSQAQ side fetch failed error=%s", type(exc).__name__)
        return {"success": False, "errorMsg": "CSQAQ 数据暂时不可用"}


def _quote_lines(quotes: tuple[ValidatedQuote, ...], *, names: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for quote in sorted(quotes, key=lambda item: item.sell_price):
        label = names.get(quote.platform, quote.platform)
        sell_count = "--" if quote.sell_count is None else str(quote.sell_count)
        if quote.bid_price is not None:
            bid = f"收 {quote.bid_price:.2f}" + (f" ×{quote.bid_count}" if quote.bid_count else "")
        else:
            bid = "收 暂无"
        lines.append(f"  {label:5s} 卖 {quote.sell_price:.2f} ×{sell_count} ｜ {bid}")
    return lines


def _quote_part(price: float | None, num: int | None, verb: str) -> str:
    if price is None:
        return f"{verb} —"
    if num is None:
        return f"{verb} {price:.2f}"
    return f"{verb} {price:.2f} ×{num}"


def _csqaq_quote_lines(detail: CsqaqGoodDetail) -> list[str]:
    by_platform = {quote.platform: quote for quote in detail.quotes}
    lines: list[str] = []
    for key in _COMMA_PLATFORMS:
        quote = by_platform.get(key)
        if quote is None:
            continue
        label = CSQAQ_PLATFORM_NAMES[key]
        sell = _quote_part(quote.sell_price, quote.sell_num, "在售")
        buy = _quote_part(quote.buy_price, quote.buy_num, "求购")
        lines.append(f"  {label:5s} {sell} ｜ {buy}")
    return lines


def _spread_lines(quotes: tuple[ValidatedQuote, ...], detail: CsqaqGoodDetail) -> list[str]:
    csqaq_by_platform = {quote.platform: quote for quote in detail.quotes}
    spreads: list[str] = []
    for quote in quotes:
        key = _STEAMDT_TO_CSQAQ.get(quote.platform)
        if key is None:
            continue
        other = csqaq_by_platform.get(key)
        if other is None or other.sell_price is None:
            continue
        diff = other.sell_price - quote.sell_price
        percent = diff / quote.sell_price * 100
        spreads.append(f"{CSQAQ_PLATFORM_NAMES[key]} {diff:+.2f}（{percent:+.2f}%）")
    if not spreads:
        return []
    return ["同平台在售价差（CSQAQ − SteamDT）:", "  " + " ｜ ".join(spreads)]


def _humanize_age(seconds: float) -> str:
    if seconds < 90 * 60:
        return f"{max(1, round(seconds / 60))} 分钟前"
    if seconds < 48 * 3600:
        return f"{round(seconds / 3600)} 小时前"
    return f"{round(seconds / 86400)} 天前"


def csqaq_supply_line(detail: CsqaqGoodDetail, *, now: float | None = None) -> str | None:
    """One honest supply line, or None when CSQAQ has no count."""

    if detail.supply_count is None:
        return None
    suffix = "（CSQAQ）"
    if detail.data_age_seconds is not None:
        suffix = f"（CSQAQ，数据 {_humanize_age(detail.data_age_seconds)}）"
    return f"存世量：{detail.supply_count:,} 件{suffix}"


def csqaq_rent_line(detail: CsqaqGoodDetail) -> str | None:
    """One rent-reference line, or None when CSQAQ has no rent fields."""

    rent = detail.rent
    if rent is None:
        return None
    parts: list[str] = []
    if rent.short_price is not None:
        parts.append(f"日租 ¥{rent.short_price:g}")
    if rent.short_annual_yield is not None:
        parts.append(f"短租年化 {rent.short_annual_yield:g}%")
    if rent.long_annual_yield is not None:
        parts.append(f"长租年化 {rent.long_annual_yield:g}%")
    if not parts:
        return None
    tail = f"（在租 {rent.leasing_num}）" if rent.leasing_num is not None else ""
    return "租金参考：" + " ｜ ".join(parts) + tail


def _detail_payload(detail: CsqaqGoodDetail) -> dict[str, Any]:
    """Typed CSQAQ detail → JSON-safe projection for the desktop shell."""

    return {
        "good_id": detail.good_id,
        "name_cn": detail.name_cn,
        "market_hash_name": detail.market_hash_name,
        "supply_count": detail.supply_count,
        "data_age_seconds": detail.data_age_seconds,
        "quotes": [
            {
                "platform": quote.platform,
                "sell_price": quote.sell_price,
                "sell_num": quote.sell_num,
                "buy_price": quote.buy_price,
                "buy_num": quote.buy_num,
            }
            for quote in detail.quotes
        ],
        "rent": (
            {
                "short_price": detail.rent.short_price,
                "long_price": detail.rent.long_price,
                "short_annual_yield": detail.rent.short_annual_yield,
                "long_annual_yield": detail.rent.long_annual_yield,
                "leasing_num": detail.rent.leasing_num,
            }
            if detail.rent is not None
            else None
        ),
        "price_change_7d": detail.price_change_7d,
        "price_change_30d": detail.price_change_30d,
    }


async def build_compare_payload(query: str, market_name: str) -> dict[str, Any]:
    """Structured 比价 data for the desktop shell (same gates as the command)."""

    csqaq_side = await fetch_csqaq_side(query, market_name)
    from src.services.market import st_get

    steamdt = await st_get("/open/cs2/v1/price/single", {"marketHashName": market_name})
    quotes: list[dict[str, Any]] = []
    steamdt_error: str | None = None
    collected_at: float | None = None
    try:
        validated = validate_quote_response(steamdt)
        quotes = [
            {
                "platform": quote.platform,
                "sell_price": quote.sell_price,
                "sell_count": quote.sell_count,
                "bid_price": quote.bid_price,
                "bid_count": quote.bid_count,
                "timestamp": quote.timestamp,
            }
            for quote in sorted(validated, key=lambda item: item.sell_price)
        ]
        if quotes:
            collected_at = min(quote["timestamp"] for quote in quotes)
    except MarketDataError as exc:
        steamdt_error = exc.user_message

    detail: CsqaqGoodDetail | None = csqaq_side.get("detail")
    csqaq_payload: dict[str, Any] | None = _detail_payload(detail) if detail is not None else None

    csqaq_by_platform = {
        quote["platform"]: quote["sell_price"]
        for quote in (csqaq_payload or {}).get("quotes", [])
        if quote["sell_price"] is not None
    }
    spreads = []
    for entry in quotes:
        key = _STEAMDT_TO_CSQAQ.get(entry["platform"])
        other = csqaq_by_platform.get(key) if key else None
        if other is None:
            continue
        diff = other - entry["sell_price"]
        spreads.append(
            {
                "platform": key,
                "csqaq": other,
                "steamdt": entry["sell_price"],
                "diff": round(diff, 2),
                "diff_percent": round(diff / entry["sell_price"] * 100, 2),
            }
        )

    return {
        "market_hash_name": market_name,
        "steamdt": {"quotes": quotes, "error": steamdt_error, "collected_at": collected_at},
        "csqaq": csqaq_payload,
        "csqaq_error": None if csqaq_payload else csqaq_side.get("errorMsg"),
        "csqaq_unconfigured": bool(csqaq_side.get("unconfigured")),
        "spreads": spreads,
    }


def format_price_comparison(
    name: str,
    steamdt_response: dict[str, Any],
    csqaq_response: dict[str, Any],
    *,
    now: float | None = None,
    hoard_line: str | None = None,
    trend_line: str | None = None,
) -> str:
    """Render the ``比价`` reply; each side reports unavailability honestly."""

    if not csqaq_response.get("success") and csqaq_response.get("unconfigured"):
        return _UNCONFIGURED_REPLY

    current = time.time() if now is None else now
    try:
        quotes = validate_quote_response(steamdt_response, now=current)
        steamdt_error: str | None = None
    except MarketDataError as exc:
        quotes = ()
        steamdt_error = exc.user_message

    detail: CsqaqGoodDetail | None = csqaq_response.get("detail")
    csqaq_error = None if detail is not None else csqaq_response.get("errorMsg")

    if quotes == () and detail is None:
        reason = steamdt_error or csqaq_error or "数据源暂时不可用"
        return f"⚠️ 比价数据这次拿不到：{reason.removeprefix('⚠️ ')}请稍后重试。"

    lines: list[str] = [f"{name} 比价", "──────────"]
    if quotes:
        stamp = format_data_time(min(quote.timestamp for quote in quotes))
        lines.append(f"SteamDT 实时（{stamp} 校验）")
        lines.extend(_quote_lines(quotes, names=_STEAMDT_DISPLAY_NAMES))
    elif steamdt_error:
        lines.append(f"SteamDT 侧：{steamdt_error.removeprefix('⚠️ ')}")

    lines.append("──────────")
    if detail is not None:
        if detail.data_age_seconds is not None:
            stamp = format_data_time(current - detail.data_age_seconds)
            lines.append(f"CSQAQ 快照（{stamp} 采集）")
        else:
            lines.append("CSQAQ 快照（数据时间未知）")
        lines.extend(_csqaq_quote_lines(detail))
        if detail.supply_count is not None:
            lines.append(f"  存世量 {detail.supply_count:,} 件")
        rent = csqaq_rent_line(detail)
        if rent is not None:
            lines.append("  " + rent.removeprefix("租金参考："))
        if quotes:
            lines.append("──────────")
            lines.extend(_spread_lines(quotes, detail))
    elif csqaq_error:
        lines.append(f"CSQAQ 侧：{csqaq_error}")

    if hoard_line:
        lines.append(hoard_line)

    if trend_line:
        lines.append(trend_line)

    lines.append(_COMPARISON_NOTE)
    return "\n".join(lines)


async def record_and_trend_line(
    market_name: str,
    steamdt_response: dict[str, Any],
    csqaq_response: dict[str, Any],
    *,
    now: float | None = None,
) -> str | None:
    """Record today's CSQAQ snapshot, then return the spread-trend line.

    Spread basis here is max CSQAQ buy ÷ cheapest SteamDT sell (both
    "now", same async caveat as the printed spreads).  ``None`` on any
    gap — recording and display are both fail-open.
    """

    try:
        detail: CsqaqGoodDetail | None = csqaq_response.get("detail")
        current = time.time() if now is None else now
        spread: float | None = None
        rent: float | None = None
        sell_total: int | None = None
        if detail is not None:
            try:
                quotes = validate_quote_response(steamdt_response, now=current)
            except MarketDataError:
                quotes = ()
            bids = [quote.buy_price for quote in detail.quotes if quote.buy_price is not None]
            if quotes and bids:
                ref = min(quote.sell_price for quote in quotes)
                if ref > 0:
                    spread = max(bids) / ref
            counts = [quote.sell_num for quote in detail.quotes if quote.sell_num]
            sell_total = sum(counts) if counts else None
            if detail.rent is not None:
                rent = (
                    detail.rent.long_annual_yield
                    if detail.rent.long_annual_yield is not None
                    else detail.rent.short_annual_yield
                )
            await csqaq_history_store.record_snapshot(
                detail.good_id,
                market_name,
                spread_ratio=spread,
                rent_annual=rent,
                sell_total=sell_total,
                basis=BASIS_COMPARE,
            )
        return await csqaq_history_store.render_trend_line(market_name)
    except Exception as exc:
        logger.warning("CSQAQ trend line failed error=%s", type(exc).__name__)
        return None


async def _hoard_reference_line(market_name: str) -> str | None:
    """One honest 挂刀参考 line; None when the item is untracked."""

    try:
        from src.services.hoard_index import hoard_index_store

        ratio = await hoard_index_store.item_ratio(market_name)
    except Exception as exc:
        logger.warning("Hoard reference line failed error=%s", type(exc).__name__)
        return None
    if ratio is None:
        return None
    return f"挂刀参考：Steam 比例 {ratio:.2f}（高分位适合出 Steam，低分位适合留平台）"


_STEAMDT_DISPLAY_NAMES = {"BUFF": "BUFF", "YOUPIN": "悠悠", "C5": "C5", "STEAM": "Steam"}


__all__ = [
    "build_compare_payload",
    "csqaq_rent_line",
    "csqaq_supply_line",
    "fetch_csqaq_side",
    "format_price_comparison",
    "record_and_trend_line",
]

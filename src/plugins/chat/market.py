"""Pure market-name parsing and response formatting for the chat plugin."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger

from src.services.config import steamdt_cache_path
from src.services.market_temperature import MarketTemperature, TemperatureResult
from src.services.market_validation import (
    PRICE_MAX_AGE_SECONDS,
    MarketDataError,
    format_data_time,
    freshness_error,
    validate_broad_market_response,
    validate_quote_response,
)
from src.services.market_validation import (
    mapping_timestamp as _mapping_timestamp,
)
from src.services.market_validation import (
    parse_timestamp as _parse_timestamp,
)

from .skin_data import AMBIGUOUS_SKIN_ALIASES, SKIN_MAP

if TYPE_CHECKING:
    from src.services.market_catalog import CatalogEntry

KLINE_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
VOLUME_MAX_AGE_SECONDS = 10 * 60
_BROAD_MARKET_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

WEAR_MAP = {
    "崭新出厂": "Factory New",
    "崭新": "Factory New",
    "fn": "Factory New",
    "略有磨损": "Minimal Wear",
    "略磨": "Minimal Wear",
    "mw": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "久经": "Field-Tested",
    "ft": "Field-Tested",
    "破损不堪": "Well-Worn",
    "破损": "Well-Worn",
    "ww": "Well-Worn",
    "战痕累累": "Battle-Scarred",
    "战痕": "Battle-Scarred",
    "bs": "Battle-Scarred",
}

_WEAR_VALUES = frozenset(WEAR_MAP.values())

PLATFORM_NAMES = {
    "BUFF": "BUFF",
    "YOUPIN": "悠悠",
    "C5": "C5",
    "STEAM": "Steam",
    "HALOSKINS": "HALO",
}

#: Seven-day average platforms in stable display order (SteamDT avg
#: endpoint may return more: DMARKET/CSMONEY/SKINPORT/WAXPEER/IGXE/V5ITEM
#: and future codes render raw rather than being dropped silently).
_AVG_PLATFORM_ORDER = (
    "BUFF",
    "YOUPIN",
    "C5",
    "STEAM",
    "DMARKET",
    "CSMONEY",
    "SKINPORT",
    "WAXPEER",
    "HALOSKINS",
    "IGXE",
    "V5ITEM",
)


def _normalise_alias(value: str) -> str:
    return re.sub(r"[\s\-_·•]", "", value.lower())


def _find_wear(query: str) -> str | None:
    lowered = query.lower()
    normalised = _normalise_alias(query)
    for alias, wear in WEAR_MAP.items():
        if alias.isascii() and alias.isalpha():
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
                return wear
        elif _normalise_alias(alias) in normalised:
            return wear
    return None


def _full_market_name(query: str) -> str | None:
    match = re.fullmatch(
        r"\s*(.{1,64}?)\s*\|\s*(.{1,120}?)\s*\(([^()]{2,32})\)\s*",
        query,
    )
    if match is None or match.group(3) not in _WEAR_VALUES:
        return None
    return f"{match.group(1).strip()} | {match.group(2).strip()} ({match.group(3)})"


def _best_skin_alias(query: str) -> tuple[str, tuple[str, str]] | None:
    normalised = _normalise_alias(query)
    best: tuple[str, tuple[str, str]] | None = None
    best_length = 0
    for alias, market_parts in SKIN_MAP.items():
        clean_alias = _normalise_alias(alias)
        if clean_alias in normalised and len(clean_alias) > best_length:
            best = (alias, market_parts)
            best_length = len(clean_alias)
    return best


_CATALOG_SPECIAL_TERMS = (
    "stattrak™",
    "stattrak",
    "暗金",
    "souvenir",
    "纪念品",
)


def _catalog_normalise(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


_CATALOG_WEAPON_PREFIX_ALIASES = (
    ("deserteagle", "deserteagle"),
    ("m4a1s", "m4a1s"),
    ("m4a1", "m4a1s"),
    ("deagle", "deserteagle"),
    ("沙鹰", "deserteagle"),
    ("ak47", "ak47"),
    ("usps", "usps"),
    ("usp", "usps"),
    ("m4a4", "m4a4"),
    ("awp", "awp"),
    ("a1", "m4a1s"),
    ("a4", "m4a4"),
    ("ak", "ak47"),
)

_CATALOG_SEARCH_PREFIX_ALIASES = (
    ("m4a1-s", "M4A1-S"),
    ("m4a1s", "M4A1-S"),
    ("m4a1", "M4A1-S"),
    ("usp-s", "USP-S"),
    ("usps", "USP-S"),
    ("m4a4", "M4A4"),
    ("deagle", "沙鹰"),
    ("ak-47", "AK-47"),
    ("ak47", "AK-47"),
    ("usp", "USP-S"),
    ("awp", "AWP"),
    ("沙鹰", "沙鹰"),
    ("a1", "M4A1-S"),
    ("a4", "M4A4"),
    ("ak", "AK-47"),
)


def _canonicalise_catalog_weapon(value: str) -> str:
    for alias, canonical in _CATALOG_WEAPON_PREFIX_ALIASES:
        if value.startswith(alias):
            return f"{canonical}{value[len(alias) :]}"
    return value


def _without_query_catalog_terms(query: str) -> str:
    stripped = query.casefold()
    for alias in sorted(WEAR_MAP, key=len, reverse=True):
        if alias.isascii() and alias.isalpha():
            stripped = re.sub(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", " ", stripped)
        else:
            stripped = stripped.replace(alias.casefold(), " ")
    for term in _CATALOG_SPECIAL_TERMS:
        stripped = stripped.replace(term, " ")
    return re.sub(r"\s+", " ", stripped).strip()


def _split_catalog_search_weapon(value: str) -> tuple[str, str] | None:
    folded = value.casefold().strip()
    for alias, canonical in _CATALOG_SEARCH_PREFIX_ALIASES:
        if folded.startswith(alias):
            suffix = folded[len(alias) : len(alias) + 1]
            if not suffix or not (suffix.isascii() and suffix.isalnum()):
                return canonical, folded[len(alias) :].strip()
        if folded.endswith(alias):
            prefix = folded[-len(alias) - 1 : -len(alias)]
            if not prefix or not (prefix.isascii() and prefix.isalnum()):
                return canonical, folded[: -len(alias)].strip()
    return None


def catalog_search_query(query: str) -> str:
    """Move a leading or trailing weapon shorthand before the finish name."""

    clean_query = query.strip()
    weapon_and_finish = _split_catalog_search_weapon(_without_query_catalog_terms(clean_query))
    if weapon_and_finish is None:
        return clean_query

    weapon, finish = weapon_and_finish
    query_normalised = _catalog_normalise(clean_query)
    parts: list[str] = []
    if any(_catalog_normalise(term) in query_normalised for term in ("stattrak", "暗金")):
        parts.append("StatTrak")
    if any(_catalog_normalise(term) in query_normalised for term in ("souvenir", "纪念品")):
        parts.append("Souvenir")
    parts.append(f"{weapon} {finish}".strip())
    if wear := _find_wear(clean_query):
        parts.append(wear)
    return " ".join(parts)


def _strip_query_catalog_terms(query: str) -> str:
    stripped = _without_query_catalog_terms(query)
    return _canonicalise_catalog_weapon(_catalog_normalise(stripped))


def _strip_candidate_catalog_terms(value: str) -> str:
    normalised = _catalog_normalise(value)
    terms = {
        *(_catalog_normalise(wear) for wear in _WEAR_VALUES),
        *(_catalog_normalise(alias) for alias in WEAR_MAP if not alias.isascii() or len(alias) > 2),
        *(_catalog_normalise(term) for term in _CATALOG_SPECIAL_TERMS),
    }
    for term in sorted(terms, key=len, reverse=True):
        normalised = normalised.replace(term, "")
    return _canonicalise_catalog_weapon(normalised)


def _catalog_weapon_forms(value: str) -> set[str]:
    canonical = _canonicalise_catalog_weapon(value)
    forms = {value, canonical}
    if canonical in {mapped for _, mapped in _CATALOG_WEAPON_PREFIX_ALIASES}:
        forms.update(
            alias for alias, mapped in _CATALOG_WEAPON_PREFIX_ALIASES if mapped == canonical
        )
    return forms


def _catalog_forms(market_name: str, display_name: str) -> set[str]:
    forms: set[str] = set()
    weapon_forms: set[str] = set()
    finish_forms: set[str] = set()
    for value in (market_name, display_name):
        core = _strip_candidate_catalog_terms(value)
        if core:
            forms.add(core)
        if "|" in value:
            weapon, finish = value.split("|", 1)
            weapon_core = _strip_candidate_catalog_terms(weapon)
            if weapon_core:
                weapon_forms.update(_catalog_weapon_forms(weapon_core))
            finish = finish.split("(", 1)[0]
            finish_core = _strip_candidate_catalog_terms(finish)
            if finish_core:
                forms.add(finish_core)
                finish_forms.add(finish_core)
    forms.update(f"{weapon}{finish}" for weapon in weapon_forms for finish in finish_forms)
    forms.update(f"{finish}{weapon}" for weapon in weapon_forms for finish in finish_forms)
    return forms


def _catalog_wear(market_name: str) -> str | None:
    match = re.search(r"\(([^()]*)\)\s*$", market_name)
    if match is None or match.group(1) not in _WEAR_VALUES:
        return None
    return match.group(1)


def resolve_catalog_name(query: str, entries: Sequence[CatalogEntry]) -> str | None:
    """Resolve one unique item from the last verified full-catalog snapshot."""

    clean_query = query.strip()
    if not clean_query or len(clean_query) > 300:
        return None
    query_core = _strip_query_catalog_terms(clean_query)
    if len(query_core) < 2:
        return None

    query_normalised = _catalog_normalise(clean_query)
    requested_wear = _find_wear(clean_query)
    wants_stattrak = any(
        _catalog_normalise(term) in query_normalised for term in ("stattrak", "暗金")
    )
    wants_souvenir = any(
        _catalog_normalise(term) in query_normalised for term in ("souvenir", "纪念品")
    )

    matches: dict[str, int] = {}
    for entry in entries:
        market_name = entry.market_name.strip()
        display_name = entry.display_name.strip()
        if not market_name or not display_name:
            continue

        market_normalised = _catalog_normalise(market_name)
        display_normalised = _catalog_normalise(display_name)
        is_stattrak = "stattrak" in market_normalised or "暗金" in display_normalised
        is_souvenir = "souvenir" in market_normalised or "纪念品" in display_normalised
        if is_stattrak != wants_stattrak or is_souvenir != wants_souvenir:
            continue

        candidate_wear = _catalog_wear(market_name)
        if requested_wear is None:
            if candidate_wear is not None:
                continue
        elif candidate_wear != requested_wear:
            continue

        forms = _catalog_forms(market_name, display_name)
        if query_core in forms:
            score = 3
        elif any(form.endswith(query_core) for form in forms):
            score = 2
        elif any(query_core in form for form in forms):
            score = 1
        else:
            continue
        matches[market_name] = max(matches.get(market_name, 0), score)

    if not matches:
        return None
    best_score = max(matches.values())
    best = sorted(name for name, score in matches.items() if score == best_score)
    return best[0] if len(best) == 1 else None


async def resolve_name(query: str) -> str | None:
    """Resolve a user-facing alias to a Steam market hash name.

    This remains async for compatibility with the original public API used by
    the alert and comparison plugins.
    """

    query = query.strip()
    if not query:
        return None
    if "|" in query:
        return _full_market_name(query)

    best = _best_skin_alias(query)
    if best is None:
        return None

    alias, (weapon, skin) = best
    if alias in AMBIGUOUS_SKIN_ALIASES:
        return None
    wear = _find_wear(query)
    if wear is None:
        return None
    return f"{weapon} | {skin} ({wear})"


def resolution_failure_message(query: str) -> str:
    """Explain a strict resolution failure while preserving the user's text."""

    original = query.strip()
    if "|" in original:
        return (
            f"未能确认物品“{original}”：请提供完整市场名和磨损，"
            "例如 AK-47 | Redline (Field-Tested)。本次未查询，也不会交给聊天模型猜。"
        )

    best = _best_skin_alias(original)
    if best is not None and best[0] in AMBIGUOUS_SKIN_ALIASES:
        alias_offset = _normalise_alias(original).find(_normalise_alias(best[0]))
        if alias_offset > 0:
            if _find_wear(original) is None:
                return (
                    f"已识别“{original}”中的武器和外观，但缺少磨损；请补充崭新、略磨、"
                    "久经、破损或战痕。本次未查询，也不会默认磨损。"
                )
            return (
                f"未找到与“{original}”完全一致的物品；请检查武器、外观和磨损。"
                "本次已停止，不会改成同系列的其他武器。"
            )
        missing = "具体武器" if _find_wear(original) is not None else "具体武器和磨损"
        return (
            f"物品“{original}”存在武器歧义，请补充{missing}"
            "（请把具体武器写在外观名前）。"
            "本次未查询，也不会交给聊天模型猜。"
        )
    if best is not None and _find_wear(original) is None:
        return (
            f"已识别“{original}”的外观，但缺少磨损；请补充崭新、略磨、久经、"
            "破损或战痕。本次未查询，也不会默认磨损。"
        )
    return (
        f"未能唯一确认物品“{original}”；可能是名称不完整、存在多个候选或全量目录"
        "仍在同步。请发送完整 Steam 市场名（含武器、外观、磨损）。"
        "本次已停止，不会交给聊天模型猜。"
    )


_NATURAL_PRICE_PATTERNS = (
    re.compile(r"^(?P<item>.+?)(?:现在)?多少钱[？?]?$"),
    re.compile(r"^(?P<item>.+?)(?:的)?价格(?:是多少|多少)?[？?]?$"),
    re.compile(r"^(?P<item>.+?)(?:现在)?什么价[？?]?$"),
)


def extract_natural_price_query(message: str) -> str | None:
    """Return the item portion of a short, explicit natural-language quote request."""

    text = message.strip()
    if not text or len(text) > 80 or "\n" in text:
        return None
    for pattern in _NATURAL_PRICE_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        item = re.sub(r"^(?:请问|麻烦|帮我查(?:一下)?|查一下)", "", match.group("item")).strip()
        return item or None
    return None


def data_time_line(timestamp: float) -> str:
    return f"数据时间: {format_data_time(timestamp)}"


def broad_market_timestamp(
    data: Mapping[str, Any] | None,
    *,
    now: float | None = None,
) -> tuple[float | None, str | None]:
    """Validate the timestamp shared by broad-index facts and screenshots."""

    if data is None:
        return None, "⚠️ 大盘数据获取失败，本次拒绝展示，请稍后重试。"
    try:
        snapshot = validate_broad_market_response(data, now=now)
    except MarketDataError as exc:
        return None, exc.user_message
    return snapshot.timestamp, None


def format_price(
    data: Mapping[str, Any],
    *,
    data_timestamp: float | None = None,
    now: float | None = None,
) -> str:
    """Format fresh market quotes; unverifiable or crossed fields fail closed."""

    payload = dict(data)
    if data_timestamp is not None and _mapping_timestamp(payload) is None:
        payload["updateTime"] = data_timestamp
    try:
        quotes = validate_quote_response(payload, now=now)
    except MarketDataError as exc:
        return exc.user_message
    quote_lines: list[str] = []
    quote_timestamps: list[float] = []
    for quote in quotes:
        sell_count_text = "--" if quote.sell_count is None else str(quote.sell_count)
        if quote.bid_price is not None and quote.bid_count is not None:
            bid_text = f"收 {quote.bid_price:>9.2f} ({quote.bid_count:>4})"
        else:
            bid_text = "收      暂无有效报价"
        quote_timestamps.append(quote.timestamp)
        quote_lines.append(
            f"  {PLATFORM_NAMES.get(quote.platform, quote.platform):8s} "
            f"卖 {quote.sell_price:>9.2f} ({sell_count_text:>5})  {bid_text}"
        )
    timestamp = min(quote_timestamps)
    lines = ["各平台报价", data_time_line(timestamp), *quote_lines]
    return "\n".join(lines)


def format_average_price(
    data: Mapping[str, Any],
    *,
    fetched_at: float,
    now: float | None = None,
) -> str:
    """Format the provider's seven-day average with an explicit fetch time."""

    if not data.get("success") or not isinstance(data.get("data"), Mapping):
        return "⚠️ 7日均价暂不可用，本次不引用。"
    current = time.time() if now is None else now
    timestamp = _parse_timestamp(fetched_at)
    if error := freshness_error(
        timestamp,
        now=current,
        max_age=PRICE_MAX_AGE_SECONDS,
        subject="7日均价请求",
    ):
        return error
    assert timestamp is not None

    payload = data["data"]
    overall = payload.get("avgPrice")
    if (
        isinstance(overall, bool)
        or not isinstance(overall, (int, float))
        or not math.isfinite(float(overall))
        or overall <= 0
    ):
        return "⚠️ 7日均价字段无效，本次不引用。"

    raw_platforms = payload.get("dataList", [])
    platforms = (
        raw_platforms
        if isinstance(raw_platforms, Sequence) and not isinstance(raw_platforms, (str, bytes))
        else []
    )
    averages: dict[str, float] = {}
    for item in platforms:
        if not isinstance(item, Mapping):
            continue
        code = item.get("platform")
        value = item.get("avgPrice")
        if (
            not isinstance(code, str)
            or not code
            or code in averages
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            continue
        averages[code] = float(value)

    lines = [
        "7日历史均价",
        f"获取时间: {format_data_time(timestamp)}",
        f"  全平台均价: ¥{float(overall):.2f}",
    ]
    for code in _AVG_PLATFORM_ORDER:
        if code in averages:
            lines.append(f"  {PLATFORM_NAMES.get(code, code)}均价: ¥{averages[code]:.2f}")
    for code in sorted(averages):
        if code not in _AVG_PLATFORM_ORDER:
            lines.append(f"  {code}均价: ¥{averages[code]:.2f}")
    return "\n".join(lines)


KlinePoint = tuple[float, float, float, float, float]


def fresh_kline_points(
    data: Mapping[str, Any],
    *,
    now: float | None = None,
) -> tuple[list[KlinePoint] | None, str | None]:
    """Validate SteamDT OHLC points and their source timestamp."""

    if not data.get("success"):
        return None, "⚠️ 市场K线获取失败，本次拒绝展示。"
    raw_points = data.get("data")
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes)):
        return None, "⚠️ 市场K线数据格式无效，本次拒绝展示。"

    points: list[KlinePoint] = []
    for raw in raw_points:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 5:
            continue
        timestamp = _parse_timestamp(raw[0])
        values = raw[1:5]
        if timestamp is None or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in values
        ):
            continue
        open_price, close_price, high_price, low_price = map(float, values)
        if high_price < max(open_price, close_price, low_price):
            continue
        if low_price > min(open_price, close_price, high_price):
            continue
        points.append((timestamp, open_price, close_price, high_price, low_price))

    points.sort(key=lambda point: point[0])
    if len(points) < 2:
        return None, "⚠️ 市场K线有效数据不足，本次拒绝展示。"

    latest = points[-1][0]
    error = freshness_error(
        latest,
        now=time.time() if now is None else now,
        max_age=KLINE_MAX_AGE_SECONDS,
        subject="市场K线",
    )
    return (None, error) if error else (points, None)


def format_kline(
    name: str,
    data: Mapping[str, Any],
    *,
    now: float | None = None,
) -> str:
    """Render recent OHLC facts with an explicit source and data time."""

    points, error = fresh_kline_points(data, now=now)
    if error:
        return error
    assert points is not None

    displayed = points[-7:]
    latest = displayed[-1][0]
    lines = [f"{name} 最近{len(displayed)}个日K", data_time_line(latest)]
    for timestamp, open_price, close_price, high_price, low_price in displayed:
        date = datetime.fromtimestamp(timestamp, _BROAD_MARKET_TZ).strftime("%m-%d")
        change = (close_price - open_price) / open_price * 100
        lines.append(
            f"  {date} 开{open_price:.2f} 收{close_price:.2f} "
            f"高{high_price:.2f} 低{low_price:.2f}  "
            f"{'+' if change >= 0 else ''}{change:.1f}%"
        )
    first_close = displayed[0][2]
    interval_change = (displayed[-1][2] - first_close) / first_close * 100
    lines.append(
        f"──────────\n展示区间涨跌: {'+' if interval_change >= 0 else ''}{interval_change:.1f}%"
    )
    return "\n".join(lines)


def format_item_analysis(
    name: str,
    quote_data: Mapping[str, Any],
    kline_data: Mapping[str, Any],
    *,
    average_data: Mapping[str, Any] | None = None,
    average_fetched_at: float | None = None,
    now: float | None = None,
) -> str:
    """Build an item summary only from timestamped quotes and validated OHLC data."""

    quote = format_price(quote_data, now=now)
    points, error = fresh_kline_points(kline_data, now=now)
    lines = [name, "", quote]
    if average_data is not None and average_fetched_at is not None:
        lines.extend(
            (
                "──────────",
                format_average_price(
                    average_data,
                    fetched_at=average_fetched_at,
                    now=now,
                ),
            )
        )
    lines.extend(("──────────", "📊 【日K统计】"))
    if error:
        lines.append(error)
        return "\n".join(lines)
    assert points is not None

    window = points[-30:]
    latest_timestamp = window[-1][0]
    closes = [point[2] for point in window]
    average = sum(closes) / len(closes)
    interval_change = (closes[-1] - closes[0]) / closes[0] * 100
    lines.extend(
        (
            data_time_line(latest_timestamp),
            f" 样本: 最近{len(window)}个日K",
            f" 收盘均价: ¥{average:.2f}",
        )
    )
    if len(points) >= 2:
        one_day = (points[-1][2] - points[-2][2]) / points[-2][2] * 100
        lines.append(f" 1日动量: {one_day:+.2f}%")
    if len(points) >= 8:
        seven_day = (points[-1][2] - points[-8][2]) / points[-8][2] * 100
        lines.append(f" 7日动量: {seven_day:+.2f}%")
    lines.append(f" 样本区间涨跌: {interval_change:+.2f}%")
    lines.append("⚠️ 仅为历史数据统计，不代表价格预测。")
    return "\n".join(lines)


def _volume_cache_path() -> Path:
    return steamdt_cache_path()


def _summary_number(value: object, *, positive: bool) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _summary_change(value: object) -> str | None:
    number = _summary_number(value, positive=False)
    if number is None or abs(number) > 1_000:
        return None
    if number > 0:
        return f"↑ {number:g}%"
    if number < 0:
        return f"↓ {abs(number):g}%"
    return "→ 0%"


def _format_summary_volume(market: Mapping[str, Any]) -> tuple[str, bool]:
    today = market.get("todayStatistics")
    yesterday = market.get("yesterdayStatistics")
    if not isinstance(today, Mapping) or not isinstance(yesterday, Mapping):
        return "───────────────\n💰 【饰品成交】\n⚠️ 饰品成交数据无效，本次不展示。\n", False

    volume = _summary_number(today.get("tradeNum"), positive=True)
    turnover = _summary_number(today.get("turnover"), positive=True)
    volume_yesterday = _summary_number(yesterday.get("tradeNum"), positive=True)
    turnover_yesterday = _summary_number(yesterday.get("turnover"), positive=True)
    volume_change = _summary_change(today.get("tradeVolumeRatio"))
    turnover_change = _summary_change(today.get("tradeAmountRatio"))
    if None in (
        volume,
        turnover,
        volume_yesterday,
        turnover_yesterday,
        volume_change,
        turnover_change,
    ):
        return "───────────────\n💰 【饰品成交】\n⚠️ 饰品成交数据无效，本次不展示。\n", False

    assert isinstance(volume, float)
    assert isinstance(turnover, float)
    assert isinstance(volume_yesterday, float)
    assert isinstance(turnover_yesterday, float)
    assert isinstance(volume_change, str)
    assert isinstance(turnover_change, str)
    return (
        "───────────────\n"
        "💰 【饰品成交】\n"
        f" 今日累计成交量: {volume / 10_000:.3f}万（昨日: {volume_yesterday / 10_000:.3f}万）\n"
        f" 今日累计成交额: {turnover / 10_000:.3f}万（昨日: {turnover_yesterday / 10_000:.3f}万）\n"
        f" 环比指标: 成交额{turnover_change} / 成交量{volume_change}\n",
        True,
    )


def _format_volume(cache_path: Path, *, now: float) -> tuple[str, bool]:
    try:
        if not cache_path.is_file() or cache_path.is_symlink():
            return "───────────────\n💰 【饰品成交】\n⚠️ 饰品成交数据缺失，本次不展示。\n", False
        timestamp = cache_path.stat().st_mtime
        if error := freshness_error(
            timestamp,
            now=now,
            max_age=VOLUME_MAX_AGE_SECONDS,
            subject="饰品成交数据",
        ):
            return f"───────────────\n💰 【饰品成交】\n{error}\n", False
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping) and payload.get("success"):
            volume = _market_metric(payload.get("volume"))
            turnover = _market_metric(payload.get("turnover"))
            volume_yesterday = _market_metric(payload.get("volume_yest"))
            turnover_yesterday = _market_metric(payload.get("turnover_yest"))
            volume_change = _market_change(payload.get("volume_change"))
            turnover_change = _market_change(payload.get("turnover_change"))
        else:
            volume = turnover = None
            volume_yesterday = turnover_yesterday = None
            volume_change = turnover_change = None
        if volume is not None and turnover is not None:
            volume_line = f" 今日累计成交量: {volume}"
            if volume_yesterday is not None:
                volume_line += f"（昨日: {volume_yesterday}）"
            turnover_line = f" 今日累计成交额: {turnover}"
            if turnover_yesterday is not None:
                turnover_line += f"（昨日: {turnover_yesterday}）"
            change_line = ""
            if turnover_change is not None and volume_change is not None:
                change_line = f" 环比指标: 成交额{turnover_change} / 成交量{volume_change}\n"
            return (
                (
                    "───────────────\n💰 【饰品成交】\n"
                    f" {data_time_line(timestamp)}\n"
                    f"{volume_line}\n"
                    f"{turnover_line}\n"
                    f"{change_line}"
                ),
                True,
            )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Volume cache unavailable: {}", type(exc).__name__)
    return "───────────────\n💰 【饰品成交】\n⚠️ 饰品成交数据无效，本次不展示。\n", False


def _market_metric(value: object) -> str | None:
    raw = str(value).strip() if value is not None else ""
    normalized = raw.replace(",", "").replace("¥", "").replace("￥", "").replace("件", "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(万)?", normalized)
    if match is None:
        return None
    number = float(match.group(1))
    if not math.isfinite(number) or number <= 0:
        return None
    return f"{match.group(1)}{match.group(2) or ''}"


def _market_change(value: object) -> str | None:
    raw = str(value).strip() if value is not None else ""
    match = re.fullmatch(r"([↑↓])\s*([+-]?\d+(?:\.\d+)?)%", raw)
    if match is None:
        return None
    direction, raw_number = match.groups()
    number = float(raw_number)
    if not math.isfinite(number) or abs(number) > 1_000:
        return None
    if direction == "↑" and number < 0:
        return None
    if direction == "↓" and number > 0:
        return None
    return f"{direction} {abs(number):g}%"


def _format_temperature(temperature: TemperatureResult | None) -> str:
    lines = ["───────────────", "🌡️ 【盘面温度】"]
    if not isinstance(temperature, MarketTemperature):
        lines.append(" 温度暂不可用: 历史样本不足")
        return "\n".join(lines)
    lines.extend(
        (
            f" 当前状态: {temperature.current_state}",
            f" 二级明细: {temperature.detail}",
            " 动量数据: "
            f"1日 {temperature.change_1d:+.2f}% / "
            f"3日 {temperature.change_3d:+.2f}% / "
            f"7日 {temperature.change_7d:+.2f}%",
            f" 温度状态: {temperature.temperature_state}",
            f" 温度分: {temperature.score:.1f}/100",
        )
    )
    if temperature.liquidity_note:
        lines.append(f" {temperature.liquidity_note}")
    return "\n".join(lines)


def format_broad_market(
    data: Mapping[str, Any] | None,
    *,
    temperature: TemperatureResult | None = None,
    volume_cache_path: Path | None = None,
    now: float | None = None,
) -> str:
    """Format a broad-market response.

    The optional path and clock make stale/corrupt cache handling directly
    testable without changing the production output.
    """

    if not data or not data.get("success") or not isinstance(data.get("data"), Mapping):
        return "⚠️ 大盘数据获取失败，本次拒绝分析，请稍后重试。"

    market = data["data"]
    current_time = time.time() if now is None else now
    data_timestamp, error = broad_market_timestamp(data, now=current_time)
    if error:
        return error
    assert data_timestamp is not None
    try:
        index = float(market["broadMarketIndex"])
        diff_yesterday = float(market["diffYesterday"])
        diff_percent = float(market["diffYesterdayRatio"])
    except (KeyError, TypeError, ValueError):
        return "⚠️ 大盘字段无效，本次拒绝分析。"
    if not all(math.isfinite(value) for value in (index, diff_yesterday, diff_percent)):
        return "⚠️ 大盘数值异常，本次拒绝分析。"
    history = market.get("historyMarketIndexList", [])
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        history = []

    # "今日高低" boundaries must follow the market's Asia/Shanghai trading day,
    # not the host machine's local timezone.
    current = datetime.fromtimestamp(data_timestamp, _BROAD_MARKET_TZ)
    today_start = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today_end = today_start + 86_400

    valid_points: list[tuple[float, float]] = []
    for point in history:
        if (
            isinstance(point, Sequence)
            and not isinstance(point, (str, bytes))
            and len(point) >= 2
            and isinstance(point[0], (int, float))
            and not isinstance(point[0], bool)
            and isinstance(point[1], (int, float))
            and not isinstance(point[1], bool)
            and math.isfinite(float(point[0]))
            and math.isfinite(float(point[1]))
        ):
            valid_points.append((float(point[0]), float(point[1])))

    today_points = [point for point in valid_points if today_start <= point[0] < today_end]
    yesterday_points = [
        point for point in valid_points if today_start - 86400 <= point[0] < today_start
    ]
    yesterday_close = yesterday_points[-1][1] if yesterday_points else index - diff_yesterday

    if "todayStatistics" in market or "yesterdayStatistics" in market:
        volume, volume_available = _format_summary_volume(market)
    else:
        volume, volume_available = _format_volume(
            volume_cache_path or _volume_cache_path(),
            now=current_time,
        )
    validation_note = (
        "ℹ️ 指数与已展示成交均通过字段及时效校验；成交为当日累计值。\n"
        if volume_available
        else "ℹ️ 指数已通过字段及时效校验；成交未通过展示条件，本次不引用。\n"
    )
    lines = [
        "📈 【市场指数】",
        data_time_line(data_timestamp),
        f" 大盘点位: {index:.2f}",
        f" 昨日收盘: {yesterday_close:.2f}",
    ]
    if today_points:
        lines.extend(
            (
                f" 今日最高: {max(point[1] for point in today_points):.2f}",
                f" 今日最低: {min(point[1] for point in today_points):.2f}",
            )
        )
    else:
        lines.append(" 今日高低: 当日分时样本不足，本次不推算")
    lines.append(f" 今日涨幅: {diff_percent:+.2f}% ({diff_yesterday:+.2f})")
    return (
        "\n".join(lines)
        + "\n"
        + volume
        + _format_temperature(temperature)
        + "\n"
        + "───────────────\n"
        + validation_note
        + "ℹ️ 温度为本机器人量化动量指标，不代表价格预测。\n"
        + "💡 发送<菜单>获取更多功能"
    )


def format_composite_market(
    data: Mapping[str, Any] | None,
    *,
    temperature: TemperatureResult | None,
    breadth_report: str,
    now: float | None = None,
) -> str:
    """Render a multi-dimensional market view without inventing one opaque score."""

    current_time = time.time() if now is None else now
    timestamp, error = broad_market_timestamp(data, now=current_time)
    if error:
        return error
    assert data is not None and timestamp is not None
    market = data.get("data")
    if not isinstance(market, Mapping):
        return "⚠️ 综合指数数据无效，本次拒绝分析。"
    try:
        index = float(market["broadMarketIndex"])
        change = float(market["diffYesterdayRatio"])
    except (KeyError, TypeError, ValueError):
        return "⚠️ 综合指数字段无效，本次拒绝分析。"
    if not math.isfinite(index) or not math.isfinite(change):
        return "⚠️ 综合指数数值异常，本次拒绝分析。"

    lines = [
        "📊 【综合市场视图】",
        data_time_line(timestamp),
        f" 大盘点位: {index:.2f}",
        f" 今日涨跌: {change:+.2f}%",
    ]
    if isinstance(temperature, MarketTemperature):
        lines.extend(
            (
                f" 动量温度: {temperature.score:+.1f}/100（{temperature.temperature_state}）",
                f" 动量说明: {temperature.detail}",
            )
        )
    else:
        lines.append(" 动量温度: 暂不可用（历史样本不足）")
    lines.extend(("──────────", breadth_report))
    lines.append("ℹ️ 三个维度分别展示，不合成为不可解释的单一分值。")
    return "\n".join(lines)


# Compatibility names historically imported from ``src.plugins.chat``.
NM = PLATFORM_NAMES
fmt_price = format_price
fmt_broad = format_broad_market

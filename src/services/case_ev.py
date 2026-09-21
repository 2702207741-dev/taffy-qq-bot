"""Case-opening expected value (开箱EV) over verified local quotes.

The pool enumeration and Valve's officially published tier odds come from
``case_meta``; every price is a validated SteamDT quote for the exact wear
variant found in the local market catalog — never a scraped snapshot and
never a guess.  The rare special pool (knives/gloves) is honestly **not**
priced item-by-item (50+ individual markets would flood the upstream), so
the reported EV is a documented *lower bound* that excludes it.  Missing
wear prices contribute zero but keep their slot in the denominator (the
average runs over every enumerated variant), so partial gaps lower the
item average honestly instead of inflating it; the reply states the
direction of the resulting bias.

EV is negative for essentially every case; the reply says so in plain
words and always carries the fixed disclaimer that the probabilities are
Valve's public odds and history promises nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .case_meta import (
    SPECIAL_ODDS,
    TIER_LABELS,
    TIER_ODDS,
    case_ev_enabled,
    case_meta_store,
)
from .market import market_service
from .market_validation import MarketDataError, validate_quote_response

logger = logging.getLogger("taffy.services.case_ev")

_PRICE_TTL = 600.0
_PRICE_SEMAPHORE = asyncio.Semaphore(4)
_PRICE_TIMEOUT = 12.0
_EV_CACHE_TTL = 3_600
_MAX_TABLE_ROWS = 12

_DISCLAIMER = "概率为 Valve 公开掉率，历史数据不构成收益承诺。"
_SPECIAL_NOTE = "罕见特殊物品（刀/手套）未逐一切价，实际期望只会高于此下界。"
_SYNCING_REPLY = (
    "箱子元数据正在后台同步（首次约需几分钟，之后 7 天内免同步），请稍后再发一次 开箱EV。"
)
_NO_PRICE_SENTINEL = "无价"

_WEAR_SUFFIXES = (
    "Factory New",
    "Minimal Wear",
    "Field-Tested",
    "Well-Worn",
    "Battle-Scarred",
)
_WEAR_LABELS = {
    "Factory New": "崭新出厂",
    "Minimal Wear": "略有磨损",
    "Field-Tested": "久经沙场",
    "Well-Worn": "破损不堪",
    "Battle-Scarred": "战痕累累",
}


@dataclass(frozen=True, slots=True)
class WearQuote:
    wear: str
    market_hash_name: str
    price: float | None


@dataclass(frozen=True, slots=True)
class CaseEvItem:
    item_name: str
    item_name_zh: str | None
    tier: str
    odds: float
    wear_quotes: tuple[WearQuote, ...]
    avg_price: float | None
    contribution: float


@dataclass(frozen=True, slots=True)
class CaseEvResult:
    market_hash_name: str
    display_name: str
    case_price: float | None
    items: tuple[CaseEvItem, ...]
    special_count: int
    unpriced_items: int
    ev: float
    missing_prices: int
    unavailable_prices: int


async def _price_one(market_hash_name: str) -> dict | None:
    """One validated SteamDT snapshot; None on any validation failure."""

    try:
        async with asyncio.timeout(_PRICE_TIMEOUT):
            async with _PRICE_SEMAPHORE:
                data = await market_service.get(
                    "/open/cs2/v1/price/single",
                    {"marketHashName": market_hash_name},
                    ttl=_PRICE_TTL,
                )
    except Exception as exc:
        logger.warning(
            "EV price fetch failed name=%s error=%s", market_hash_name, type(exc).__name__
        )
        return None
    try:
        quotes = validate_quote_response(data)
    except MarketDataError:
        return None
    if not quotes:
        return None
    return {"min_sell": min(quote.sell_price for quote in quotes), "quotes": quotes}


def _wear_variants(entries: tuple[str, ...], base_name: str) -> list[str]:
    """Local-catalog wear variants of one base skin, canonical order."""

    prefix = f"{base_name} ("
    found: list[tuple[int, str]] = []
    for market_name in entries:
        if not market_name.startswith(prefix) or not market_name.endswith(")"):
            continue
        wear = market_name[len(prefix) : -1]
        if wear in _WEAR_SUFFIXES:
            found.append((_WEAR_SUFFIXES.index(wear), market_name))
    return [name for _order, name in sorted(found)]


async def compute_case_ev(crate, items, catalog_names: tuple[str, ...]) -> CaseEvResult:
    """Pure-orchestration EV computation from a resolved crate and its pool."""

    tier_items: dict[str, list] = {tier: [] for tier in TIER_ODDS}
    for item in items:
        tier_items.setdefault(item.tier, []).append(item)
    for bucket in tier_items.values():
        bucket.sort(key=lambda entry: entry.item_name)

    # Case price: the tradable container itself, one validated snapshot.
    case_snapshot = await _price_one(crate.market_hash_name)
    case_price = case_snapshot["min_sell"] if case_snapshot else None

    wear_names: list[str] = []
    per_item_variants: dict[str, list[str]] = {}
    for item in items:
        variants = _wear_variants(catalog_names, item.item_name)
        per_item_variants[item.item_name] = variants
        wear_names.extend(variants)

    priced: dict[str, float | None] = {}

    async def price_task(name: str) -> None:
        snapshot = await _price_one(name)
        priced[name] = snapshot["min_sell"] if snapshot else None

    if wear_names:
        await asyncio.gather(*(price_task(name) for name in wear_names))

    ev_items: list[CaseEvItem] = []
    unpriced_items = 0
    missing_prices = 0
    unavailable_prices = 0
    ev = 0.0
    for tier, bucket in tier_items.items():
        if not bucket:
            continue
        odds = TIER_ODDS[tier] / len(bucket)
        for item in bucket:
            variants = per_item_variants[item.item_name]
            quotes = tuple(
                WearQuote(
                    wear=name[name.rfind("(") + 1 : -1],
                    market_hash_name=name,
                    price=priced.get(name),
                )
                for name in variants
            )
            values = [quote.price for quote in quotes if quote.price is not None]
            missing_prices += len(quotes) - len(values)
            if variants and not values:
                unavailable_prices += 1
            # Missing wear contributes zero but stays in the denominator:
            # the average is over every enumerated variant (quotes), not
            # only the priced subset (values).
            avg_price = sum(values) / len(quotes) if values else None
            contribution = odds * avg_price if avg_price is not None else 0.0
            if avg_price is None:
                unpriced_items += 1
            else:
                ev += contribution
            ev_items.append(
                CaseEvItem(
                    item_name=item.item_name,
                    item_name_zh=item.item_name_zh,
                    tier=tier,
                    odds=odds,
                    wear_quotes=quotes,
                    avg_price=avg_price,
                    contribution=contribution,
                )
            )

    display = crate.name_zh or crate.name_en
    return CaseEvResult(
        market_hash_name=crate.market_hash_name,
        display_name=display,
        case_price=case_price,
        items=tuple(sorted(ev_items, key=lambda entry: (-entry.contribution, entry.item_name))),
        special_count=crate.rare_count,
        unpriced_items=unpriced_items,
        ev=ev,
        missing_prices=missing_prices,
        unavailable_prices=unavailable_prices,
    )


def case_ev_payload(result: CaseEvResult) -> dict:
    """Structured payload for the desktop shell (same numbers as the reply)."""

    return {
        "market_hash_name": result.market_hash_name,
        "display_name": result.display_name,
        "case_price": result.case_price,
        "ev_lower_bound": round(result.ev, 2),
        "return_rate_percent": (
            round(result.ev / result.case_price * 100, 2)
            if result.case_price and result.case_price > 0
            else None
        ),
        "special_count": result.special_count,
        "special_odds": SPECIAL_ODDS,
        "unpriced_items": result.unpriced_items,
        "missing_prices": result.missing_prices,
        "items": [
            {
                "item_name": item.item_name,
                "item_name_zh": item.item_name_zh,
                "tier": item.tier,
                "odds": round(item.odds, 6),
                "avg_price": round(item.avg_price, 2) if item.avg_price is not None else None,
                "contribution": round(item.contribution, 4),
                "wear_prices": {
                    quote.wear: round(quote.price, 2) if quote.price is not None else None
                    for quote in item.wear_quotes
                },
            }
            for item in result.items
        ],
        "disclaimer": _DISCLAIMER,
        "special_note": _SPECIAL_NOTE,
        "computed_at": int(time.time()),
    }


def _item_label(item: CaseEvItem) -> str:
    tier_label = TIER_LABELS.get(item.tier, item.tier)
    return f"{item.item_name}（{tier_label}）"


async def _catalog_market_names() -> tuple[str, ...]:
    """Local catalog names (already synced from steamdt), for wear variants."""

    from .market_catalog_store import market_catalog_store

    try:
        entries = await market_catalog_store.entries()
    except Exception as exc:
        logger.warning("Catalog unavailable for EV error=%s", type(exc).__name__)
        return ()
    return tuple(entry.market_name for entry in entries)


def _storable_ev_payload(result: CaseEvResult) -> dict:
    """The single payload shape persisted in the EV cache.

    Both callers write through here.  The key set is part of the cache contract
    because the text path renders ``cached["reply_text"]``; the desktop path used
    to store a payload without it, so ``开箱EV`` answered with an **empty QQ
    message** whenever the desktop wrote first (no exception, no log).
    """

    payload = case_ev_payload(result)
    payload["reply_text"] = render_case_ev(result)
    return payload


def _cached_payload_is_complete(payload: dict | None) -> bool:
    """Whether a cached payload may be served without recomputing.

    ``cached_ev`` returns whatever JSON was stored, including rows written by the
    pre-fix desktop path — those lack ``reply_text`` and must be treated as an
    incomplete cache entry: the text path would render ``""`` from them.  Both
    readers share this predicate so the cache self-heals in either order.
    """

    if payload is None:
        return False
    return bool(str(payload.get("reply_text") or "").strip())


#: Keys that exist in the cache payload but must not reach the desktop HTTP body.
#: ``desktop_api.desktop_market_case_ev`` returns ``{"computed": True, **payload}``
#: with no ``response_model``, so *every* key of this payload becomes part of the
#: public response contract.
_DESKTOP_EXCLUDED_KEYS = frozenset({"reply_text"})


def _desktop_ev_payload(payload: dict) -> dict:
    """The EV payload as the desktop contract sees it (QQ-only fields removed).

    Kept deterministic on purpose: before this existed the desktop endpoint
    returned ``cached`` verbatim, so ``reply_text`` showed up **only when the QQ
    path happened to have written first** — a probabilistically-changing public
    response shape, which no test pinned.
    """

    return {key: value for key, value in payload.items() if key not in _DESKTOP_EXCLUDED_KEYS}


async def build_case_ev_reply(query: str) -> str:
    """Render the ``开箱EV`` reply; every failure mode stays honest."""

    if not case_ev_enabled():
        return "开箱EV 功能当前是关闭的。"
    clean = query.strip()
    if not clean:
        return "格式：开箱EV 箱子名（例如：开箱EV 梦魇武器箱）"

    store = case_meta_store
    await store.initialize()
    # Background mode: a first sync downloads ~17 MB and must never block a
    # QQ reply; the caller gets an honest "syncing" answer meanwhile.
    if not await store.sync_if_due(background=True):
        return _SYNCING_REPLY
    crate = await store.find_crate(clean)
    if crate is None:
        return (
            f"没有唯一匹配到“{clean}”这个箱子。请发送完整箱子名"
            "（中文或英文均可），例如：开箱EV 梦魇武器箱。"
        )
    if crate.unknown_count > 0:
        return (
            f"⚠️ “{crate.name_en}”的元数据里有 {crate.unknown_count} 件未识别稀有度的物品，"
            "官方掉率无法可靠分摊，本次拒绝计算（绝不猜测概率）。"
        )
    items = await store.crate_items(crate.crate_id)
    if not items:
        return "⚠️ 这个箱子在本地元数据里没有内含物清单，无法计算 EV。"

    cached = await store.cached_ev(crate.market_hash_name, ttl_seconds=_EV_CACHE_TTL)
    if cached is not None and _cached_payload_is_complete(cached):
        return str(cached["reply_text"])

    # No cache, or an incomplete one (pre-fix desktop rows): compute once and
    # persist the complete payload, which repairs the entry for every later read.
    catalog_names = await _catalog_market_names()
    result = await compute_case_ev(crate, items, catalog_names)
    payload = _storable_ev_payload(result)
    await store.store_ev(crate.market_hash_name, payload)
    return str(payload["reply_text"])


async def build_case_ev_payload(query: str) -> dict | None:
    """Structured 开箱EV data for the desktop shell; None = not computable."""

    if not case_ev_enabled():
        return None
    clean = query.strip()
    if not clean:
        return None
    store = case_meta_store
    await store.initialize()
    if not await store.sync_if_due(background=True):
        return None
    crate = await store.find_crate(clean)
    if crate is None or crate.unknown_count > 0:
        return None
    items = await store.crate_items(crate.crate_id)
    if not items:
        return None
    cached = await store.cached_ev(crate.market_hash_name, ttl_seconds=_EV_CACHE_TTL)
    if cached is not None and _cached_payload_is_complete(cached):
        return _desktop_ev_payload(cached)
    catalog_names = await _catalog_market_names()
    result = await compute_case_ev(crate, items, catalog_names)
    payload = _storable_ev_payload(result)
    await store.store_ev(crate.market_hash_name, payload)
    return _desktop_ev_payload(payload)


def render_case_ev(result: CaseEvResult, *, now: float | None = None) -> str:
    """Deterministic 开箱EV reply; every gap is stated, never filled."""

    lines = [f"{result.display_name} 开箱EV", "──────────"]
    if result.case_price is not None:
        lines.append(f"箱子现价：¥{result.case_price:.2f}（本地已校验报价）")
    else:
        lines.append("箱子现价：无价（本地报价缺失，无法计算回报率）")

    lines.append("──────────")
    lines.append("内含物明细（期望贡献降序）：")
    for item in result.items[:_MAX_TABLE_ROWS]:
        label = _item_label(item)
        odds = f"{item.odds * 100:.2f}%"
        if item.avg_price is None:
            lines.append(f"  {label}  掉率 {odds}  均价 {_NO_PRICE_SENTINEL}")
        else:
            lines.append(
                f"  {label}  掉率 {odds}  均价 ¥{item.avg_price:.2f}  贡献 ¥{item.contribution:.3f}"
            )
    shown = len(result.items)
    if shown > _MAX_TABLE_ROWS:
        lines.append(f"  ……其余 {shown - _MAX_TABLE_ROWS} 件同公式累加")

    lines.append("──────────")
    lines.append(f"常规皮肤池期望值 EV（下界）：¥{result.ev:.2f}")
    if result.case_price and result.case_price > 0:
        rate = result.ev / result.case_price * 100
        verdict = "开箱期望为负" if result.ev < result.case_price else "EV 为正（请核对数据）"
        lines.append(
            f"对比箱子现价：回报率约 {rate:.1f}% —— {verdict}，"
            f"平均每箱期望亏损 ¥{max(0.0, result.case_price - result.ev):.2f}。"
        )
    else:
        lines.append("箱子现价缺失，回报率无法计算（不做猜测）。")
    if result.special_count:
        lines.append(
            f"另有罕见特殊物品 ×{result.special_count}（合计掉率 {SPECIAL_ODDS * 100:.2f}%）："
            + _SPECIAL_NOTE
        )
    if result.missing_prices:
        lines.append(
            f"有 {result.missing_prices} 个磨损报价缺失，均按 0 计入，实际 EV 高于此下界。"
        )
    if result.unavailable_prices:
        lines.append(f"其中 {result.unavailable_prices} 件全部磨损均无价，明细已标“无价”。")
    lines.append(_DISCLAIMER)
    synced = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    lines.append(f"元数据：ByMykel/CSGO-API（{synced} 同步）")
    return "\n".join(lines)

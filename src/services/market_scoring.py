"""Deterministic buy/sell reference scoring over verified local data.

No LLM participates anywhere in this module: every number is a pure
function of validated daily closes, stored liquidity counts and — when the
CSQAQ side is configured — one cross-platform snapshot (spread, rent yield
and per-platform sell counts).  Each factor reports its own score,
effective weight and the exact basis it was computed from, so the output
can be quoted verbatim without inventing anything.  Items with fewer than
``MIN_DAILY_SAMPLES`` closes are not scored at all.

V0.5 sell-side additions ported from allureking/cs2-inventory-manager:
**年化衰减** (annualized holding bleed: local price annualization plus the
CSQAQ rent yield), **集中度** (HHI over per-platform sell counts) and
**收益达标度**.  The original 达标度 measured realized P&L against a
holder's target; this project has no portfolio system (holding P&L
tracking was explicitly rejected in V0.4), so the metric is rewritten to a
price-level definition — the current close's distance to the 90-day high —
and only the indicator definition is ported.  Factors whose inputs are
missing return their weight to the remaining factors, exactly like the
CSQAQ spread factor.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .csqaq import csqaq_service
from .csqaq_history import BASIS_SCORE, csqaq_history_store
from .indicators import (
    bollinger_lower,
    momentum_pair,
    percentile_rank,
    return_z_score,
    wilder_rsi,
)
from .market_history import market_history
from .market_prediction import item_scenario_lines

logger = logging.getLogger("taffy.services.market_scoring")

MIN_DAILY_SAMPLES = 24
_DISCLAIMER = "历史规则回测参考，不构成交易建议。"
_UNCONFIGURED_NOTE = "未配置 CSQAQ，价差项权重已归还其余项"
_REWRITE_NOTE = (
    "收益达标度改写说明：原指标为持仓收益达标（本项目无持仓系统，"
    "V0.4 已否决持仓盈亏追踪），改写为收盘价距 90 日高点的达标度，仅移植指标定义。"
)


@dataclass(frozen=True, slots=True)
class ScoreFactor:
    """One transparent scoring component."""

    name: str
    score: float
    weight: float
    basis: str


@dataclass(frozen=True, slots=True)
class SideScore:
    """Weighted total over the factor list plus any redistribution note."""

    total: float
    factors: tuple[ScoreFactor, ...]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TradeScore:
    market_name: str
    sample_days: int
    buy: SideScore
    sell: SideScore


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _redistribute(factors: list[ScoreFactor], dropped: ScoreFactor) -> list[ScoreFactor]:
    """Return factors with ``dropped``'s weight spread proportionally."""

    remaining = [factor for factor in factors if factor is not dropped]
    kept_weight = sum(factor.weight for factor in remaining)
    if kept_weight <= 0:
        return factors
    redistributed = dropped.weight / kept_weight
    return [
        ScoreFactor(factor.name, factor.score, factor.weight * (1 + redistributed), factor.basis)
        for factor in remaining
    ]


def _redistribute_many(
    factors: list[ScoreFactor], dropped: Sequence[ScoreFactor]
) -> list[ScoreFactor]:
    """One-shot proportional redistribution of several dropped factors.

    Dropping factors one at a time compounds the redistribution (each pass
    scales the surviving weights by the current kept total, which already
    includes earlier surplus), so the dropped set is removed in a single
    pass and their combined weight is spread in proportion to the survivors'
    current weights.  The invariant Σ weight = 1 holds by construction.
    """

    dropped_ids = {id(factor) for factor in dropped}
    remaining = [factor for factor in factors if id(factor) not in dropped_ids]
    dropped_weight = sum(factor.weight for factor in dropped)
    kept_weight = sum(factor.weight for factor in remaining)
    if kept_weight <= 0:
        return factors
    redistributed = dropped_weight / kept_weight
    return [
        ScoreFactor(factor.name, factor.score, factor.weight * (1 + redistributed), factor.basis)
        for factor in remaining
    ]


def _weighted_total(factors: Sequence[ScoreFactor]) -> float:
    weight = sum(factor.weight for factor in factors)
    if weight <= 0:
        return 0.0
    return sum(factor.score * factor.weight for factor in factors) / weight


def _buy_factors(
    closes: Sequence[float],
    *,
    spread_ratio: float | None,
) -> tuple[ScoreFactor, ...]:
    factors: list[ScoreFactor] = []

    rsi = wilder_rsi(closes)
    if rsi is None:
        oversold = ScoreFactor("超卖", 0.0, 0.25, "RSI 样本不足")
    elif rsi < 30:
        oversold = ScoreFactor("超卖", 100.0, 0.25, f"RSI={rsi:.1f}（<30 满档）")
    elif rsi < 40:
        oversold = ScoreFactor(
            "超卖", 100.0 * (40 - rsi) / 10, 0.25, f"RSI={rsi:.1f}（30~40 线性衰减）"
        )
    else:
        oversold = ScoreFactor("超卖", 0.0, 0.25, f"RSI={rsi:.1f}（≥40 不给档）")
    factors.append(oversold)

    latest = float(closes[-1])
    highest = max(float(close) for close in closes)
    drawdown = (1 - latest / highest) * 100 if highest > 0 else 0.0
    factors.append(
        ScoreFactor(
            "回撤位",
            _clamp(drawdown / 30 * 100),
            0.25,
            f"距{len(closes)}日高点回撤 {drawdown:.1f}%（30% 满档）",
        )
    )

    lower = bollinger_lower(closes)
    if lower is None:
        factors.append(ScoreFactor("布林下轨", 0.0, 0.20, "布林样本不足"))
    elif latest <= lower:
        factors.append(
            ScoreFactor("布林下轨", 100.0, 0.20, f"收盘 {latest:.2f} ≤ 下轨 {lower:.2f}")
        )
    elif latest <= lower * 1.01:
        percent = (latest - lower) / lower * 100 if lower > 0 else 100.0
        factors.append(
            ScoreFactor(
                "布林下轨",
                _clamp(100 * (1 - percent)),
                0.20,
                f"收盘高出下轨 {percent:.2f}%（≤1% 线性）",
            )
        )
    else:
        factors.append(
            ScoreFactor("布林下轨", 0.0, 0.20, f"收盘 {latest:.2f} 高于下轨 {lower:.2f} 超 1%")
        )

    # VWAP position (optional data source, V0.5 night batch): price below
    # the volume-weighted reference line is a favourable entry.  Without a
    # real volume series this uses the pure-price anchor — the factor note
    # says so honestly (never labelled as true VWAP).
    from .indicators import vwap

    vwap_value = vwap(closes)
    if vwap_value is None:
        factors.append(ScoreFactor("VWAP位置", 0.0, 0.10, "VWAP 样本不足"))
    elif latest <= vwap_value:
        factors.append(
            ScoreFactor("VWAP位置", 100.0, 0.10, f"收盘 {latest:.2f} ≤ 价格锚 {vwap_value:.2f}")
        )
    else:
        premium = (latest / vwap_value - 1) * 100 if vwap_value > 0 else 0.0
        factors.append(
            ScoreFactor(
                "VWAP位置",
                _clamp(100 - premium * 20),
                0.10,
                f"收盘高于价格锚 {premium:.2f}%（>5% 不给档）",
            )
        )

    if spread_ratio is None:
        spread = ScoreFactor("跨平台价差", 0.0, 0.20, "CSQAQ 数据不可用")
    else:
        if spread_ratio >= 0.98:
            score = 100.0
        elif spread_ratio <= 0.90:
            score = 0.0
        else:
            score = _clamp((spread_ratio - 0.90) / 0.08 * 100)
        spread = ScoreFactor(
            "跨平台价差",
            score,
            0.20,
            f"CSQAQ 最高求购/本地收盘 = {spread_ratio:.3f}（≥0.98 满档）",
        )
    factors.append(spread)
    return tuple(factors)


def _sell_factors(
    closes: Sequence[float],
    *,
    sell_count: int | None,
    bid_count: int | None,
    rent_annual: float | None,
    platform_sell_counts: tuple[int, ...] | None,
    hoard_ratio: float | None = None,
) -> tuple[ScoreFactor, ...]:
    factors: list[ScoreFactor] = []

    latest = float(closes[-1])
    percentile = percentile_rank(latest, closes)
    if percentile is None:
        factors.append(ScoreFactor("价格分位", 0.0, 0.25, "样本不足"))
    else:
        factors.append(
            ScoreFactor(
                "价格分位",
                _clamp(percentile),
                0.25,
                f"处于{len(closes)}日分位 {percentile:.0f}%（越高越利卖）",
            )
        )

    momentum = momentum_pair(closes, window=7)
    if momentum is None:
        factors.append(ScoreFactor("动量衰减", 0.0, 0.20, "动量样本不足"))
    else:
        recent, prior = momentum
        decay = prior - recent
        factors.append(
            ScoreFactor(
                "动量衰减",
                _clamp(decay * 12.5),
                0.20,
                f"近7日 {recent:+.1f}%（前7日 {prior:+.1f}%，走弱 8pp 满档）",
            )
        )

    z_score = return_z_score(closes, window=14)
    if z_score is None:
        factors.append(ScoreFactor("异常波动", 0.0, 0.20, "波动样本不足"))
    elif z_score <= 1.5:
        factors.append(
            ScoreFactor("异常波动", 0.0, 0.20, f"收益率Z值 {z_score:.2f}（≤1.5 不给档）")
        )
    else:
        factors.append(
            ScoreFactor(
                "异常波动",
                _clamp((z_score - 1.5) / 1.5 * 100),
                0.20,
                f"收益率Z值 {z_score:.2f}（≥3 满档）",
            )
        )

    if sell_count is None or bid_count is None or sell_count <= 0:
        liquidity = ScoreFactor("流动性", 0.0, 0.13, "挂单数快照缺失")
    else:
        ratio = bid_count / sell_count
        if ratio >= 0.10:
            score = 100.0
        elif ratio <= 0.01:
            score = 0.0
        else:
            score = _clamp((ratio - 0.01) / 0.09 * 100)
        liquidity = ScoreFactor(
            "流动性",
            score,
            0.13,
            f"求购/在售挂单 {bid_count}/{sell_count}（比值 ≥0.10 满档）",
        )
    factors.append(liquidity)

    factors.append(_annualized_decay_factor(closes, rent_annual=rent_annual))
    factors.append(_profit_attainment_factor(closes))
    factors.append(_concentration_factor(platform_sell_counts))
    factors.append(_hoard_timing_factor(hoard_ratio))
    return tuple(factors)


def _annualized_decay_factor(closes: Sequence[float], *, rent_annual: float | None) -> ScoreFactor:
    """年化衰减：价格年化收益 + CSQAQ 租金年化，净持有越亏越利卖。"""

    if rent_annual is None:
        return ScoreFactor("年化衰减", 0.0, 0.10, "CSQAQ 租金数据缺失")
    latest = float(closes[-1])
    first = float(closes[0])
    if first <= 0 or latest <= 0:
        return ScoreFactor("年化衰减", 0.0, 0.10, "价格样本异常")
    days = max(len(tuple(closes)), 1)
    price_annual = ((latest / first) ** (365.0 / days) - 1.0) * 100.0
    net = price_annual + rent_annual
    if net >= 0:
        score = 0.0
    elif net <= -20.0:
        score = 100.0
    else:
        score = _clamp(-net / 20.0 * 100)
    return ScoreFactor(
        "年化衰减",
        score,
        0.10,
        f"价格年化 {price_annual:+.1f}% + 租金年化 {rent_annual:+.1f}% = 净 {net:+.1f}%（净亏 20% 满档）",
    )


def _profit_attainment_factor(closes: Sequence[float]) -> ScoreFactor:
    """收益达标度（改写定义）：收盘价距 90 日高点越近越利卖。"""

    latest = float(closes[-1])
    high = max(float(close) for close in closes)
    if high <= 0:
        return ScoreFactor("收益达标度", 0.0, 0.05, "价格样本异常")
    room = (high - latest) / high
    if room <= 0.02:
        score = 100.0
    elif room >= 0.20:
        score = 0.0
    else:
        score = _clamp((0.20 - room) / 0.18 * 100)
    return ScoreFactor(
        "收益达标度",
        score,
        0.04,
        f"距{len(closes)}日高点 {room * 100:.1f}%（≤2% 满档）",
    )


def _concentration_factor(platform_sell_counts: tuple[int, ...] | None) -> ScoreFactor:
    """集中度：六平台在售量的 HHI，越分散流动性质量越好。"""

    counts = [count for count in (platform_sell_counts or ()) if count > 0]
    if len(counts) < 2:
        return ScoreFactor("集中度", 0.0, 0.04, "CSQAQ 平台在售数据缺失")
    total = sum(counts)
    hhi = sum((count / total) ** 2 for count in counts)
    if hhi >= 1.0:
        score = 0.0
    else:
        score = _clamp((1.0 - hhi) / (1.0 - 1.0 / 6.0) * 100)
    return ScoreFactor(
        "集中度",
        score,
        0.04,
        f"平台在售 HHI={hhi:.2f}（{len(counts)} 平台，越分散越利卖）",
    )


def _hoard_timing_factor(hoard_ratio: float | None) -> ScoreFactor:
    """挂刀时机：Steam 挂刀比例高分位 → 出 Steam 换人民币划算。

    数据来自 iflow.work 挂刀指数快照（全市场比例分位）；样本不足或
    快照缺失时权重归还其余项（见 ``compute_trade_score`` 的剥离逻辑）。
    比例高 = 平台价相对 Steam 折价小 = 此时出货到 Steam 再换汇更划算。
    """

    if hoard_ratio is None:
        return ScoreFactor("挂刀时机", 0.0, 0.04, "挂刀快照缺失")
    score = _clamp(hoard_ratio * 100)
    return ScoreFactor(
        "挂刀时机",
        score,
        0.04,
        f"Steam 挂刀比例 {hoard_ratio:.2f}（≥0.8 出 Steam 划算）",
    )


def compute_trade_score(
    closes: Sequence[float],
    *,
    sample_days: int,
    sell_count: int | None,
    bid_count: int | None,
    spread_ratio: float | None,
    rent_annual: float | None = None,
    platform_sell_counts: tuple[int, ...] | None = None,
    hoard_ratio: float | None = None,
) -> TradeScore | None:
    """Score one item from verified inputs; ``None`` below the sample gate."""

    values = [float(close) for close in closes]
    if len(values) < MIN_DAILY_SAMPLES or any(not value > 0 for value in values):
        return None

    buy_raw = list(_buy_factors(values, spread_ratio=spread_ratio))
    buy_note: str | None = None
    spread = next(factor for factor in buy_raw if factor.name == "跨平台价差")
    if spread_ratio is None:
        buy_raw = _redistribute(buy_raw, spread)
        buy_note = _UNCONFIGURED_NOTE

    sell_raw = list(
        _sell_factors(
            values,
            sell_count=sell_count,
            bid_count=bid_count,
            rent_annual=rent_annual,
            platform_sell_counts=platform_sell_counts,
            hoard_ratio=hoard_ratio,
        )
    )
    sell_notes: list[str] = []
    liquidity = next(factor for factor in sell_raw if factor.name == "流动性")
    if sell_count is None or bid_count is None or sell_count <= 0:
        sell_raw = _redistribute(sell_raw, liquidity)
        sell_notes.append("挂单数快照缺失，流动性项权重已归还其余项")
    csqaq_missing: list[str] = []
    if rent_annual is None:
        decay = next((factor for factor in sell_raw if factor.name == "年化衰减"), None)
        if decay is not None:
            sell_raw = _redistribute(sell_raw, decay)
            csqaq_missing.append("年化衰减")
    if platform_sell_counts is None or len([c for c in platform_sell_counts if c > 0]) < 2:
        concentration = next((factor for factor in sell_raw if factor.name == "集中度"), None)
        if concentration is not None:
            sell_raw = _redistribute(sell_raw, concentration)
            csqaq_missing.append("集中度")
    if csqaq_missing:
        sell_notes.append("CSQAQ 数据缺失，" + "、".join(csqaq_missing) + "项权重已归还其余项")
    hoard = next((factor for factor in sell_raw if factor.name == "挂刀时机"), None)
    if hoard is not None and hoard_ratio is None:
        # 与 CSQAQ 剥离分开做会导致重复摊回（每次摊回都基于前一轮
        # 已增大的剩余权重），这里把挂刀因子与 CSQAQ 缺失项合并为
        # 一次性按比例摊回，保证 Σ权重恒为 1。
        if csqaq_missing:
            hoard_note = "挂刀快照缺失，挂刀时机与 CSQAQ 缺失项权重已一并归还其余项"
            dropped = [factor for factor in sell_raw if factor.name in csqaq_missing]
            dropped.append(hoard)
            sell_raw = _redistribute_many(sell_raw, tuple(dropped))
        else:
            sell_raw = _redistribute(sell_raw, hoard)
            hoard_note = "挂刀快照缺失，挂刀时机项权重已归还其余项"
        sell_notes.append(hoard_note)

    return TradeScore(
        market_name="",
        sample_days=sample_days,
        buy=SideScore(_weighted_total(buy_raw), tuple(buy_raw), buy_note),
        sell=SideScore(_weighted_total(sell_raw), tuple(sell_raw), "；".join(sell_notes) or None),
    )


@dataclass(frozen=True, slots=True)
class CsqaqScoreInputs:
    """One CSQAQ snapshot projected onto the scoring factors."""

    spread_ratio: float | None = None
    rent_annual: float | None = None
    platform_sell_counts: tuple[int, ...] | None = None


async def _csqaq_inputs(user_query: str, market_name: str, latest_close: float) -> CsqaqScoreInputs:
    """Spread, rent yield and per-platform counts from one verified detail."""

    if latest_close <= 0:
        return CsqaqScoreInputs()
    try:
        response = await csqaq_service.verified_detail(user_query, market_hash_name=market_name)
    except Exception as exc:
        logger.warning("CSQAQ score inputs fetch failed error=%s", type(exc).__name__)
        return CsqaqScoreInputs()
    if not response.get("success"):
        return CsqaqScoreInputs()
    detail = response.get("detail")
    if detail is None:
        return CsqaqScoreInputs()

    spread_ratio: float | None = None
    bids = [quote.buy_price for quote in detail.quotes if quote.buy_price is not None]
    if bids:
        spread_ratio = max(bids) / latest_close

    rent_annual: float | None = None
    if detail.rent is not None:
        rent_annual = (
            detail.rent.long_annual_yield
            if detail.rent.long_annual_yield is not None
            else detail.rent.short_annual_yield
        )

    counts = tuple(
        quote.sell_num
        for quote in detail.quotes
        if quote.sell_num is not None and quote.sell_num > 0
    )
    try:
        await csqaq_history_store.record_snapshot(
            detail.good_id,
            market_name,
            spread_ratio=spread_ratio,
            rent_annual=rent_annual,
            sell_total=sum(counts) if counts else None,
            basis=BASIS_SCORE,
        )
    except Exception as exc:
        logger.warning("CSQAQ history record failed error=%s", type(exc).__name__)
    return CsqaqScoreInputs(spread_ratio, rent_annual, counts or None)


async def _hoard_ratio(market_name: str) -> float | None:
    """One item's hoard ratio from the local snapshot; None = untracked."""
    try:
        from .hoard_index import hoard_index_store

        return await hoard_index_store.item_ratio(market_name)
    except Exception as exc:
        logger.warning("Hoard ratio lookup failed error=%s", type(exc).__name__)
        return None


async def build_trade_score_reply(market_name: str, *, user_query: str | None = None) -> str:
    """Render the ``买卖参考`` reply; every failure mode stays honest."""

    history = await market_history.item_daily_closes(market_name, days=90)
    if history is None:
        return "⚠️ 本地行情池还没有这个饰品的最新样本，暂不评分。"
    closes, sell_count, bid_count = history
    if len(closes) < MIN_DAILY_SAMPLES:
        return f"⚠️ 样本不足（{len(closes)}/{MIN_DAILY_SAMPLES} 个日线），暂不评分，绝不给半残分数。"

    inputs = CsqaqScoreInputs()
    if user_query and csqaq_service.configured():
        inputs = await _csqaq_inputs(user_query, market_name, float(closes[-1][1]))
    score = compute_trade_score(
        [close for _day, close in closes],
        sample_days=len(closes),
        sell_count=sell_count,
        bid_count=bid_count,
        spread_ratio=inputs.spread_ratio,
        rent_annual=inputs.rent_annual,
        platform_sell_counts=inputs.platform_sell_counts,
        hoard_ratio=await _hoard_ratio(market_name),
    )
    if score is None:
        return "⚠️ 样本不足，暂不评分，绝不给半残分数。"
    reply = render_trade_score(market_name, score)
    scenario = _item_scenario_section(closes)
    if scenario is not None:
        reply += "\n──────────\n" + scenario
    return reply


def _item_scenario_section(closes: list[tuple[int, float]]) -> str | None:
    """Per-item history-scenario appendix; ``None`` keeps the card unchanged."""

    dated = {date(1970, 1, 1) + timedelta(days=day): price for day, price in closes}
    lines = item_scenario_lines(dated)
    if not lines:
        return None
    return "📈 历史情景（自身动量同档）:\n" + "\n".join(lines)


async def score_summary_line(market_name: str, *, user_query: str | None = None) -> str | None:
    """One-line summary for 饰品查询; ``None`` on any gap (silent omission)."""

    try:
        history = await market_history.item_daily_closes(market_name, days=90)
        if history is None:
            return None
        closes, sell_count, bid_count = history
        if len(closes) < MIN_DAILY_SAMPLES:
            return None
        inputs = CsqaqScoreInputs()
        if user_query and csqaq_service.configured():
            inputs = await _csqaq_inputs(user_query, market_name, float(closes[-1][1]))
        score = compute_trade_score(
            [close for _day, close in closes],
            sample_days=len(closes),
            sell_count=sell_count,
            bid_count=bid_count,
            spread_ratio=inputs.spread_ratio,
            rent_annual=inputs.rent_annual,
            platform_sell_counts=inputs.platform_sell_counts,
            hoard_ratio=await _hoard_ratio(market_name),
        )
        if score is None:
            return None
        return (
            f"买卖参考：买入 {score.buy.total:.0f} / 卖出 {score.sell.total:.0f}"
            "（发送 买卖参考 查看明细）"
        )
    except Exception as exc:
        logger.warning("Score summary failed error=%s", type(exc).__name__)
        return None


async def build_trade_score_payload(
    market_name: str, *, user_query: str | None = None
) -> dict[str, Any] | None:
    """Structured trade score for the desktop shell; ``None`` = unscored."""

    history = await market_history.item_daily_closes(market_name, days=90)
    if history is None:
        return None
    closes, sell_count, bid_count = history
    if len(closes) < MIN_DAILY_SAMPLES:
        return None
    inputs = CsqaqScoreInputs()
    if user_query and csqaq_service.configured():
        inputs = await _csqaq_inputs(user_query, market_name, float(closes[-1][1]))
    score = compute_trade_score(
        [close for _day, close in closes],
        sample_days=len(closes),
        sell_count=sell_count,
        bid_count=bid_count,
        spread_ratio=inputs.spread_ratio,
        rent_annual=inputs.rent_annual,
        platform_sell_counts=inputs.platform_sell_counts,
        hoard_ratio=await _hoard_ratio(market_name),
    )
    if score is None:
        return None

    def side(side_score: SideScore) -> dict[str, Any]:
        return {
            "total": round(side_score.total, 1),
            "note": side_score.note,
            "factors": [
                {
                    "name": factor.name,
                    "score": round(factor.score, 1),
                    "weight": round(factor.weight, 4),
                    "basis": factor.basis,
                }
                for factor in side_score.factors
            ],
        }

    return {
        "market_hash_name": market_name,
        "sample_days": score.sample_days,
        "buy": side(score.buy),
        "sell": side(score.sell),
        "disclaimer": _DISCLAIMER,
        "rewrite_note": _REWRITE_NOTE,
    }


def render_trade_score(name: str, score: TradeScore) -> str:
    """Deterministic text rendering with weights, bases and the disclaimer."""

    def _side(title: str, side: SideScore) -> list[str]:
        lines = [f"{title} {side.total:.0f}/100"]
        for factor in side.factors:
            lines.append(
                f"  · {factor.name}（权重 {factor.weight * 100:.0f}%）{factor.score:.0f} 分 —— {factor.basis}"
            )
        if side.note:
            lines.append(f"  ※ {side.note}")
        return lines

    lines = [f"{name} 买卖参考", "──────────"]
    lines.extend(_side("📈 买入参考", score.buy))
    lines.extend(_side("📉 卖出参考", score.sell))
    lines.append(f"样本：{score.sample_days} 个日线（近 90 天）")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)

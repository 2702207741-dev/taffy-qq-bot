"""Interpreted market insights over the verified fact pipeline.

Two capabilities live here:

* :func:`explain_movement` — check one item's persisted 24-hour change, then
  summarise public reasons through the news/fact pipeline with per-sentence
  ``[来源N]`` grounding.
* :func:`interpret_market` — render a short LLM reading of verified local
  breadth facts, with every output number re-validated against the input and
  a deterministic template as the fallback.

Neither capability introduces SteamDT polling: movement data comes from the
local snapshot pool; the search/fetch pipeline is the existing whitelist one.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .csqaq import csqaq_service
from .fact_sources import FactBundle, collect_fact_sources
from .index_history import index_history_store
from .llm import (
    fact_system_prompt,
    filter_verified_fact_sentences,
    try_anthropic,
    untrusted_context,
)
from .market_history import market_history
from .market_prediction import brief_history_lines
from .tz import configured_timezone

logger = logging.getLogger("taffy.market_insight")

MOVEMENT_THRESHOLD_PERCENT = 5.0
_MOVEMENT_MODEL_TOKENS = 900
_BRIEF_MODEL_TOKENS = 700

_BRIEF_SYSTEM_PROMPT = (
    "你是CS2饰品市场的资深分析师（老师人设）：有观点、敢判断、说话实在。"
    "只能使用用户消息<untrusted_context>中给出的数字，禁止使用模型记忆补充或编造任何数字。"
    "输出三部分：①盘面状态解读（1-2句，先总结强弱与结构，用给定数字支撑）；"
    '②倾向判断（1句概率化表述，如"短期偏暖/偏冷/拉锯"，只能用给定数字推导，不得自造概率数字）；'
    '③操作参考（1句，用"持有者可考虑…/空仓者可参考…"措辞，禁止"一定/必须/建议立即买入卖出"等指令性表达）。'
    "除给定数字外不得出现任何其他阿拉伯数字（包括日期），不写标题、不列来源。"
)


@dataclass(frozen=True, slots=True)
class MovementExplanation:
    kind: str
    change_percent: float | None = None
    content: str | None = None
    source_lines: tuple[str, ...] = ()
    #: Deterministic CSQAQ data lines (Steam turnover); empty on any gap.
    extra_lines: tuple[str, ...] = ()


async def _csqaq_movement_lines(market_name: str, user_query: str | None) -> tuple[str, ...]:
    """One verified CSQAQ Steam-turnover fact, or nothing on any gap.

    Every field shown must exist and the statistics period must be
    parseable; otherwise the fact is omitted entirely (fail-closed).
    """

    if not user_query:
        return ()
    try:
        response = await csqaq_service.verified_detail(user_query, market_hash_name=market_name)
    except Exception as exc:
        logger.warning("CSQAQ movement fact failed error=%s", type(exc).__name__)
        return ()
    if not response.get("success"):
        return ()
    detail = response.get("detail")
    if detail is None or detail.steam_turnover_number is None or detail.turnover_period_at is None:
        return ()
    try:
        period_text = datetime.fromtimestamp(
            detail.turnover_period_at, tz=configured_timezone()
        ).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ()
    price_part = (
        f"，成交均价 ${detail.steam_turnover_avg_price:.2f}"
        if detail.steam_turnover_avg_price is not None
        else ""
    )
    return (
        f"CSQAQ 统计：Steam 近期成交 {detail.steam_turnover_number:,} 笔{price_part}"
        f"（截至 {period_text}）",
        f"数据来源: https://csqaq.com/goods/{detail.good_id}",
    )


async def explain_movement(
    market_name: str, *, user_query: str | None = None
) -> MovementExplanation:
    """Explain one item's 24h movement from local data plus verified sources."""

    movement = await market_history.item_change(market_name)
    if movement is None:
        return MovementExplanation(kind="no_data")
    _latest, change = movement
    if abs(change) < MOVEMENT_THRESHOLD_PERCENT:
        return MovementExplanation(kind="no_movement", change_percent=change)

    direction = "上涨" if change > 0 else "下跌"

    async def _collect_bundle() -> FactBundle | None:
        try:
            return await collect_fact_sources(f"{market_name} {direction} 原因 CS2", "market")
        except Exception as exc:
            logger.warning("Movement fact collection failed error=%s", type(exc).__name__)
            return None

    bundle, csqaq_lines = await asyncio.gather(
        _collect_bundle(),
        _csqaq_movement_lines(market_name, user_query),
    )
    if bundle is None or not bundle.is_verified or not bundle.sources:
        return MovementExplanation(
            kind="no_sources", change_percent=change, extra_lines=csqaq_lines
        )

    user_content = (
        f"用户问题：{market_name} 近24小时{direction} {change:+.2f}%，"
        "请依据证据正文说明可能的原因，逐句标注[来源N]。\n\n"
        f"{untrusted_context(bundle.render_context(), limit=7000)}"
    )
    try:
        content = await try_anthropic(
            fact_system_prompt(),
            [{"role": "user", "content": user_content}],
            _MOVEMENT_MODEL_TOKENS,
        )
    except Exception as exc:
        logger.warning("Movement interpretation call failed error=%s", type(exc).__name__)
        return MovementExplanation(kind="no_sources", change_percent=change)

    validation = filter_verified_fact_sentences(content, bundle)
    if not validation.valid_sentences:
        logger.info(
            "Movement interpretation rejected valid=0 citations=%d grounding=%d",
            validation.rejected_citations,
            validation.rejected_grounding,
        )
        return MovementExplanation(
            kind="no_sources", change_percent=change, extra_lines=csqaq_lines
        )
    return MovementExplanation(
        kind="explained",
        change_percent=change,
        content=validation.content,
        source_lines=_cited_sources(bundle, validation.content),
        extra_lines=csqaq_lines,
    )


def _cited_sources(bundle: FactBundle, content: str) -> tuple[str, ...]:
    cited = sorted(
        {
            int(match.group(1))
            for match in re.finditer(r"\[来源(\d+)\]", content)
            if 1 <= int(match.group(1)) <= len(bundle.sources)
        }
    )
    return tuple(
        f"[来源{number}] {bundle.sources[number - 1].title}（{bundle.sources[number - 1].domain}）"
        for number in cited
    )


@dataclass(frozen=True, slots=True)
class MarketBrief:
    kind: str
    content: str = ""


async def _brief_facts() -> dict[str, Any] | None:
    closes = await index_history_store.load_closes()
    breadth = await market_history.breadth()
    movers = await market_history.top_movers(3)
    if len(closes) < 2 and breadth is None:
        return None
    facts: dict[str, Any] = {}
    if len(closes) >= 2:
        facts["latest_close"] = closes[-1][1]
        facts["close_change_percent"] = (closes[-1][1] / closes[-2][1] - 1) * 100
    if breadth is not None:
        facts["rising_ratio"] = breadth.rising_ratio
        facts["median_change"] = breadth.median_change
        facts["compared"] = breadth.compared
        facts["sell_depth"] = breadth.sell_depth
        facts["bid_depth"] = breadth.bid_depth
    if movers:
        facts["top_movers"] = [(name, change) for name, change, _price, _time in movers]
    return facts


def _brief_allowed_numbers(facts: dict[str, Any]) -> set[float]:
    allowed: set[float] = set()
    for key in (
        "latest_close",
        "close_change_percent",
        "rising_ratio",
        "median_change",
        "compared",
        "sell_depth",
        "bid_depth",
    ):
        if key in facts:
            allowed.add(round(float(facts[key]), 2))
    for _name, change in facts.get("top_movers", ()):
        allowed.add(round(float(change), 2))
    return allowed


def _numbers_only_from_input(content: str, allowed: set[float]) -> bool:
    """Reject any Arabic numeral in ``content`` that is not an input number.

    Magnitudes are compared (sign-insensitive) so "0.22%" remains valid when
    the input fact was ``-0.22``.
    """

    for match in re.finditer(r"\d+(?:\.\d+)?", content):
        value = abs(float(match.group(0)))
        if not any(abs(value - abs(candidate)) < 0.005 for candidate in allowed):
            logger.info("Brief number rejected value=%s", match.group(0))
            return False
    return True


def _template_outlook(facts: dict[str, Any]) -> str:
    """Data-driven fallback conclusion; never call a green day cold."""

    if "rising_ratio" in facts:
        rising_ratio = float(facts["rising_ratio"])
        median_change = float(facts.get("median_change", 0.0) or 0.0)
        if rising_ratio >= 60 and median_change > 0:
            return "老师视角：短期动能偏暖——持有者可考虑分批设好心理价位，空仓者注意追高风险。"
        if rising_ratio <= 40 and median_change < 0:
            return (
                "老师视角：短期动能偏弱，倾向偏冷——"
                "持有者可考虑设好心理价位，空仓者参考等待企稳信号。"
            )
        return "老师视角：多空拉锯、方向未明——持有者可考虑设好心理价位，空仓者等待方向明朗。"
    change = float(facts.get("close_change_percent", 0.0) or 0.0)
    if change > 0:
        return "老师视角：大盘收红但样本覆盖有限——持有者可考虑设好心理价位，空仓者注意追高风险。"
    if change < 0:
        return (
            "老师视角：短期动能偏弱，倾向偏冷——持有者可考虑设好心理价位，空仓者参考等待企稳信号。"
        )
    return "老师视角：大盘走平、方向未明——持有者可考虑设好心理价位，空仓者等待方向明朗。"


def _template_brief(facts: dict[str, Any]) -> str:
    lines: list[str] = []
    if "latest_close" in facts:
        lines.append(
            f"大盘收于 ¥{facts['latest_close']:.2f}，"
            f"较前一收盘日 {float(facts['close_change_percent']):+.2f}%。"
        )
    if "rising_ratio" in facts:
        lines.append(
            f"同样本 {int(facts['compared'])} 件中上涨占比 {float(facts['rising_ratio']):.1f}%，"
            f"中位涨跌 {float(facts['median_change']):+.2f}%。"
        )
        lines.append(
            f"挂单深度：在售 {int(facts['sell_depth'])} / 求购 {int(facts['bid_depth'])}。"
        )
    movers = facts.get("top_movers")
    if movers:
        names = "、".join(f"{name} {change:+.2f}%" for name, change in movers[:3])
        lines.append(f"近期波动较大的有 {names}。")
    lines.append(_template_outlook(facts))
    lines.append("以上为本地快照统计，历史情景概率，不构成交易建议。")
    return "\n".join(lines)


async def _history_suffix() -> str:
    """Deterministic history block; ``""`` on any gap (brief stays served)."""

    try:
        closes = await index_history_store.load_closes()
        lines = brief_history_lines(dict(closes))
    except Exception as exc:
        logger.warning("Brief history block failed error=%s", type(exc).__name__)
        return ""
    if not lines:
        return ""
    return "\n" + "\n".join(lines)


async def interpret_market() -> MarketBrief:
    """Render a short market brief; every number must come from local facts."""

    facts = await _brief_facts()
    if facts is None:
        return MarketBrief(kind="no_data")
    allowed = _brief_allowed_numbers(facts)
    user_content = (
        "请基于以下已验证的本地行情事实写盘面解读（3到5句，只能使用这些数字）：\n"
        f"{untrusted_context(_facts_text(facts))}"
    )
    history_suffix = await _history_suffix()
    for _attempt in range(2):
        try:
            content = await try_anthropic(
                _BRIEF_SYSTEM_PROMPT,
                [{"role": "user", "content": user_content}],
                _BRIEF_MODEL_TOKENS,
            )
        except Exception as exc:
            logger.warning("Brief interpretation call failed error=%s", type(exc).__name__)
            break
        content = content.strip()
        if content and _numbers_only_from_input(content, allowed):
            return MarketBrief(kind="llm", content=content + history_suffix)
    return MarketBrief(kind="template", content=_template_brief(facts) + history_suffix)


def _facts_text(facts: dict[str, Any]) -> str:
    parts: list[str] = []
    if "latest_close" in facts:
        parts.append(
            f"大盘最新收盘：{float(facts['latest_close']):.2f}；"
            f"较前一收盘日涨跌：{float(facts['close_change_percent']):+.2f}%"
        )
    if "rising_ratio" in facts:
        parts.append(
            f"上涨占比：{float(facts['rising_ratio']):.1f}%（同样本 {int(facts['compared'])} 件）；"
            f"中位涨跌：{float(facts['median_change']):+.2f}%；"
            f"在售挂单 {int(facts['sell_depth'])}；求购挂单 {int(facts['bid_depth'])}"
        )
    movers = facts.get("top_movers")
    if movers:
        parts.append(
            "近期波动较大：" + "、".join(f"{name} {change:+.2f}%" for name, change in movers[:3])
        )
    return "\n".join(parts)


__all__ = [
    "MOVEMENT_THRESHOLD_PERCENT",
    "MarketBrief",
    "MovementExplanation",
    "explain_movement",
    "interpret_market",
]

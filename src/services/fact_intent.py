"""Conservative routing policy for CS2 claims that require real evidence.

The classifier intentionally prefers an unnecessary lookup over an
unsupported answer.  It does not decide whether a claim is true; it only
decides when the chat model is forbidden from answering without a data source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class FactKind(StrEnum):
    PRICE = "price"
    MARKET = "market"
    TEAM_PLAYER = "team_player"
    VERSION = "version"
    EVENT = "event"
    NEWS = "news"
    CS2_GENERAL = "cs2_general"


@dataclass(frozen=True, slots=True)
class FactIntent:
    kind: FactKind
    minimum_sources: int
    source_label: str
    authoritative_domains: tuple[str, ...] = ()


_VALVE_PRIMARY_DOMAINS = (
    "counter-strike.net",
    "store.steampowered.com",
    "steamcommunity.com",
)
_EVENT_PRIMARY_DOMAINS = (
    "blast.tv",
    "esl.com",
    "eslfaceitgroup.com",
    "pgl-esports.com",
)


_QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "么",
    "谁",
    "哪个",
    "哪里",
    "多少",
    "什么",
    "怎样",
    "如何",
    "为何",
    "为什么",
    "是不是",
    "有没有",
    "现在",
    "目前",
    "最新",
)
_PRICE_MARKERS = (
    "价格",
    "多少钱",
    "值多少",
    "报价",
    "在售",
    "收购价",
    "求购价",
    "均价",
)
_MARKET_FACT_MARKERS = (
    "大盘",
    "行情",
    "市场指数",
    "饰品市场",
    "饰品趋势",
    "价格趋势",
    "抄底",
    "囤货",
)
_NEWS_MARKERS = ("新闻", "快讯", "今日cs", "cs2资讯", "最新消息")
_VERSION_MARKERS = (
    "版本",
    "更新日志",
    "补丁",
    "patch",
    "改动",
    "削弱",
    "加强",
    "地图池",
    "active duty",
)
_BROAD_UPDATE_MARKERS = ("更新", "更新了什么", "最近改了什么")
_EVENT_MARKERS = (
    "赛事",
    "比赛",
    "比分",
    "赛程",
    "冠军",
    "决赛",
    "major",
    "iem",
    "blast",
    "pgl",
    "esl",
    "epl",
)
_TEAM_PLAYER_MARKERS = (
    "战队",
    "队伍",
    "阵容",
    "选手",
    "职业哥",
    "教练",
    "转会",
    "签约",
    "加入",
    "离队",
    "效力",
    "退役",
    "替补",
    "bench",
    "roster",
    "transfer",
)
_CS2_MARKERS = ("cs2", "csgo", "cs:go", "反恐精英", "hltv", "v社")
_CS_GAMEPLAY_MARKERS = (
    "tick",
    "tickrate",
    "subtick",
    "rating",
    "elo",
    "vac",
    "prime",
    "急停",
    "压枪",
    "弹道",
    "烟雾弹",
    "闪光弹",
    "燃烧弹",
    "投掷物",
    "经济局",
    "经济机制",
    "经济系统",
    "长枪局",
    "强起",
    "eco",
    "force buy",
    "起枪",
    "拆弹",
    "护甲",
    "头盔",
    "穿甲",
    "爆头",
    "一枪头",
    "伤害",
    "射速",
    "移速",
    "准星",
    "烟",
    "雷",
    "道具",
    "控制台",
    "指令",
    "段位",
    "荒漠迷城",
    "炼狱小镇",
    "炙热沙城",
    "沙二",
    "远古遗迹",
    "核子危机",
    "殒命大厦",
    "死亡游乐园",
    "阿努比斯",
    "列车停放站",
    "dust2",
    "mirage",
    "inferno",
    "nuke",
    "ancient",
    "anubis",
    "overpass",
    "train",
    "vertigo",
    "ak-47",
    "awp",
    "m4a1",
    "m4a4",
    "沙鹰",
    "usp",
    "glock",
)
_INSTRUCTION_MARKERS = (
    "怎么",
    "能不能",
    "给我",
    "来一套",
    "一套",
    "教我",
    "讲讲",
    "讲一下",
    "说一下",
    "解释",
    "介绍",
    "分析一下",
    "怎么看",
)
_CLAIM_MARKERS = (
    "听说",
    "据说",
    "有人说",
    "是不是",
    "应该是",
    "等于",
    "是",
)
_CASUAL_PREFERENCE_MARKERS = (
    "你喜欢",
    "你想不想",
    "陪我玩",
    "一起玩",
    "我喜欢",
    "我讨厌",
)
_PLAYER_ROSTER_PATTERN = re.compile(
    r"(?<![a-z0-9_.-])[a-z][a-z0-9_.-]{1,23}(?![a-z0-9_.-]).{0,20}"
    r"(?:在哪|哪队|去哪|加入|加盟|转会|离队|效力|退役)",
    re.IGNORECASE,
)
_PLAYER_QUESTION_PATTERN = re.compile(
    r"(?<![a-z0-9_.-])[a-z][a-z0-9_.-]{1,23}(?![a-z0-9_.-]).{0,12}"
    r"(?:是谁|哪个队|什么队|什么战队|国籍|年龄|rating|数据)",
    re.IGNORECASE,
)
_ROSTER_CLAIM_PATTERN = re.compile(
    r"(?:加入|加盟|转会|签约|去了|进了|进)(?:.{0,16})"
    r"(?:spirit|falcons|faze|vitality|navi|g2|mouz|liquid|绿龙|小蜜蜂|老鼠|液体)",
    re.IGNORECASE,
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def detect_fact_intents(message: str) -> tuple[FactIntent, ...]:
    """Return every independent evidence policy needed for *message*.

    News always needs two independent domains. Version/general CS2 facts may
    use one Valve primary page, and event facts may use one organiser primary
    page; otherwise two independent domains are required. Player/roster facts
    require two independent domains because the allowlist has no stable team
    primary-source set. Market prices are satisfied only by the dedicated
    quote path, never by web snippets or model memory. Broader market questions
    are redirected to deterministic commands instead of the chat model.
    """

    text = " ".join(str(message).strip().casefold().split())
    if not text:
        return ()

    if _contains_any(text, _PRICE_MARKERS):
        return (FactIntent(FactKind.PRICE, 1, "市场实时报价"),)
    if _contains_any(text, _MARKET_FACT_MARKERS):
        return (FactIntent(FactKind.MARKET, 1, "机器人实时市场命令"),)

    cs2_context = _contains_any(text, _CS2_MARKERS + _CS_GAMEPLAY_MARKERS)
    intents: list[FactIntent] = []
    if _contains_any(text, _VERSION_MARKERS) or (
        cs2_context and _contains_any(text, _BROAD_UPDATE_MARKERS)
    ):
        intents.append(
            FactIntent(
                FactKind.VERSION,
                2,
                "Valve/Steam 官方公告或两个独立可靠来源",
                _VALVE_PRIMARY_DOMAINS,
            )
        )
    if _contains_any(text, _EVENT_MARKERS):
        intents.append(
            FactIntent(
                FactKind.EVENT,
                2,
                "赛事主办方官网或两个独立可靠赛果页",
                _EVENT_PRIMARY_DOMAINS,
            )
        )
    if (
        _contains_any(text, _TEAM_PLAYER_MARKERS)
        or _PLAYER_ROSTER_PATTERN.search(text)
        or _PLAYER_QUESTION_PATTERN.search(text)
        or _ROSTER_CLAIM_PATTERN.search(text)
    ):
        intents.append(FactIntent(FactKind.TEAM_PLAYER, 2, "两个独立可靠阵容页"))

    if intents:
        return tuple(intents)
    if _contains_any(text, _NEWS_MARKERS):
        return (FactIntent(FactKind.NEWS, 2, "官方页面/可靠媒体"),)

    if _contains_any(text, _CASUAL_PREFERENCE_MARKERS):
        return ()
    asks_question = _contains_any(
        text,
        _QUESTION_MARKERS + _INSTRUCTION_MARKERS + _CLAIM_MARKERS,
    )
    if asks_question and cs2_context:
        return (
            FactIntent(
                FactKind.CS2_GENERAL,
                2,
                "Valve 官方页面或两个独立可靠来源",
                _VALVE_PRIMARY_DOMAINS,
            ),
        )
    return ()


def detect_fact_intent(message: str) -> FactIntent | None:
    """Compatibility helper returning the first required evidence policy."""

    intents = detect_fact_intents(message)
    return intents[0] if intents else None


def requires_verified_sources(message: str) -> bool:
    """Small compatibility helper for routing and tests."""

    return detect_fact_intent(message) is not None


__all__ = [
    "FactIntent",
    "FactKind",
    "detect_fact_intent",
    "detect_fact_intents",
    "requires_verified_sources",
]

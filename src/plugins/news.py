"""Fast deterministic CS2 update and tournament command."""

from __future__ import annotations

import asyncio
import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.official_cs2 import format_official_report, official_cs2
from src.services.v4_policy import v4_enabled_for_user

from .feature_gate import audited_finish, check_feature

_log = logging.getLogger("taffy.news")
_RATE_LIMITER = SlidingWindowLimiter(6, 300)
_EXACT_COMMANDS = {
    "CS2新闻",
    "cs2新闻",
    "最新更新",
    "版本更新",
    "新箱子",
    "新胶囊",
    "新收藏品",
    "CS2更新",
    "饰品新闻",
    "市场动态",
    "CS2资讯",
    "CS2赛事",
    "赛事消息",
    "近期赛事",
}
_MATCH_MARKERS = ("赛事", "比赛", "赛程", "赛果", "战队")
_UPDATE_MARKERS = ("更新", "版本", "补丁", "箱子", "胶囊", "收藏品", "新闻", "资讯")


def _is_news_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return (
        text in _EXACT_COMMANDS
        or text.startswith("CS2新闻 ")
        or text.startswith("cs2新闻 ")
        or text.startswith("搜索cs2最近")
        or text.startswith("搜索CS2最近")
    )


def requested_sections(query: str) -> tuple[bool, bool]:
    include_matches = any(marker in query for marker in _MATCH_MARKERS)
    include_updates = any(marker in query for marker in _UPDATE_MARKERS)
    if not include_matches and not include_updates:
        return True, True
    return include_updates, include_matches


news = on_message(rule=Rule(_is_news_request), priority=8, block=True)


@news.handle()
async def handle_news(event: MessageEvent) -> None:
    query = event.get_plaintext().strip()[:200]
    if not await check_feature(news, event, "news"):
        return
    if not v4_enabled_for_user(event.user_id):
        await audited_finish(
            news, event, "news", "官方更新与赛事新链路正在由管理员试用，三天验收后再开放。"
        )
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await audited_finish(news, event, "news", "新闻查询太频繁了，请稍后再试。")
    include_updates, include_matches = requested_sections(query)
    try:
        task = asyncio.create_task(
            official_cs2.report(
                include_updates=include_updates,
                include_matches=include_matches,
            )
        )
        try:
            report = await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except TimeoutError:
            await news.send(text_message("正在核对官方更新和赛事数据，马上好。"))
            report = await task
        content = format_official_report(
            report,
            asks_benefit=any(marker in query for marker in ("利好", "好消息", "积极")),
        )
        await audited_finish(news, event, "news", content)
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Official CS2 request failed error=%s", type(exc).__name__)
        await audited_finish(
            news, event, "news", "官方更新与赛事数据暂时不可用，本次不让模型补写。"
        )


__all__ = ["requested_sections"]

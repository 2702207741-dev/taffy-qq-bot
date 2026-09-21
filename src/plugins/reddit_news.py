"""Community speculation command backed by Reddit RSS feeds."""

from __future__ import annotations

import asyncio
import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.reddit_news import RedditNewsUnavailable, reddit_news_service

from .feature_gate import audited_finish, check_feature

_log = logging.getLogger("taffy.reddit_news")
_RATE_LIMITER = SlidingWindowLimiter(6, 300)
_EXACT_COMMANDS = {
    "CS2爆料",
    "cs2爆料",
    "社区爆料",
    "社区资讯",
}


def _is_reddit_news_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    return event.get_plaintext().strip() in _EXACT_COMMANDS


reddit_news_matcher = on_message(rule=Rule(_is_reddit_news_request), priority=8, block=True)


@reddit_news_matcher.handle()
async def handle_reddit_news(event: MessageEvent) -> None:
    if not await check_feature(reddit_news_matcher, event, "news"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await audited_finish(
            reddit_news_matcher, event, "news", "社区资讯查询太频繁了，请稍后再试。"
        )
    service = reddit_news_service()
    try:
        task = asyncio.create_task(service.community_report())
        try:
            report = await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except TimeoutError:
            await reddit_news_matcher.send(text_message("正在拉取 Reddit 社区爆料，稍等一下。"))
            report = await task
        await audited_finish(reddit_news_matcher, event, "news", report)
    except MatcherException:
        raise
    except RedditNewsUnavailable:
        await audited_finish(
            reddit_news_matcher,
            event,
            "news",
            "社区资讯暂时不可用（Reddit 限流或网络故障），本次不让模型补写。",
        )
    except Exception as exc:
        _log.exception("Community news request failed error=%s", type(exc).__name__)
        await audited_finish(
            reddit_news_matcher,
            event,
            "news",
            "社区资讯暂时不可用，本次不让模型补写。",
        )


__all__ = ["_EXACT_COMMANDS"]

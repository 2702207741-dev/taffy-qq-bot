"""Per-user CS2 watchlist commands backed by transactional SQLite."""

from __future__ import annotations

import asyncio
import logging
import time

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.exception import MatcherException
from nonebot.rule import Rule

from src.core.rate_limit import SlidingWindowLimiter
from src.services.market_validation import (
    MarketDataError,
    format_data_time,
    validate_quote_response,
)
from src.state.runtime import watchlists
from src.state.watchlist_repository import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_ITEMS_PER_OWNER,
    WatchlistQuotaExceededError,
)

from .chat_utils import st_get
from .feature_gate import audited_finish, check_feature

_log = logging.getLogger("taffy.watchlist")
_MAX_ITEMS = MAX_ITEMS_PER_OWNER
_MAX_NAME_LENGTH = MAX_DISPLAY_NAME_LENGTH
_QUERY_LIMITER = SlidingWindowLimiter(10, 60)
_COMMAND_LIMITER = SlidingWindowLimiter(30, 60)
_PLATFORM_NAMES = {
    "BUFF": "BUFF",
    "YOUPIN": "悠悠",
    "C5": "C5",
    "STEAM": "Steam",
    "HALOSKINS": "HALO",
}


def _is_watchlist_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    message = event.get_plaintext().strip()
    return message in {"自选", "我的自选", "自选查价"} or message.startswith(("加自选", "删自选"))


watch_cmd = on_message(rule=Rule(_is_watchlist_request), priority=7, block=True)


def _format_prices(data: dict, *, now: float | None = None) -> str:
    try:
        quotes = validate_quote_response(data, now=now)
    except MarketDataError as exc:
        return exc.user_message
    timestamp = min(quote.timestamp for quote in quotes)
    values = [
        f"{_PLATFORM_NAMES.get(quote.platform, quote.platform)}: ¥{quote.sell_price:.2f}"
        for quote in quotes[:4]
    ]
    return f"数据时间: {format_data_time(timestamp)}\n  " + " | ".join(values)


@watch_cmd.handle()
async def handle_watchlist(_bot: Bot, event: MessageEvent) -> None:
    message = event.get_plaintext().strip()
    owner_user_id = int(event.user_id)
    if not await check_feature(watch_cmd, event, "watchlist"):
        return
    limiter_key = str(owner_user_id)
    if not await _COMMAND_LIMITER.allow(limiter_key):
        await audited_finish(watch_cmd, event, "watchlist", "自选操作太频繁了，请稍后再试。")

    try:
        mine = await watchlists.list_owner(owner_user_id)

        if message in {"自选", "我的自选"}:
            if not mine:
                await audited_finish(
                    watch_cmd, event, "watchlist", "你还没有自选饰品。用“加自选 饰品名”添加。"
                )
            lines = ["⭐ 我的自选"]
            lines.extend(f"  {index}. {item.display_name}" for index, item in enumerate(mine, 1))
            await audited_finish(watch_cmd, event, "watchlist", "\n".join(lines))

        if message == "自选查价":
            if not await _QUERY_LIMITER.allow(limiter_key):
                await audited_finish(watch_cmd, event, "watchlist", "查价太频繁了，请稍后再试。")
            if not mine:
                await audited_finish(watch_cmd, event, "watchlist", "你还没有自选饰品。")
            selected = mine[:5]
            results = await asyncio.gather(
                *(
                    st_get(
                        "/open/cs2/v1/price/single",
                        {"marketHashName": item.display_name},
                    )
                    for item in selected
                )
            )
            now = time.time()
            lines = [
                f"🔍 {item.display_name}\n  {_format_prices(data, now=now)}"
                for item, data in zip(selected, results, strict=True)
            ]
            if len(mine) > len(selected):
                lines.append(f"仅展示前 {len(selected)} 项，避免一次请求过多。")
            await audited_finish(watch_cmd, event, "watchlist", "\n\n".join(lines))

        if message.startswith("加自选"):
            query = message.removeprefix("加自选").strip()
            if not query or len(query) > _MAX_NAME_LENGTH:
                await audited_finish(
                    watch_cmd,
                    event,
                    "watchlist",
                    f"饰品名不能为空且不能超过 {_MAX_NAME_LENGTH} 个字符。",
                )
            from .chat import resolve_name

            resolved = await resolve_name(query)
            if not resolved:
                await audited_finish(
                    watch_cmd, event, "watchlist", "未识别这个饰品，请使用完整市场名或常用昵称。"
                )
            try:
                added = await watchlists.add(owner_user_id, resolved)
            except WatchlistQuotaExceededError:
                await audited_finish(
                    watch_cmd, event, "watchlist", f"每人最多保存 {_MAX_ITEMS} 个自选饰品。"
                )
            action = "已添加" if added.created else "已经存在"
            await audited_finish(
                watch_cmd,
                event,
                "watchlist",
                f"⭐ {action}: {added.item.display_name}\n当前共 {added.count} 个",
            )

        if message.startswith("删自选"):
            raw_index = message.removeprefix("删自选").strip()
            if not raw_index.isdecimal() or int(raw_index) < 1:
                await audited_finish(
                    watch_cmd, event, "watchlist", "格式: 删自选 1（编号见“我的自选”）"
                )
            removed = await watchlists.remove_at(owner_user_id, int(raw_index) - 1)
            if removed is None:
                await audited_finish(
                    watch_cmd, event, "watchlist", "没有这个编号，请先查看“我的自选”。"
                )
            await audited_finish(watch_cmd, event, "watchlist", f"已移除: {removed.display_name}")
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception(
            "Watchlist command failed user=%s error=%s",
            owner_user_id,
            type(exc).__name__,
        )
        await audited_finish(watch_cmd, event, "watchlist", "自选数据暂时不可用，请稍后再试。")

"""Compare two CS2 item prices."""

from __future__ import annotations

import asyncio
import re
import time

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.rule import Rule

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.market_validation import (
    MarketDataError,
    ValidatedQuote,
    format_data_time,
    validate_quote_response,
)

from .chat_utils import st_get
from .feature_gate import audited_finish, check_feature

PLATFORM_NAMES = {
    "BUFF": "BUFF",
    "YOUPIN": "悠悠",
    "C5": "C5",
    "STEAM": "Steam",
    "HALOSKINS": "HALO",
}
_RATE_LIMITER = SlidingWindowLimiter(10, 60)


def _is_compare_request(event: Event) -> bool:
    return isinstance(
        event, (GroupMessageEvent, PrivateMessageEvent)
    ) and event.get_plaintext().strip().startswith("对比 ")


compare = on_message(rule=Rule(_is_compare_request), priority=7, block=True)


def _parse_names(message: str) -> tuple[str, str] | None:
    body = message.removeprefix("对比").strip()
    parts = re.split(r"\s+(?:vs|VS|和|与)\s+", body, maxsplit=1)
    if len(parts) != 2:
        return None
    first, second = (part.strip() for part in parts)
    if not first or not second or len(first) > 120 or len(second) > 120:
        return None
    return first, second


def _quotes(data: dict, *, now: float) -> tuple[ValidatedQuote, ...]:
    return validate_quote_response(data, now=now)


def _format_price(quotes: tuple[ValidatedQuote, ...], label: str) -> str:
    timestamp = min(quote.timestamp for quote in quotes)
    rows = [
        f"  {PLATFORM_NAMES.get(quote.platform, quote.platform)}: ¥{quote.sell_price:.2f}"
        for quote in quotes[:5]
    ]
    return f"🔍 {label}\n  数据时间: {format_data_time(timestamp)}\n" + "\n".join(rows)


def _buff_price(quotes: tuple[ValidatedQuote, ...]) -> float:
    match = next((quote for quote in quotes if quote.platform == "BUFF"), None)
    return match.sell_price if match is not None else 0.0


@compare.handle()
async def handle_compare(event: MessageEvent) -> None:
    if not await check_feature(compare, event, "compare"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await audited_finish(compare, event, "compare", "对比查询太频繁了，请稍后再试。")
    parsed = _parse_names(event.get_plaintext().strip())
    if parsed is None:
        await compare.finish(
            text_message("格式: 对比 饰品A vs 饰品B\n例如: 对比 红线 久经 vs 印花集 崭新")
        )

    first_query, second_query = parsed
    from .chat import resolve_name

    first_name, second_name = await asyncio.gather(
        resolve_name(first_query), resolve_name(second_query)
    )
    if not first_name or not second_name:
        await audited_finish(
            compare, event, "compare", "有饰品名称无法识别，请使用完整市场名或常用昵称。"
        )

    first_data, second_data = await asyncio.gather(
        st_get("/open/cs2/v1/price/single", {"marketHashName": first_name}),
        st_get("/open/cs2/v1/price/single", {"marketHashName": second_name}),
    )
    now = time.time()
    try:
        first_quotes = _quotes(first_data, now=now)
        second_quotes = _quotes(second_data, now=now)
    except MarketDataError as exc:
        await audited_finish(
            compare, event, "compare", f"⚠️ 对比已停止：{exc.user_message.removeprefix('⚠️ ')}"
        )
    result = (
        f"{_format_price(first_quotes, first_name)}\n\n{_format_price(second_quotes, second_name)}"
    )

    first_buff = _buff_price(first_quotes)
    second_buff = _buff_price(second_quotes)
    if first_buff and second_buff:
        difference = first_buff - second_buff
        result += f"\n\n📊 BUFF差价: ¥{difference:.0f} ({difference / second_buff * 100:+.1f}%)"

    await audited_finish(compare, event, "compare", result)

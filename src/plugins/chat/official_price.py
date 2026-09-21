"""Official Steam market price command with SteamDT cross-check."""

from __future__ import annotations

import logging
import re
from typing import NoReturn

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

from src.core.messages import text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services.market_validation import validate_quote_response
from src.services.steam_official_price import OfficialPrice, get_official_price

from ..feature_gate import check_feature, record_event_usage

_log = logging.getLogger("taffy.official_price")
_RATE_LIMITER = SlidingWindowLimiter(10, 60)


def _is_official_price_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text = event.get_plaintext().strip()
    return text.startswith("官方价 ") and len(text) > 4


official_price_cmd = on_message(rule=Rule(_is_official_price_request), priority=7, block=True)


async def _finish(event: MessageEvent, text: str) -> NoReturn:
    await record_event_usage(
        event=event,
        feature="market",
        input_text=event.get_plaintext().strip(),
        output_text=text,
    )
    await official_price_cmd.finish(text_message(text))
    raise RuntimeError("matcher.finish returned unexpectedly")


def _parse_price(value: str) -> float | None:
    """Parse ``¥12.34`` / ``12,34`` / ``1,234.5`` style prices."""

    cleaned = value.replace("¥", "").replace("€", "").strip()
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if match is None:
        return None
    try:
        result = float(match.group(0))
    except ValueError:
        return None
    return result if result > 0 else None


def render_official_price(
    name: str,
    official: OfficialPrice,
    steamdt_price: float | None,
) -> str:
    """Render the official price block with an optional SteamDT cross-check."""

    lines = [
        "🔍 Steam 官方市场价",
        "────────",
        name,
    ]
    if official.lowest_price:
        lines.append(f"最低价: {official.lowest_price}")
    if official.median_price:
        lines.append(f"中位价: {official.median_price}")
    if official.volume:
        lines.append(f"24h 成交量: {official.volume} 件")
    lines.append("────────")
    median = _parse_price(official.median_price)
    if steamdt_price is not None and median is not None:
        delta = (median - steamdt_price) / steamdt_price * 100
        lines.append(f"📊 对照 SteamDT: ¥{steamdt_price:.2f}（官方 vs 平台 {delta:+.1f}%）")
    elif steamdt_price is not None:
        lines.append(f"📊 对照 SteamDT: ¥{steamdt_price:.2f}（官方中位价未解析）")
    lines.append("Steam 官方市场价，仅供参考")
    return "\n".join(lines)


def render_official_price_fallback(name: str, steamdt_price: float) -> str:
    """Render the degraded official-price block from SteamDT-sourced data."""

    return (
        "🔍 Steam 官方市场价（经 SteamDT 数据）\n"
        "────────\n"
        f"{name}\n"
        f"Steam 官方市场: ¥{steamdt_price:.2f}\n"
        "────────\n"
        "ℹ️ 官方接口暂时不可用，以上价格来自 SteamDT 采集的 Steam 官方市场数据\n"
        "Steam 官方市场价，仅供参考"
    )


@official_price_cmd.handle()
async def handle_official_price(_bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not await check_feature(official_price_cmd, event, "market"):
        return
    if not await _RATE_LIMITER.allow(str(event.user_id)):
        await _finish(event, "官方价查询太频繁了，请稍后再试。")
    query = text.removeprefix("官方价").strip()
    try:
        from . import resolve_name

        market_name = await resolve_name(query)
        if market_name is None:
            from .market import resolution_failure_message

            await _finish(event, resolution_failure_message(query))

        steamdt_price: float | None = None
        from ..chat_utils import st_get

        try:
            response = await st_get(
                "/open/cs2/v1/price/single",
                {"marketHashName": market_name},
            )
            quotes = validate_quote_response(response)
            for quote in quotes:
                if quote.platform == "STEAM":
                    steamdt_price = float(quote.sell_price)
                    break
        except Exception as exc:
            _log.warning(
                "SteamDT cross-check unavailable name=%s error=%s",
                market_name[:40],
                type(exc).__name__,
            )

        official = await get_official_price(market_name)
        if official is None:
            if steamdt_price is not None:
                await _finish(event, render_official_price_fallback(market_name, steamdt_price))
            await _finish(event, "Steam 官方价暂时不可用（接口限流或异常），请稍后再试。")
        await _finish(event, render_official_price(market_name, official, steamdt_price))
    except MatcherException:
        raise
    except Exception as exc:
        _log.exception("Official price command failed error=%s", type(exc).__name__)
        await _finish(event, "官方价这次没查到，请稍后再试。")


__all__ = ["handle_official_price", "official_price_cmd", "render_official_price"]

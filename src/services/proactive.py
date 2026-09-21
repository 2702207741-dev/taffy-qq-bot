"""Opt-in proactive services: morning digest and watchlist dip notices.

Everything is strictly local: the watchlist table, the hourly
``quote_snapshots`` store and the index daily closes.  No SteamDT request,
no LLM call.  Pushes are gated three ways before any send — the global
``TAFFY_PROACTIVE_ENABLED`` switch (default off), the per-user opt-in
recorded through the ``早盘提醒`` command, and the per-user daily cap —
and every outcome (delivered / skipped / failed, with a reason) is written
to the ``proactive_deliveries`` ledger for deduplication and audit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone

from .config import bounded_env_int, env_bool
from .index_history import index_history_store
from .market_history import market_history

logger = logging.getLogger("taffy.proactive")

_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_MAX_WATCH_ITEMS = 5
_SEND_TIMEOUT_SECONDS = 20.0
_DIP_DAYS = 7
_DIP_RATIO = 0.95


def proactive_enabled() -> bool:
    """Master switch; ships off so nobody is pushed at without consent."""

    return env_bool("TAFFY_PROACTIVE_ENABLED", False)


def proactive_daily_cap() -> int:
    return bounded_env_int("TAFFY_PROACTIVE_DAILY_CAP", 2, 0, 10)


def proactive_morning_hour() -> int:
    return bounded_env_int("TAFFY_PROACTIVE_MORNING_HOUR", 9, 0, 23)


def _local_day_bounds(current: int) -> tuple[str, int]:
    """Return (YYYYMMDD key, local-midnight timestamp) for ``current``."""

    local = datetime.fromtimestamp(current, _MARKET_TIMEZONE)
    midnight = datetime(local.year, local.month, local.day, tzinfo=_MARKET_TIMEZONE)
    return local.strftime("%Y%m%d"), int(midnight.timestamp())


async def _market_overview_line() -> str:
    """Reuse the daily-report index data source (local closes only)."""

    try:
        closes = await index_history_store.load_closes()
    except Exception as exc:
        logger.warning("Index closes unavailable error=%s", type(exc).__name__)
        return "大盘：日线数据积累中"
    if len(closes) < 2:
        return "大盘：日线数据积累中"
    latest_date, latest_close = closes[-1]
    previous_close = closes[-2][1]
    change = (latest_close / previous_close - 1) * 100
    return (
        f"大盘：¥{latest_close:.2f}（较前一收盘日 {change:+.2f}%，截至 {latest_date.isoformat()}）"
    )


async def _render_morning(
    day_text: str,
    overview: str,
    priced: Sequence[tuple[str, Mapping[str, float]]],
) -> str:
    lines = [f"🌅 【塔菲早盘提醒】 {day_text}", overview, "⭐ 自选行情（本地快照）："]
    for index, (name, snapshot) in enumerate(priced, 1):
        change = float(snapshot["week_change"])
        lines.append(
            f"  {index}. {name} ¥{float(snapshot['latest_price']):.2f}（7日 {change:+.1f}%）"
        )
    hoard = await _hoard_overview_line()
    if hoard:
        lines.append(hoard)
    lines.append("ℹ️ 数据来自本地小时快照，仅供参考；发送“关闭早盘提醒”可停止推送。")
    return "\n".join(lines)


async def _hoard_overview_line() -> str:
    """One honest 挂刀 line for the morning digest; '' when snapshot absent."""

    try:
        from .hoard_index import hoard_summary_line

        line = await hoard_summary_line()
    except Exception as exc:
        logger.warning("Hoard overview line failed error=%s", type(exc).__name__)
        return ""
    return line or ""


async def morning_push_once(bot: object, *, now: float | None = None) -> int:
    """Send the morning digest to every opted-in user; return sends."""

    if not proactive_enabled():
        return 0
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from ..state.runtime import proactive_deliveries, watchlists

    current = int(time.time() if now is None else now)
    day_key, day_start = _local_day_bounds(current)
    day_text = datetime.fromtimestamp(current, _MARKET_TIMEZONE).strftime("%m-%d")
    cap = proactive_daily_cap()
    sent = 0
    try:
        users = await proactive_deliveries.list_optedin()
    except Exception as exc:
        logger.warning("Morning opt-in list failed error=%s", type(exc).__name__)
        return 0
    for user_id in users:
        try:
            if await proactive_deliveries.has_delivery(user_id, "morning", day_key):
                continue
            if await proactive_deliveries.count_delivered_since(user_id, since=day_start) >= cap:
                continue
            items = await watchlists.list_owner(user_id)
            if not items:
                await proactive_deliveries.record(
                    user_id, "morning", day_key, "skipped", detail="自选为空"
                )
                continue
            priced: list[tuple[str, dict[str, float]]] = []
            for item in items[:_MAX_WATCH_ITEMS]:
                snapshot = await market_history.item_snapshot(item.display_name)
                if snapshot is not None:
                    priced.append((item.display_name, snapshot))
            if not priced:
                await proactive_deliveries.record(
                    user_id, "morning", day_key, "skipped", detail="本地快照未覆盖自选"
                )
                continue
            overview = await _market_overview_line()
            text = await _render_morning(day_text, overview, priced)
            async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                await bot.send_private_msg(  # type: ignore[attr-defined]
                    user_id=user_id,
                    message=Message(MessageSegment.text(text)),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Morning push failed user=%s error=%s", user_id, type(exc).__name__)
            try:
                await proactive_deliveries.record(
                    user_id,
                    "morning",
                    day_key,
                    "failed",
                    detail=type(exc).__name__,
                )
            except Exception:
                logger.exception("Morning failure ledger write failed")
            continue
        # Ledger bookkeeping must never abort the loop for the remaining users.
        try:
            await proactive_deliveries.record(user_id, "morning", day_key, "delivered")
        except Exception:
            logger.exception("Morning delivery ledger write failed user=%s", user_id)
        sent += 1
    return sent


def _render_dip(name: str, latest: float, prior_min: float, days: int) -> str:
    return (
        f"📉 【自选新低提醒】\n{name}\n"
        f"已跌到 ¥{latest:.2f}，是近 {days} 天新低（此前最低 ¥{prior_min:.2f}）\n"
        "ℹ️ 来自本地快照对比，仅供参考；发送“关闭早盘提醒”可停止推送。"
    )


async def dip_scan_once(bot: object, *, now: float | None = None) -> int:
    """Scan opted-in users' watchlists for fresh multi-day lows; return sends."""

    if not proactive_enabled():
        return 0
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from ..state.runtime import proactive_deliveries, watchlists

    current = int(time.time() if now is None else now)
    day_key, day_start = _local_day_bounds(current)
    cap = proactive_daily_cap()
    sent = 0
    try:
        users = await proactive_deliveries.list_optedin()
    except Exception as exc:
        logger.warning("Dip opt-in list failed error=%s", type(exc).__name__)
        return 0
    for user_id in users:
        dedupe_key: str | None = None
        delivered = False
        try:
            if await proactive_deliveries.count_delivered_since(user_id, since=day_start) >= cap:
                continue
            items = await watchlists.list_owner(user_id)
            for item in items:
                dedupe_key = None
                delivered = False
                dip = await market_history.item_dip(item.display_name, days=_DIP_DAYS)
                if dip is None:
                    continue
                latest, prior_min, _prior_buckets = dip
                if latest > prior_min * _DIP_RATIO:
                    continue
                dedupe_key = f"{day_key}:{item.display_name[:120]}"
                if await proactive_deliveries.has_delivery(user_id, "dip", dedupe_key):
                    continue
                if (
                    await proactive_deliveries.count_delivered_since(user_id, since=day_start)
                    >= cap
                ):
                    break
                text = _render_dip(item.display_name, latest, prior_min, _DIP_DAYS)
                async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                    await bot.send_private_msg(  # type: ignore[attr-defined]
                        user_id=user_id,
                        message=Message(MessageSegment.text(text)),
                    )
                delivered = True
                try:
                    await proactive_deliveries.record(user_id, "dip", dedupe_key, "delivered")
                except Exception:
                    logger.exception("Dip delivery ledger write failed user=%s", user_id)
                sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Dip scan failed user=%s error=%s", user_id, type(exc).__name__)
            # Mirror the morning path: a failed send must occupy a ledger row
            # so the same notice is not re-attempted forever within the day.
            if dedupe_key is not None and not delivered:
                try:
                    await proactive_deliveries.record(
                        user_id,
                        "dip",
                        dedupe_key,
                        "failed",
                        detail=type(exc).__name__,
                    )
                except Exception:
                    logger.exception("Dip failure ledger write failed")
            continue
    return sent


__all__ = [
    "dip_scan_once",
    "morning_push_once",
    "proactive_daily_cap",
    "proactive_enabled",
    "proactive_morning_hour",
]

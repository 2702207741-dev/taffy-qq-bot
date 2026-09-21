"""Template-rendered daily and weekly market reports from local data only.

Every number comes from persisted local stores (index daily closes, hourly
quote snapshots, delivered alerts, the scraped SteamDT summary cache).  The
rendering is pure templating — no LLM and no live market request — so a
report can never invent or approximate a fact.  Sections without enough
data render an explicit placeholder instead of a guess.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import NotRequired, TypedDict

from .config import steamdt_cache_path
from .index_history import index_history_store
from .market_history import market_history

logger = logging.getLogger("taffy.market_report")

_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_MOVEMENT_LIMIT = 5
_ALERTS_LIMIT = 5


@dataclass(frozen=True, slots=True)
class _AlertTrigger:
    market_name: str
    direction: str
    target_price_cents: int


class ReportData(TypedDict):
    """Structured report shared by the text renderer and the PNG card."""

    kind: str
    weekly: bool
    today: date
    closes: tuple[tuple[date, float], ...]
    movers: list[tuple[str, float, float, float]] | None
    risers: list[tuple[str, float, float, float]]
    fallers: list[tuple[str, float, float, float]]
    triggers: tuple[_AlertTrigger, ...]
    alert_total: int
    hoard_line: NotRequired[str | None]


def _local_date(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, _MARKET_TIMEZONE).date()


def _index_lines(closes: tuple[tuple[date, float], ...], *, weekly: bool) -> list[str]:
    """Render the broad-index section from persisted daily closes."""

    span = 7 if weekly else 1
    needed = 2 if not weekly else span + 1
    if len(closes) < needed:
        return ["大盘：日线数据积累中"]
    latest_date, latest_close = closes[-1]
    if weekly:
        base_date, base_close = closes[-(span + 1)]
        window = [close for _, close in closes[-span:]]
        change = (latest_close / base_close - 1) * 100
        label = f"近{span}个交易日"
        return [
            f"大盘：¥{latest_close:.2f}（{label} {change:+.2f}%）",
            f"区间高 ¥{max(window):.2f}｜低 ¥{min(window):.2f}｜截至 {latest_date.isoformat()}",
        ]
    previous_close = closes[-2][1]
    change = (latest_close / previous_close - 1) * 100
    week = [close for _, close in closes[-7:]]
    return [
        f"大盘：¥{latest_close:.2f}（较前一收盘日 {change:+.2f}%）",
        f"近7日高 ¥{max(week):.2f}｜低 ¥{min(week):.2f}｜截至 {latest_date.isoformat()}",
    ]


def _movement_lines(
    movers: list[tuple[str, float, float, float]] | None, *, weekly: bool
) -> list[str]:
    if not movers:
        return ["异动数据采集中"]
    window = "近7日" if weekly else "24小时"
    lines = [f"今日异动（{window}同样本）：" if not weekly else f"本周异动（{window}同样本）："]
    lines.extend(
        f"  {name} {change:+.2f}%" for name, change, _price, _time in movers[:_MOVEMENT_LIMIT]
    )
    return lines


async def _hoard_line() -> str | None:
    """One honest 挂刀 line from the local snapshot; None when absent.

    The upstream iflow.work archive stopped producing dumps after 2026-05-20
    (verified 2026-08-30), so the line always carries the real dump age when
    a snapshot exists; without one it renders the explicit placeholder.
    """

    try:
        from .hoard_index import hoard_summary_line

        line = await hoard_summary_line()
    except Exception as exc:
        logger.warning("Hoard summary line failed error=%s", type(exc).__name__)
        line = None
    return line


def _alert_lines(triggers: tuple[_AlertTrigger, ...], total: int) -> list[str]:
    if total <= 0:
        return ["预警触发：本时段无记录"]
    lines = [f"预警触发：{total} 条"]
    lines.extend(
        f"  {trigger.market_name} "
        f"{'涨到' if trigger.direction == 'above' else '跌到'} "
        f"¥{trigger.target_price_cents / 100:.2f} ✅"
        for trigger in triggers[:_ALERTS_LIMIT]
    )
    if total > _ALERTS_LIMIT:
        lines.append(f"  …另有 {total - _ALERTS_LIMIT} 条")
    return lines


def _volume_lines(cache: dict[str, object]) -> list[str]:
    volume = cache.get("volume")
    volume_change = cache.get("volume_change")
    if not isinstance(volume, str) or not volume:
        return ["成交概况：缓存未就绪"]
    change_text = f"（环比 {volume_change}）" if isinstance(volume_change, str) else ""
    return [f"成交概况：今日成交量 {volume}{change_text}"]


def _read_summary_cache() -> dict[str, object]:
    try:
        payload = steamdt_cache_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _delivered_alerts(since: float) -> tuple[tuple[_AlertTrigger, ...], int]:
    from src.state.runtime import database

    cutoff = int(since)
    try:
        rows = await database.read(
            """
            SELECT market_name, direction, target_price_cents, delivered_at
            FROM alerts
            WHERE status = 'delivered' AND delivered_at IS NOT NULL AND delivered_at >= ?
            ORDER BY delivered_at DESC
            """,
            (cutoff,),
        )
    except Exception as exc:
        logger.warning("Delivered alert summary failed error=%s", type(exc).__name__)
        return (), 0
    triggers = tuple(
        _AlertTrigger(
            str(row["market_name"]), str(row["direction"]), int(row["target_price_cents"])
        )
        for row in rows
    )
    return triggers[:_ALERTS_LIMIT], len(triggers)


async def build_report_data(kind: str, *, now: float | None = None) -> ReportData | None:
    """Assemble one structured report, or ``None`` when nothing is reportable.

    The dict is the single source of truth for both the text rendering and
    the PNG card rendering (v4): index lines, movement lines, alert lines,
    volume lines and a ranked movers split (top five risers/fallers).
    """

    weekly = kind == "weekly"
    current = time.time() if now is None else now
    closes: tuple[tuple[date, float], ...] = ()
    try:
        closes = await index_history_store.load_closes()
    except Exception as exc:
        logger.warning("Index closes unavailable error=%s", type(exc).__name__)
    movers: list[tuple[str, float, float, float]] | None = None
    try:
        movers = await market_history.top_movers(
            _MOVEMENT_LIMIT,
            baseline_hours=24 if not weekly else 24 * 7,
        )
    except Exception as exc:
        logger.warning("Top movers unavailable error=%s", type(exc).__name__)

    if not closes and not movers:
        return None

    today = _local_date(current)
    window_start = (
        datetime(today.year, today.month, today.day, tzinfo=_MARKET_TIMEZONE)
        - (timedelta(days=7) if weekly else timedelta(days=0))
    ).timestamp()
    triggers, total = await _delivered_alerts(window_start)
    hoard_line = await _hoard_line()

    risers = sorted(
        (item for item in movers or () if item[1] > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:_MOVEMENT_LIMIT]
    fallers = sorted(
        (item for item in movers or () if item[1] < 0),
        key=lambda item: item[1],
    )[:_MOVEMENT_LIMIT]
    return ReportData(
        kind=kind,
        weekly=weekly,
        today=today,
        closes=closes,
        movers=movers,
        risers=risers,
        fallers=fallers,
        triggers=triggers,
        alert_total=total,
        hoard_line=hoard_line,
    )


def render_report_text(data: ReportData) -> str:
    """Render the text report; the layout is the frozen v3 output."""

    weekly = bool(data["weekly"])
    today = data["today"]
    closes = data["closes"]
    movers = data["movers"]
    triggers = data["triggers"]
    total = int(data["alert_total"])
    hoard_line = data.get("hoard_line")
    lines = [f"📅 塔菲{'周报' if weekly else '日报'} {today.strftime('%m-%d')}", "────────"]
    lines.extend(_index_lines(closes, weekly=weekly))
    lines.append("温度：本地快照暂未覆盖，略")
    lines.extend(_movement_lines(movers, weekly=weekly))
    lines.extend(_alert_lines(triggers, total))
    lines.append(hoard_line or "挂刀：快照积累中")
    lines.extend(_volume_lines(_read_summary_cache()))
    lines.extend(["────────", "💡 数据来源：SteamDT 公开行情，仅供参考"])
    return "\n".join(lines)


async def build_report(kind: str, *, now: float | None = None) -> str | None:
    """Render one daily/weekly text report, or ``None`` when nothing is reportable."""

    data = await build_report_data(kind, now=now)
    return None if data is None else render_report_text(data)


__all__ = ["ReportData", "build_report", "build_report_data", "render_report_text"]

"""Read-only aggregations for the management panel.

Every query here is a bounded read against the durable state database
(``usage_log``, ``alerts``, ``watchlist_items``) or the local index store.
No network I/O, no writes, no LLM.  Missing data surfaces as empty series
with a note so the frontend can render an explicit empty state instead of
a misleading zero.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..state.runtime import backups, database
from .index_history import index_history_store

logger = logging.getLogger("taffy.panel_stats")

_PANEL_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_USAGE_TREND_DAYS = 90
MAX_MARKET_TREND_DAYS = 180


def _local_day_start(current: int) -> int:
    local = datetime.fromtimestamp(current, _PANEL_TIMEZONE)
    midnight = datetime(local.year, local.month, local.day, tzinfo=_PANEL_TIMEZONE)
    return int(midnight.timestamp())


async def dashboard_summary(*, now: int | None = None) -> dict[str, Any]:
    """Assemble the dashboard card metrics from local stores only."""

    current = int(time.time() if now is None else now)
    day_start = _local_day_start(current)
    try:
        today_rows = await database.read(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT user_id) AS users FROM usage_log "
            "WHERE created_at >= ?",
            (day_start,),
        )
        alert_rows = await database.read(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered
            FROM alerts
            """
        )
        watch_rows = await database.read("SELECT COUNT(*) AS total FROM watchlist_items")
    except Exception as exc:
        logger.warning("Panel summary query failed error=%s", type(exc).__name__)
        raise
    today_total = int(today_rows[0]["total"]) if today_rows else 0
    active_users = int(today_rows[0]["users"]) if today_rows else 0
    backup_status = await backups.status()
    return {
        "today_usage": today_total,
        "active_users": active_users,
        "alerts_pending": int(alert_rows[0]["pending"] or 0) if alert_rows else 0,
        "alerts_delivered": int(alert_rows[0]["delivered"] or 0) if alert_rows else 0,
        "watchlist_total": int(watch_rows[0]["total"]) if watch_rows else 0,
        "backup_last_success_at": backup_status.last_success_at,
        "backup_last_error": backup_status.last_error,
        "generated_at": current,
    }


async def usage_trend(days: int = 30) -> dict[str, Any]:
    """Per-day usage counts for the last ``days`` days (zero-filled)."""

    bounded = max(1, min(int(days), MAX_USAGE_TREND_DAYS))
    current = int(time.time())
    first_day = _local_day_start(current) - (bounded - 1) * 86400
    try:
        rows = await database.read(
            """
            SELECT CAST((created_at + 28800) / 86400 AS INTEGER) AS day_key,
                   COUNT(*) AS total
            FROM usage_log
            WHERE created_at >= ?
            GROUP BY day_key
            """,
            (first_day - 28800,),
        )
    except Exception as exc:
        logger.warning("Panel usage trend failed error=%s", type(exc).__name__)
        raise
    counts = {int(row["day_key"]): int(row["total"]) for row in rows}
    labels: list[str] = []
    values: list[int] = []
    for offset in range(bounded):
        day_start = first_day + offset * 86400
        labels.append(datetime.fromtimestamp(day_start, _PANEL_TIMEZONE).strftime("%m-%d"))
        values.append(counts.get((day_start + 28800) // 86400, 0))
    return {"days": bounded, "labels": labels, "values": values}


async def feature_pie(days: int = 7) -> dict[str, Any]:
    """Usage counts grouped by feature over the recent window."""

    bounded = max(1, min(int(days), 30))
    current = int(time.time())
    since = current - bounded * 86400
    try:
        rows = await database.read(
            """
            SELECT feature, COUNT(*) AS total FROM usage_log
            WHERE created_at >= ?
            GROUP BY feature
            ORDER BY total DESC
            """,
            (since,),
        )
    except Exception as exc:
        logger.warning("Panel feature pie failed error=%s", type(exc).__name__)
        raise
    if not rows:
        return {"days": bounded, "labels": [], "values": [], "note": "数据采集中"}
    return {
        "days": bounded,
        "labels": [str(row["feature"]) for row in rows],
        "values": [int(row["total"]) for row in rows],
    }


async def market_trend(days: int = 30) -> dict[str, Any]:
    """Broad-index daily closes for the line chart (local store only)."""

    bounded = max(1, min(int(days), MAX_MARKET_TREND_DAYS))
    try:
        closes = await index_history_store.load_closes()
    except Exception as exc:
        logger.warning("Panel market trend failed error=%s", type(exc).__name__)
        closes = ()
    if not closes:
        return {"days": bounded, "labels": [], "values": [], "note": "数据采集中"}
    window = closes[-bounded:]
    return {
        "days": bounded,
        "labels": [day.isoformat() for day, _close in window],
        "values": [round(close, 2) for _day, close in window],
    }


async def llm_usage_trend(days: int = 7) -> dict[str, Any]:
    """Daily LLM token/request totals from ``llm_usage_daily`` (no content)."""

    bounded = max(1, min(int(days), 90))
    try:
        rows = await database.read(
            """
            SELECT day,
                   SUM(requests) AS requests,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens
            FROM llm_usage_daily
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
            """,
            (bounded,),
        )
        per_model = await database.read(
            """
            SELECT day, provider, model, requests, input_tokens, output_tokens
            FROM llm_usage_daily
            ORDER BY day DESC, provider, model
            LIMIT ?
            """,
            (bounded * 10,),
        )
    except Exception as exc:
        logger.warning("Panel llm usage failed error=%s", type(exc).__name__)
        return {
            "days": bounded,
            "labels": [],
            "requests": [],
            "input_tokens": [],
            "output_tokens": [],
            "per_model": [],
            "note": "暂无记录",
        }
    ordered = list(reversed(rows))
    return {
        "days": bounded,
        "labels": [str(row["day"]) for row in ordered],
        "requests": [int(row["requests"] or 0) for row in ordered],
        "input_tokens": [int(row["input_tokens"] or 0) for row in ordered],
        "output_tokens": [int(row["output_tokens"] or 0) for row in ordered],
        "per_model": [
            {
                "day": str(row["day"]),
                "provider": str(row["provider"]),
                "model": str(row["model"]),
                "requests": int(row["requests"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
            }
            for row in per_model
        ],
    }


__all__ = [
    "dashboard_summary",
    "feature_pie",
    "llm_usage_trend",
    "market_trend",
    "usage_trend",
]

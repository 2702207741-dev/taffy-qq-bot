"""Administrator-only operational status command."""

from __future__ import annotations

from datetime import datetime

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.rule import Rule

from src.core.messages import text_message
from src.services.http_client import http_pool_snapshot
from src.services.market_catalog_store import market_catalog_store
from src.services.resilience import circuit_snapshots
from src.services.telemetry import telemetry
from src.state.runtime import backups, database, state_healthcheck


def _is_status_request(event: Event) -> bool:
    return isinstance(event, (GroupMessageEvent, PrivateMessageEvent)) and (
        event.get_plaintext().strip() == "塔菲状态"
    )


def _superusers() -> set[str]:
    configured: object = getattr(get_driver().config, "superusers", set())
    if not isinstance(configured, (set, frozenset, list, tuple)):
        return set()
    return {str(value) for value in configured}


def _time_label(value: object) -> str:
    if not isinstance(value, int) or value <= 0:
        return "从未"
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M")


def _pool_time_label(value: datetime | None) -> str:
    if value is None:
        return "从未"
    return value.astimezone().strftime("%m-%d %H:%M")


def _pool_reason_label(value: str | None) -> str:
    if value is None:
        return "从未"
    labels = {
        "connect_timeout": "连接超时",
        "read_timeout": "读取超时",
        "write_timeout": "写入超时",
        "connect_error": "连接失败",
        "read_error": "读取中断",
        "write_error": "写入中断",
        "remote_protocol": "远端协议断开",
        "proxy_error": "代理连接失败",
        "transport_timeout": "传输超时",
        "network_error": "网络中断",
        "request_cancelled": "请求取消",
    }
    return labels.get(value, "其他传输错误")


status_command = on_message(rule=Rule(_is_status_request), priority=4, block=True)


@status_command.handle()
async def handle_status(event: MessageEvent) -> None:
    if str(event.user_id) not in _superusers():
        await status_command.finish(text_message("这个状态页只对管理员开放。"))

    summaries = await telemetry.summary(hours=24)
    catalog = await market_catalog_store.status()
    circuits = await circuit_snapshots()
    pool = await http_pool_snapshot()
    alert_rows = await database.read(
        """
        SELECT status, COUNT(*) AS count,
               COALESCE(SUM(evaluation_failures), 0) AS evaluation_failures
        FROM alerts GROUP BY status ORDER BY status
        """
    )
    job_rows = await database.read(
        """
        SELECT job_name, MAX(updated_at) AS last_updated_at,
               MAX(CASE WHEN status = 'completed' THEN updated_at END) AS last_success_at
        FROM background_job_runs GROUP BY job_name ORDER BY job_name
        """
    )
    backup = await backups.status()

    lines = [
        "🛠️ 【塔菲状态｜最近24小时】",
        f"核心状态: {'ready' if await state_healthcheck() else 'not ready'}",
        f"HTTP连接: 第{pool.generation}代｜本次重建 {pool.rebuilds}次｜待释放 {pool.retired_clients}",
        f" 最近重建: {_pool_time_label(pool.last_rebuild_at)}｜原因: {_pool_reason_label(pool.last_reason)}",
        "──────────",
        "功能成功率 / P95:",
    ]
    command_summaries = [item for item in summaries if item.operation.startswith("command.")]
    if not command_summaries:
        lines.append(" 暂无聚合样本")
    else:
        for item in command_summaries:
            lines.append(
                f" {item.operation.removeprefix('command.')}: "
                f"{item.success_rate * 100:.1f}% / ≤{item.p95_upper_ms}ms "
                f"(n={item.total})"
            )

    lines.extend(
        (
            "──────────",
            "饰品目录:",
            f" 全量目录: {catalog['full_count']}件｜站点目录: {catalog['sitemap_count']}件"
            f"｜在线补充: {catalog['live_count']}件",
            f" 最近成功: {_time_label(catalog['last_success_at'])}",
        )
    )
    if catalog["last_error"]:
        lines.append(f" 当前校验: {catalog['last_error']}")

    lines.extend(("──────────", "上游熔断:"))
    if not circuits:
        lines.append(" 尚无熔断记录")
    else:
        for circuit in circuits:
            lines.append(f" {circuit.name}: {circuit.state}｜近窗失败 {circuit.recent_failures}")

    lines.extend(("──────────", "价格预警:"))
    if not alert_rows:
        lines.append(" 无预警")
    else:
        for row in alert_rows:
            lines.append(
                f" {row['status']}: {row['count']}｜行情延后累计 {row['evaluation_failures']}"
            )

    lines.extend(("──────────", "后台任务最近成功:"))
    if not job_rows:
        lines.append(" 暂无任务记录")
    else:
        for row in job_rows:
            lines.append(f" {row['job_name']}: {_time_label(row['last_success_at'])}")
    lines.extend(
        (
            "──────────",
            "备份:",
            f" 最近成功: {_time_label(backup.last_success_at)}｜日备份 {backup.daily_count}/7｜周备份 {backup.weekly_count}/4",
        )
    )
    if backup.last_error:
        lines.append(f" 最近错误: {backup.last_error}")
    lines.append("ℹ️ 这里只显示聚合运行信息，不记录QQ内容、账号或查询对象。")
    await status_command.finish(text_message("\n".join(lines)))


__all__ = ["handle_status"]

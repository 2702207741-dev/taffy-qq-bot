from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

from src.services.backup_service import BackupStatus
from src.services.http_client import HTTPPoolSnapshot
from src.services.resilience import CircuitSnapshot
from src.services.telemetry import OperationSummary

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import admin_status


@pytest.mark.asyncio
async def test_status_page_is_admin_only() -> None:
    event = SimpleNamespace(user_id=42, get_plaintext=lambda: "塔菲状态")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(admin_status, "_superusers", return_value={"1243607273"}),
        patch.object(admin_status.status_command, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await admin_status.handle_status(event)  # type: ignore[arg-type]
    assert "只对管理员开放" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_status_page_renders_aggregate_health_without_identities() -> None:
    event = SimpleNamespace(user_id=1243607273, get_plaintext=lambda: "塔菲状态")
    summaries = (
        OperationSummary("command.market", 10, 9, 0, 1, 0, 7, 123.4, 800),
        OperationSummary("background.collect", 2, 2, 0, 0, 0, 0, 10, 50),
    )
    finish = AsyncMock(side_effect=FinishedException())
    reads = AsyncMock(
        side_effect=[
            [{"status": "pending", "count": 4, "evaluation_failures": 7}],
            [{"job_name": "market-history", "last_success_at": 1_800_000_000}],
        ]
    )
    with (
        patch.object(admin_status, "_superusers", return_value={"1243607273"}),
        patch.object(admin_status.telemetry, "summary", AsyncMock(return_value=summaries)),
        patch.object(
            admin_status.market_catalog_store,
            "status",
            AsyncMock(
                return_value={
                    "full_count": 1_200,
                    "sitemap_count": 24_283,
                    "live_count": 8,
                    "last_success_at": 1_800_000_000,
                    "last_error": "integrity gate active",
                }
            ),
        ),
        patch.object(
            admin_status,
            "circuit_snapshots",
            AsyncMock(return_value=(CircuitSnapshot("valve", "open", 5, 12),)),
        ),
        patch.object(
            admin_status,
            "http_pool_snapshot",
            AsyncMock(
                return_value=HTTPPoolSnapshot(
                    generation=3,
                    rebuilds=2,
                    retired_clients=1,
                    last_rebuild_at=None,
                    last_reason="connect_error",
                )
            ),
        ),
        patch.object(admin_status.database, "read", reads),
        patch.object(
            admin_status.backups,
            "status",
            AsyncMock(return_value=BackupStatus(1_800_000_000, "DiskError", 7, 4)),
        ),
        patch.object(admin_status, "state_healthcheck", AsyncMock(return_value=True)),
        patch.object(admin_status.status_command, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await admin_status.handle_status(event)  # type: ignore[arg-type]

    rendered = str(finish.await_args.args[0])
    assert "market: 90.0% / ≤800ms" in rendered
    assert "全量目录: 1200件" in rendered
    assert "valve: open" in rendered
    assert "HTTP连接: 第3代｜本次重建 2次｜待释放 1" in rendered
    assert "原因: 连接失败" in rendered
    assert "行情延后累计 7" in rendered
    assert "DiskError" in rendered
    assert "1243607273" not in rendered


@pytest.mark.asyncio
async def test_status_page_handles_empty_aggregate_tables() -> None:
    event = SimpleNamespace(user_id=1, get_plaintext=lambda: "塔菲状态")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(admin_status, "_superusers", return_value={"1"}),
        patch.object(admin_status.telemetry, "summary", AsyncMock(return_value=())),
        patch.object(
            admin_status.market_catalog_store,
            "status",
            AsyncMock(
                return_value={
                    "full_count": 0,
                    "sitemap_count": 0,
                    "live_count": 0,
                    "last_success_at": None,
                    "last_error": None,
                }
            ),
        ),
        patch.object(admin_status, "circuit_snapshots", AsyncMock(return_value=())),
        patch.object(
            admin_status,
            "http_pool_snapshot",
            AsyncMock(
                return_value=HTTPPoolSnapshot(
                    generation=0,
                    rebuilds=0,
                    retired_clients=0,
                    last_rebuild_at=None,
                    last_reason=None,
                )
            ),
        ),
        patch.object(admin_status.database, "read", AsyncMock(side_effect=[[], []])),
        patch.object(
            admin_status.backups,
            "status",
            AsyncMock(return_value=BackupStatus(None, None, 0, 0)),
        ),
        patch.object(admin_status, "state_healthcheck", AsyncMock(return_value=False)),
        patch.object(admin_status.status_command, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await admin_status.handle_status(event)  # type: ignore[arg-type]
    rendered = str(finish.await_args.args[0])
    assert "not ready" in rendered
    assert "暂无聚合样本" in rendered and "无预警" in rendered
    assert "尚无熔断记录" in rendered and "从未" in rendered

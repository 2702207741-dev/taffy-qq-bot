from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services.state_maintenance import StateMaintenanceService, maintenance_run_key
from src.state.job_repository import JobClaim


def _claim(timestamp: int, interval: int) -> JobClaim:
    return JobClaim(
        run_key=maintenance_run_key(timestamp, interval),
        job_name="state-maintenance",
        lease_token="worker-lease",
        lease_expires_at=timestamp + 300,
        attempts=1,
    )


@pytest.mark.asyncio
async def test_run_once_uses_layered_settings_and_every_retention_path() -> None:
    now = datetime(2026, 7, 11, 12, 0, 5, tzinfo=timezone.utc)
    timestamp = int(now.timestamp())
    interval = 121
    jobs = SimpleNamespace(
        claim=AsyncMock(return_value=_claim(timestamp, interval)),
        mark_completed=AsyncMock(return_value=True),
        record_failure=AsyncMock(return_value=True),
        cleanup_completed=AsyncMock(return_value=5),
        cleanup_abandoned=AsyncMock(return_value=6),
    )
    broadcasts = SimpleNamespace(
        cleanup_history=AsyncMock(return_value=1),
        cleanup_failed=AsyncMock(return_value=2),
    )
    alerts = SimpleNamespace(cleanup_failed=AsyncMock(return_value=3))
    sessions = SimpleNamespace(cleanup_inactive=AsyncMock(return_value=4))
    inventory = SimpleNamespace(
        cleanup_events=AsyncMock(return_value=7),
        cleanup_item_history=AsyncMock(return_value=8),
    )
    settings = {
        "TAFFY_MAINTENANCE_INTERVAL_SECONDS": "120.1",
        "TAFFY_RETENTION_BATCH_SIZE": "12.1",
        "TAFFY_DELIVERY_RETENTION_DAYS": "2.1",
        "TAFFY_FAILED_RETENTION_DAYS": "4.1",
        "TAFFY_JOB_RETENTION_DAYS": "6.1",
        "TAFFY_SESSION_RETENTION_DAYS": "8.1",
        "TAFFY_INVENTORY_EVENT_RETENTION_DAYS": "10.1",
        "TAFFY_INVENTORY_HISTORY_RETENTION_DAYS": "11.1",
    }
    service = StateMaintenanceService(
        jobs=jobs,
        broadcasts=broadcasts,
        alerts=alerts,
        sessions=sessions,
        inventory=inventory,
        setting_reader=settings.get,
    )

    assert await service.run_once(now)

    jobs.claim.assert_awaited_once_with(
        "state-maintenance",
        maintenance_run_key(timestamp, interval),
        lease_seconds=300,
        now=timestamp,
    )
    broadcasts.cleanup_history.assert_awaited_once_with(
        delivered_before=timestamp - 3 * 86_400,
        limit=13,
    )
    broadcasts.cleanup_failed.assert_awaited_once_with(
        failed_before=timestamp - 5 * 86_400,
        limit=13,
    )
    alerts.cleanup_failed.assert_awaited_once_with(
        failed_before=timestamp - 5 * 86_400,
        limit=13,
    )
    sessions.cleanup_inactive.assert_awaited_once_with(
        before=timestamp - 9 * 86_400,
        limit=13,
    )
    inventory.cleanup_events.assert_awaited_once_with(
        before=timestamp - 11 * 86_400,
        limit=13,
    )
    inventory.cleanup_item_history.assert_awaited_once_with(
        before=timestamp - 12 * 86_400,
        limit=13,
    )
    jobs.cleanup_completed.assert_awaited_once_with(
        before=timestamp - 7 * 86_400,
        limit=13,
    )
    jobs.cleanup_abandoned.assert_awaited_once_with(
        before=timestamp - 7 * 86_400,
        limit=13,
    )
    jobs.mark_completed.assert_awaited_once_with(_claim(timestamp, interval))
    jobs.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_records_failure_type_and_logs_persistence_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 7, 11, 12, 0, 5, tzinfo=timezone.utc)
    timestamp = int(now.timestamp())
    claim = _claim(timestamp, 60)
    jobs = SimpleNamespace(
        claim=AsyncMock(return_value=claim),
        mark_completed=AsyncMock(return_value=True),
        record_failure=AsyncMock(side_effect=OSError("database unavailable")),
        cleanup_completed=AsyncMock(return_value=0),
        cleanup_abandoned=AsyncMock(return_value=0),
    )
    broadcasts = SimpleNamespace(
        cleanup_history=AsyncMock(side_effect=RuntimeError("cleanup failed")),
        cleanup_failed=AsyncMock(return_value=0),
    )
    service = StateMaintenanceService(
        jobs=jobs,
        broadcasts=broadcasts,
        alerts=SimpleNamespace(cleanup_failed=AsyncMock(return_value=0)),
        sessions=SimpleNamespace(cleanup_inactive=AsyncMock(return_value=0)),
        inventory=SimpleNamespace(
            cleanup_events=AsyncMock(return_value=0),
            cleanup_item_history=AsyncMock(return_value=0),
        ),
        setting_reader=lambda _name: "1",
    )

    with caplog.at_level(logging.ERROR, logger="taffy.services.state_maintenance"):
        assert not await service.run_once(now)

    jobs.record_failure.assert_awaited_once_with(
        claim,
        error="RuntimeError",
        retry_at=timestamp + 60,
        now=timestamp,
    )
    jobs.mark_completed.assert_not_awaited()
    broadcasts.cleanup_failed.assert_not_awaited()
    assert "State maintenance failed" in caplog.text
    assert "Unable to persist state-maintenance failure" in caplog.text


@pytest.mark.asyncio
async def test_delivery_retention_cannot_overlap_catchup_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 7, 11, 12, 0, 5, tzinfo=timezone.utc)
    timestamp = int(now.timestamp())
    jobs = SimpleNamespace(
        claim=AsyncMock(return_value=_claim(timestamp, 3_600)),
        mark_completed=AsyncMock(return_value=True),
        record_failure=AsyncMock(return_value=True),
        cleanup_completed=AsyncMock(return_value=0),
        cleanup_abandoned=AsyncMock(return_value=0),
    )
    broadcasts = SimpleNamespace(
        cleanup_history=AsyncMock(return_value=0),
        cleanup_failed=AsyncMock(return_value=0),
    )
    settings = {
        "TAFFY_DELIVERY_RETENTION_DAYS": "1",
        "TAFFY_BROADCAST_CATCHUP_MINUTES": "1440",
    }
    service = StateMaintenanceService(
        jobs=jobs,
        broadcasts=broadcasts,
        alerts=SimpleNamespace(cleanup_failed=AsyncMock(return_value=0)),
        sessions=SimpleNamespace(cleanup_inactive=AsyncMock(return_value=0)),
        inventory=SimpleNamespace(
            cleanup_events=AsyncMock(return_value=0),
            cleanup_item_history=AsyncMock(return_value=0),
        ),
        setting_reader=settings.get,
    )

    with caplog.at_level(logging.WARNING, logger="taffy.services.state_maintenance"):
        assert await service.run_once(now)

    broadcasts.cleanup_history.assert_awaited_once_with(
        delivered_before=timestamp - 2 * 86_400,
        limit=1_000,
    )
    assert "overlaps the catch-up window" in caplog.text

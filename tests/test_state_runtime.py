from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.state import runtime


@pytest.fixture(autouse=True)
def reset_runtime_state():
    previous = runtime._initialized
    runtime._initialized = False
    yield
    runtime._initialized = previous


@pytest.mark.asyncio
async def test_initialize_runs_every_repository_before_marking_ready() -> None:
    calls: list[str] = []

    def initializer(name: str) -> AsyncMock:
        async def run() -> None:
            calls.append(name)

        return AsyncMock(side_effect=run)

    with (
        patch.object(runtime.sessions, "initialize", initializer("sessions")),
        patch.object(
            runtime.persona_memories,
            "initialize",
            initializer("persona_memories"),
        ),
        patch.object(runtime.persona_styles, "initialize", initializer("persona_styles")),
        patch.object(runtime.alerts, "initialize", initializer("alerts")),
        patch.object(runtime.watchlists, "initialize", initializer("watchlists")),
        patch.object(runtime.schedules, "initialize", initializer("schedules")),
        patch.object(runtime.broadcasts, "initialize", initializer("broadcasts")),
        patch.object(runtime.jobs, "initialize", initializer("jobs")),
        patch.object(
            runtime.inventory_monitors,
            "initialize",
            initializer("inventory_monitors"),
        ),
        patch.object(runtime.database, "integrity_check", AsyncMock(return_value=True)),
    ):
        await runtime.initialize_state()
        await runtime.initialize_state()

    assert calls == [
        "sessions",
        "persona_memories",
        "persona_styles",
        "alerts",
        "watchlists",
        "schedules",
        "broadcasts",
        "jobs",
        "inventory_monitors",
    ]
    assert runtime._initialized


@pytest.mark.asyncio
async def test_failed_migration_never_marks_runtime_ready() -> None:
    with (
        patch.object(runtime.sessions, "initialize", AsyncMock()),
        patch.object(runtime.persona_memories, "initialize", AsyncMock()),
        patch.object(runtime.persona_styles, "initialize", AsyncMock()),
        patch.object(
            runtime.alerts,
            "initialize",
            AsyncMock(side_effect=RuntimeError("migration failed")),
        ),
    ):
        with pytest.raises(RuntimeError, match="migration failed"):
            await runtime.initialize_state()

    assert not runtime._initialized


@pytest.mark.asyncio
async def test_health_requires_all_markers_and_schedule_state() -> None:
    runtime._initialized = True
    marker_rows = [
        {"source_name": "legacy:alerts.json"},
        {"source_name": "watchlist-json-v1"},
        {"source_name": "schedule-config-json-v1"},
    ]
    with (
        patch.object(runtime.sessions, "healthcheck", AsyncMock(return_value=True)),
        patch.object(runtime.database, "read", AsyncMock(return_value=marker_rows)),
        patch.object(runtime.schedules, "get", AsyncMock(return_value=object())),
    ):
        assert await runtime.state_healthcheck()

    with (
        patch.object(runtime.sessions, "healthcheck", AsyncMock(return_value=True)),
        patch.object(runtime.database, "read", AsyncMock(return_value=marker_rows[:2])),
    ):
        assert not await runtime.state_healthcheck()


@pytest.mark.asyncio
async def test_shutdown_checkpoints_wal_and_clears_ready_state() -> None:
    runtime._initialized = True
    with patch.object(
        runtime.database,
        "checkpoint",
        AsyncMock(return_value=(0, 1, 1)),
    ) as checkpoint:
        await runtime.shutdown_state()

    checkpoint.assert_awaited_once()
    assert not runtime._initialized


@pytest.mark.asyncio
async def test_shutdown_still_checkpoints_when_telemetry_flush_fails() -> None:
    runtime._initialized = True
    with (
        patch.object(
            runtime.telemetry,
            "flush",
            AsyncMock(side_effect=RuntimeError("forced telemetry failure")),
        ),
        patch.object(
            runtime.database,
            "checkpoint",
            AsyncMock(return_value=(0, 1, 1)),
        ) as checkpoint,
        pytest.raises(RuntimeError, match="forced telemetry failure"),
    ):
        await runtime.shutdown_state()

    checkpoint.assert_awaited_once_with()
    assert not runtime._initialized

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import nonebot
import pytest
from nonebot.permission import SuperUser

from src.core.database import StateDatabase
from src.state.broadcast_repository import BroadcastRepository
from src.state.job_repository import JobRepository
from src.state.schedule_repository import ScheduleConfig, ScheduleVersionConflictError

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins import scheduler


def _config(
    *,
    enabled: bool = True,
    warm_cache_enabled: bool = True,
    alerts_enabled: bool = True,
    timezone_name: str = "Asia/Shanghai",
    groups: tuple[int, ...] = (100, 200),
    times: tuple[str, ...] = ("12:00",),
    version: int = 7,
    updated_at: int = 1,
) -> ScheduleConfig:
    return ScheduleConfig(
        enabled=enabled,
        warm_cache_enabled=warm_cache_enabled,
        alerts_enabled=alerts_enabled,
        timezone=timezone_name,
        groups=groups,
        times=times,
        version=version,
        updated_at=updated_at,
    )


def _market_response(index: float = 1_234.5) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "broadMarketIndex": index,
            "diffYesterdayRatio": -1.25,
            "updateTime": datetime(2026, 7, 11, 4, 0, tzinfo=timezone.utc).timestamp(),
        },
    }


def _schedule_service(config: ScheduleConfig) -> SimpleNamespace:
    return SimpleNamespace(get=AsyncMock(return_value=config))


class _TaskRepositoryRouter:
    """Give concurrent scheduler calls independent repository connections."""

    def __init__(self, *repositories: object) -> None:
        self._repositories = repositories
        self._next = 0
        self._selected: ContextVar[int | None] = ContextVar(
            f"repository-router-{id(self)}", default=None
        )

    def _repository(self) -> object:
        selected = self._selected.get()
        if selected is None:
            if self._next >= len(self._repositories):
                raise AssertionError("more scheduler workers than repository connections")
            selected = self._next
            self._next += 1
            self._selected.set(selected)
        return self._repositories[selected]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository(), name)


@pytest.fixture(autouse=True)
async def _restore_scheduler_lifecycle() -> None:
    original_bot = scheduler._bot_instance
    await scheduler._cancel_scheduler_tasks("test setup")
    yield
    await scheduler.shutdown_scheduler()
    scheduler._bot_instance = original_bot


def test_time_validation_is_exact() -> None:
    for valid in ("00:00", "08:05", "23:59"):
        assert scheduler._is_valid_time(valid), valid
    for invalid in ("8:05", "24:00", "23:60", "08:05 ", "noon", None):
        assert not scheduler._is_valid_time(invalid), invalid


def test_schedule_key_interprets_naive_clock_in_configured_timezone() -> None:
    local = datetime(2026, 7, 11, 12, 0, 59)
    aware_utc = datetime(2026, 7, 11, 4, 0, 1, tzinfo=timezone.utc)

    assert scheduler._market_schedule_key(local, "Asia/Shanghai") == "market-index:20260711T0400Z"
    assert (
        scheduler._market_schedule_key(aware_utc, "Asia/Shanghai") == "market-index:20260711T0400Z"
    )
    assert scheduler._market_schedule_key(local, "UTC") == "market-index:20260711T1200Z"


@pytest.mark.asyncio
async def test_config_load_and_save_use_optimistic_repository_version() -> None:
    before = _config(version=3)
    after = _config(enabled=False, version=4)
    repository = SimpleNamespace(
        get=AsyncMock(return_value=before),
        replace=AsyncMock(return_value=after),
    )
    replacement = before.as_dict()
    replacement["enabled"] = False

    with patch.object(scheduler, "schedules", repository):
        assert await scheduler.load_cfg() is before
        assert await scheduler.save_cfg(replacement, expected_version=3) is after

    repository.replace.assert_awaited_once_with(replacement, expected_version=3)


@pytest.mark.asyncio
async def test_admin_save_reports_an_optimistic_version_conflict() -> None:
    with patch.object(
        scheduler,
        "save_cfg",
        new=AsyncMock(side_effect=ScheduleVersionConflictError(3, 4)),
    ):
        result = await scheduler._save_command_config(_config().as_dict(), expected_version=3)

    assert result is not None
    assert "其他管理员" in result
    assert "重试" in result


@pytest.mark.asyncio
async def test_matcher_uses_real_superuser_permission() -> None:
    class Adapter:
        @staticmethod
        def get_name() -> str:
            return "OneBot V11"

    class Event:
        def __init__(self, user_id: str) -> None:
            self.user_id = user_id

        def get_user_id(self) -> str:
            return self.user_id

    bot = SimpleNamespace(adapter=Adapter(), config=SimpleNamespace(superusers={"onebot:42"}))
    checkers = list(scheduler.sch.permission.checkers)

    assert len(checkers) == 1
    checker = checkers[0].call
    assert isinstance(checker, SuperUser)
    assert await checker(bot, Event("42"))
    assert not await checker(bot, Event("43"))


@pytest.mark.asyncio
async def test_disabled_broadcast_still_starts_connection_background_tasks() -> None:
    parked = asyncio.Event()

    async def park(*_args: object, **_kwargs: object) -> None:
        await parked.wait()

    bot = object()
    with (
        patch.object(scheduler, "_broadcast_loop", park),
        patch.object(scheduler, "_warm_volume_cache", park),
        patch.object(scheduler, "_check_alerts_loop", park),
        patch.object(scheduler, "_check_inventory_monitors_loop", park),
        patch.object(scheduler, "_maintenance_loop", park),
        patch.object(scheduler, "_operations_loop", park),
        patch.object(scheduler, "_notify_version_update", park),
        patch.object(scheduler, "_cache_warmer_enabled", return_value=True),
    ):
        await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
        tasks = dict(scheduler._scheduler_tasks)
        await asyncio.sleep(0)

        # QQ-connection-bound tasks (maintenance/operations are started
        # separately as application-level tasks and are not in this set).
        assert set(tasks) == {
            "broadcast",
            "warm-cache",
            "alerts",
            "inventory-monitors",
            "version-notify",
        }
        assert all(not task.done() for task in tasks.values())
        await scheduler.stop_scheduler(bot)  # type: ignore[arg-type]

    # A disconnect clears only the connection-bound tasks.
    assert scheduler._scheduler_tasks == {}
    assert all(task.done() for task in tasks.values())


@pytest.mark.asyncio
async def test_cache_warmer_switch_omits_optional_background_task() -> None:
    parked = asyncio.Event()

    async def park(*_args: object, **_kwargs: object) -> None:
        await parked.wait()

    bot = object()
    with (
        patch.object(scheduler, "_broadcast_loop", park),
        patch.object(scheduler, "_warm_volume_cache", park),
        patch.object(scheduler, "_check_alerts_loop", park),
        patch.object(scheduler, "_check_inventory_monitors_loop", park),
        patch.object(scheduler, "_maintenance_loop", park),
        patch.object(scheduler, "_operations_loop", park),
        patch.object(scheduler, "_notify_version_update", park),
        patch.object(scheduler, "_cache_warmer_enabled", return_value=False),
    ):
        await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
        tasks = dict(scheduler._scheduler_tasks)
        await asyncio.sleep(0)

        assert set(tasks) == {
            "broadcast",
            "alerts",
            "inventory-monitors",
            "version-notify",
        }
        await scheduler.stop_scheduler(bot)  # type: ignore[arg-type]

    assert scheduler._scheduler_tasks == {}
    assert all(task.done() for task in tasks.values())


@pytest.mark.asyncio
async def test_operations_loop_isolates_independent_job_failures() -> None:
    telemetry = SimpleNamespace(
        flush=AsyncMock(side_effect=RuntimeError("forced telemetry failure"))
    )
    catalog = SimpleNamespace(schedule_refresh_if_due=Mock(), schedule_sitemap_sync_if_due=Mock())
    history = SimpleNamespace(collect_once=AsyncMock(), refresh_equipment_components=AsyncMock())
    index_history = SimpleNamespace(backfill_if_due=AsyncMock())
    backup_service = SimpleNamespace(run_if_due=AsyncMock())
    config_snapshots = SimpleNamespace(run_if_due=AsyncMock())

    with (
        patch.object(scheduler, "telemetry", telemetry),
        patch.object(scheduler, "market_catalog_store", catalog),
        patch.object(scheduler, "market_history", history),
        patch.object(scheduler, "index_history_store", index_history),
        patch.object(scheduler, "backups", backup_service),
        patch.object(scheduler, "config_snapshots", config_snapshots),
        patch.object(scheduler, "_reddit_warmup_tick", Mock()),
        patch.object(
            scheduler.asyncio,
            "sleep",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await scheduler._operations_loop()

    telemetry.flush.assert_awaited_once_with()
    catalog.schedule_refresh_if_due.assert_called_once_with()
    catalog.schedule_sitemap_sync_if_due.assert_called_once_with()
    history.collect_once.assert_awaited_once_with()
    index_history.backfill_if_due.assert_awaited_once_with()
    backup_service.run_if_due.assert_awaited_once_with()
    config_snapshots.run_if_due.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_restart_does_not_resend_a_successful_group(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    first = BroadcastRepository(StateDatabase(database_path))
    await first.initialize()
    config = _config(groups=(100,), times=("12:00",))
    bot = SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 991}))
    fetch = AsyncMock(return_value=_market_response())
    now = datetime(2026, 7, 11, 12, 0, 5)

    with (
        patch.object(scheduler, "broadcasts", first),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        result = await scheduler._broadcast_tick(now)

    assert [item.success for item in result] == [True]
    rows = await first.list(schedule_key="market-index:20260711T0400Z")
    assert len(rows) == 1
    assert rows[0].status == "delivered"
    assert rows[0].message_id == "991"
    assert rows[0].payload_text is not None

    restarted = BroadcastRepository(StateDatabase(database_path))
    await restarted.initialize()
    with (
        patch.object(scheduler, "broadcasts", restarted),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        assert await scheduler._broadcast_tick(now) == []

    bot.send_group_msg.assert_awaited_once()
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_catches_up_a_missed_slot_once(tmp_path: Path) -> None:
    path = tmp_path / "catch-up.db"
    repository = BroadcastRepository(StateDatabase(path))
    await repository.initialize()
    config = _config(groups=(100,), times=("12:00",))
    bot = SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 1}))
    fetch = AsyncMock(return_value=_market_response())
    five_minutes_late = datetime(2026, 7, 11, 12, 5, 0)

    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        first = await scheduler._broadcast_tick(five_minutes_late)
        second = await scheduler._broadcast_tick(five_minutes_late)

    assert [item.success for item in first] == [True]
    assert second == []
    bot.send_group_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_config_change_does_not_retroactively_backfill_old_slots(tmp_path: Path) -> None:
    path = tmp_path / "config-cutoff.db"
    repository = BroadcastRepository(StateDatabase(path))
    await repository.initialize()
    current = datetime(2026, 7, 11, 12, 5, 0, tzinfo=timezone.utc)
    config = _config(
        timezone_name="UTC",
        groups=(100,),
        times=("12:00",),
        updated_at=int(current.timestamp()),
    )
    bot = SimpleNamespace(send_group_msg=AsyncMock())

    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
    ):
        assert await scheduler._broadcast_tick(current) == []

    bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_failure_retries_only_failed_group_and_stores_type_only(
    tmp_path: Path,
) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "partial.db"), retry_base_seconds=30)
    await repository.initialize()
    config = _config(groups=(100, 200))
    attempts: defaultdict[int, int] = defaultdict(int)

    async def send_group_msg(*, group_id: int, message: object) -> dict[str, int]:
        del message
        attempts[group_id] += 1
        if group_id == 200 and attempts[group_id] == 1:
            raise TimeoutError("sensitive transport detail")
        return {"message_id": group_id * 10 + attempts[group_id]}

    bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=send_group_msg))
    fetch = AsyncMock(return_value=_market_response())
    first_now = datetime(2026, 7, 11, 12, 0, 5)
    retry_now = datetime(2026, 7, 11, 12, 0, 36)
    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        first = await scheduler._broadcast_tick(first_now)
        failed = (await repository.list(schedule_key="market-index:20260711T0400Z"))[1]
        second = await scheduler._broadcast_tick(retry_now)

    assert [item.success for item in first] == [True, False]
    assert [item.group_id for item in second] == ["200"]
    assert [item.success for item in second] == [True]
    assert failed.last_error == "TimeoutError"
    assert "sensitive" not in (failed.last_error or "")
    assert attempts == {100: 1, 200: 2}
    rows = await repository.list(schedule_key="market-index:20260711T0400Z")
    assert {row.status for row in rows} == {"delivered"}


@pytest.mark.asyncio
async def test_retry_reuses_the_first_frozen_payload(tmp_path: Path) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "frozen.db"), retry_base_seconds=30)
    await repository.initialize()
    config = _config(groups=(100,))
    sent_payloads: list[str] = []

    async def send_group_msg(*, group_id: int, message: object) -> dict[str, int]:
        del group_id
        sent_payloads.append(str(message))
        if len(sent_payloads) == 1:
            raise ConnectionError("first send failed")
        return {"message_id": 2}

    bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=send_group_msg))
    fetch = AsyncMock(
        side_effect=[
            _market_response(1_111.0),
            _market_response(9_999.0),
        ]
    )
    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        await scheduler._broadcast_tick(datetime(2026, 7, 11, 12, 0, 5))
        await scheduler._broadcast_tick(datetime(2026, 7, 11, 12, 0, 36))

    assert fetch.await_count == 1
    assert sent_payloads[0] == sent_payloads[1]
    assert "1111.00" in sent_payloads[0]
    assert "9999.00" not in sent_payloads[1]


@pytest.mark.asyncio
async def test_payload_failure_releases_all_groups_without_sending(tmp_path: Path) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "payload-failure.db"))
    await repository.initialize()
    config = _config(groups=(100, 200))
    bot = SimpleNamespace(send_group_msg=AsyncMock())
    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(
            scheduler,
            "_fetch_market_index",
            AsyncMock(return_value={"success": False}),
        ),
    ):
        results = await scheduler._broadcast_tick(datetime(2026, 7, 11, 12, 0, 5))

    assert len(results) == 2
    assert not any(result.success for result in results)
    rows = await repository.list(schedule_key="market-index:20260711T0400Z")
    assert {row.status for row in rows} == {"pending"}
    assert {row.attempts for row in rows} == {1}
    bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_workers_claim_each_group_only_once(tmp_path: Path) -> None:
    database_path = tmp_path / "workers.db"
    first_repository = BroadcastRepository(StateDatabase(database_path))
    second_repository = BroadcastRepository(StateDatabase(database_path))
    await asyncio.gather(first_repository.initialize(), second_repository.initialize())
    repositories = _TaskRepositoryRouter(first_repository, second_repository)
    config = _config(groups=(100, 200))

    async def send_group_msg(*, group_id: int, message: object) -> dict[str, int]:
        del message
        await asyncio.sleep(0.01)
        return {"message_id": group_id}

    bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=send_group_msg))
    fetch = AsyncMock(return_value=_market_response())
    now = datetime(2026, 7, 11, 12, 0, 5)
    with (
        patch.object(scheduler, "broadcasts", repositories),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        left, right = await asyncio.gather(
            scheduler._broadcast_tick(now),
            scheduler._broadcast_tick(now),
        )

    assert sum(item.success for item in left + right) == 2
    assert sorted(call.kwargs["group_id"] for call in bot.send_group_msg.await_args_list) == [
        100,
        200,
    ]
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_reenabled_broadcast_does_not_send_a_frozen_payload_after_it_is_stale(
    tmp_path: Path,
) -> None:
    repository = BroadcastRepository(
        StateDatabase(tmp_path / "disabled-retry.db"), retry_base_seconds=30
    )
    await repository.initialize()
    enabled = _config(groups=(100,))
    disabled = _config(enabled=False, groups=(100,))
    bot = SimpleNamespace(
        send_group_msg=AsyncMock(side_effect=[ConnectionError("offline"), {"message_id": 2}])
    )
    fetch = AsyncMock(return_value=_market_response())
    config_service = SimpleNamespace(get=AsyncMock(side_effect=[enabled, disabled, enabled]))
    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", config_service),
        patch.object(scheduler, "_bot_instance", bot),
        patch.object(scheduler, "_fetch_market_index", fetch),
    ):
        await scheduler._broadcast_tick(datetime(2026, 7, 11, 12, 0, 5))
        paused = await scheduler._broadcast_tick(datetime(2026, 7, 11, 13, 0, 5))
        result = await scheduler._broadcast_tick(datetime(2026, 7, 11, 14, 0, 5))

    assert paused == []
    assert [item.group_id for item in result] == ["100"]
    assert not result[0].success
    assert result[0].error == "MarketPayloadError"
    assert bot.send_group_msg.await_count == 1
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_warm_cache_job_is_unique_per_interval_across_workers(tmp_path: Path) -> None:
    database_path = tmp_path / "warm.db"
    first_repository = JobRepository(StateDatabase(database_path))
    second_repository = JobRepository(StateDatabase(database_path))
    await asyncio.gather(first_repository.initialize(), second_repository.initialize())
    repositories = _TaskRepositoryRouter(first_repository, second_repository)
    runner = AsyncMock(return_value=True)
    config = _config(warm_cache_enabled=True)
    now = datetime(2026, 7, 11, 12, 0, 5)

    with (
        patch.object(scheduler, "jobs", repositories),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_cache_warmer_enabled", return_value=True),
        patch.object(scheduler, "_run_warm_cache_once", runner),
        patch.dict(os.environ, {"TAFFY_WARM_CACHE_INTERVAL_SECONDS": "300"}),
    ):
        outcomes = await asyncio.gather(
            scheduler._warm_cache_tick(now),
            scheduler._warm_cache_tick(now),
        )

    assert sorted(outcomes) == [False, True]
    runner.assert_awaited_once()

    restarted = JobRepository(StateDatabase(database_path))
    await restarted.initialize()
    with (
        patch.object(scheduler, "jobs", restarted),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.object(scheduler, "_run_warm_cache_once", runner),
        patch.dict(os.environ, {"TAFFY_WARM_CACHE_INTERVAL_SECONDS": "300"}),
    ):
        assert not await scheduler._warm_cache_tick(now)
    runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_switch_skips_cache_claim_and_schedule_read() -> None:
    schedule_service = SimpleNamespace(get=AsyncMock())
    job_service = SimpleNamespace(claim=AsyncMock())
    with (
        patch.object(scheduler, "schedules", schedule_service),
        patch.object(scheduler, "jobs", job_service),
        patch.object(scheduler, "_cache_warmer_enabled", return_value=False),
    ):
        assert not await scheduler._warm_cache_tick(datetime(2026, 7, 11, 12, 0, 5))

    schedule_service.get.assert_not_awaited()
    job_service.claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_cache_failure_persists_only_exception_type(tmp_path: Path) -> None:
    class BrowserCrashedError(RuntimeError):
        pass

    repository = JobRepository(StateDatabase(tmp_path / "warm-failure.db"))
    await repository.initialize()
    with (
        patch.object(scheduler, "jobs", repository),
        patch.object(scheduler, "schedules", _schedule_service(_config())),
        patch.object(scheduler, "_cache_warmer_enabled", return_value=True),
        patch.object(
            scheduler,
            "_run_warm_cache_once",
            new=AsyncMock(side_effect=BrowserCrashedError("browser profile secret")),
        ),
        patch.dict(os.environ, {"TAFFY_WARM_CACHE_INTERVAL_SECONDS": "300"}),
    ):
        assert not await scheduler._warm_cache_tick(datetime(2026, 7, 11, 12, 0, 5))

    rows = await repository.database.read("SELECT last_error FROM background_job_runs")
    assert rows == [{"last_error": "BrowserCrashedError"}]


@pytest.mark.asyncio
async def test_maintenance_is_fenced_and_invokes_all_retention_paths(tmp_path: Path) -> None:
    repository = JobRepository(StateDatabase(tmp_path / "maintenance.db"))
    await repository.initialize()
    broadcast_service = SimpleNamespace(
        cleanup_history=AsyncMock(return_value=2),
        cleanup_failed=AsyncMock(return_value=1),
    )
    alert_service = SimpleNamespace(cleanup_failed=AsyncMock(return_value=3))
    session_service = SimpleNamespace(cleanup_inactive=AsyncMock(return_value=4))
    inventory_service = SimpleNamespace(
        cleanup_events=AsyncMock(return_value=5),
        cleanup_item_history=AsyncMock(return_value=6),
    )
    usage_log_service = SimpleNamespace(cleanup=AsyncMock(return_value=7))
    now = datetime(2026, 7, 11, 12, 0, 5, tzinfo=timezone.utc)

    with (
        patch.object(scheduler, "jobs", repository),
        patch.object(scheduler, "broadcasts", broadcast_service),
        patch.object(scheduler, "alert_repository", alert_service),
        patch.object(scheduler, "sessions", session_service),
        patch.object(scheduler, "inventory_monitors", inventory_service),
        patch.object(scheduler, "usage_log", usage_log_service),
        patch.object(
            scheduler,
            "session_summaries",
            SimpleNamespace(
                cleanup=AsyncMock(return_value=0),
                purge_orphans=AsyncMock(return_value=0),
            ),
        ),
    ):
        assert await scheduler._maintenance_tick(now)
        assert not await scheduler._maintenance_tick(now)

    broadcast_service.cleanup_history.assert_awaited_once()
    broadcast_service.cleanup_failed.assert_awaited_once()
    alert_service.cleanup_failed.assert_awaited_once()
    session_service.cleanup_inactive.assert_awaited_once()
    inventory_service.cleanup_events.assert_awaited_once()
    inventory_service.cleanup_item_history.assert_awaited_once()
    usage_log_service.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_lease_covers_worst_case_bounded_send_batch() -> None:
    observed: dict[str, int] = {}

    async def claim_due(**kwargs):
        observed["lease_seconds"] = kwargs["lease_seconds"]
        observed["limit"] = kwargs["limit"]
        return []

    repository = SimpleNamespace(claim_due=claim_due)
    config = _config(times=())
    with (
        patch.object(scheduler, "broadcasts", repository),
        patch.object(scheduler, "schedules", _schedule_service(config)),
        patch.dict(
            os.environ,
            {
                "TAFFY_BROADCAST_RETRY_BATCH_SIZE": "100",
                "TAFFY_BROADCAST_SEND_CONCURRENCY": "5",
                "TAFFY_BROADCAST_SEND_TIMEOUT_SECONDS": "20",
                "TAFFY_BROADCAST_LEASE_SECONDS": "180",
            },
        ),
    ):
        assert await scheduler._broadcast_tick(datetime(2026, 7, 11, 12, 0, 5)) == []

    assert observed["limit"] == 100
    # Retry rows can belong to 100 distinct schedule keys, which the current
    # delivery implementation processes serially.
    assert observed["lease_seconds"] >= 2_290


def test_broadcast_lease_accounts_for_serial_catchup_slots() -> None:
    with patch.dict(
        os.environ,
        {
            "TAFFY_BROADCAST_SEND_CONCURRENCY": "5",
            "TAFFY_BROADCAST_SEND_TIMEOUT_SECONDS": "20",
            "TAFFY_BROADCAST_LEASE_SECONDS": "180",
        },
    ):
        # Ten catch-up slots with one group each are ten serial waves, not two
        # concurrent waves based on the aggregate row count.
        lease = scheduler._broadcast_lease_seconds(10, serial_waves=10)

    assert lease >= 310


def test_child_environment_does_not_forward_api_secrets() -> None:
    with patch.dict(
        os.environ,
        {
            "PATH": "safe-path",
            "DEEPSEEK_API_KEY": "do-not-copy",
            "ANTHROPIC_API_KEY": "do-not-copy",
            "PLAYWRIGHT_BROWSERS_PATH": "browser-cache",
        },
        clear=True,
    ):
        environment = scheduler._subprocess_env()

    assert environment["PATH"] == "safe-path"
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "browser-cache"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.asyncio
async def test_cache_warmer_uses_current_python_and_checks_return_code(
    tmp_path: Path,
) -> None:
    class FailedProcess:
        returncode = 7

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"output", b"failure"

    script = tmp_path / "scraper.py"
    script.write_text("print('ok')", encoding="utf-8")
    process = FailedProcess()
    create_process = AsyncMock(return_value=process)
    with (
        patch.dict(
            os.environ,
            {
                "TAFFY_SCRAPE_SCRIPT": str(script),
                "TAFFY_SCRAPE_WORKDIR": str(tmp_path),
            },
            clear=True,
        ),
        patch.object(asyncio, "create_subprocess_exec", create_process),
    ):
        succeeded = await scheduler._run_warm_cache_once()

    assert not succeeded
    args, kwargs = create_process.await_args
    assert args[0] == sys.executable
    assert Path(args[1]) == script.resolve()
    assert Path(kwargs["cwd"]) == tmp_path.resolve()


@pytest.mark.asyncio
async def test_terminate_escalates_to_kill_after_wait_timeout() -> None:
    class StubbornProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.actions: list[str] = []
            self.killed = False

        def terminate(self) -> None:
            self.actions.append("terminate")

        def kill(self) -> None:
            self.actions.append("kill")
            self.killed = True

        async def wait(self) -> int:
            self.actions.append("wait")
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9
            return self.returncode

    process = StubbornProcess()
    await scheduler._terminate_process(process, grace_seconds=0.01)  # type: ignore[arg-type]

    assert process.actions == ["terminate", "wait", "kill", "wait"]


@pytest.mark.asyncio
async def test_learning_tick_claims_reflection_by_configured_local_hour() -> None:
    """A4 regression: TAFFY_REFLECTION_HOUR must fire in Asia/Shanghai, not UTC."""

    # 2026-08-21 19:30 UTC == 2026-08-22 03:30 Asia/Shanghai.  The legacy
    # UTC-based hour formula yields 19; the configured local hour is 3.
    fixed_utc = datetime(2026, 8, 21, 19, 30, tzinfo=timezone.utc)
    claims: list[tuple[str, str]] = []

    async def fake_claim(name: str, key: str, **_kwargs: object) -> None:
        claims.append((name, key))
        return None

    with (
        patch.dict(os.environ, {"TAFFY_REFLECTION_HOUR": "3"}),
        patch.object(scheduler.learning_service, "learning_enabled", return_value=True),
        patch.object(scheduler.learning_service, "observe_once", AsyncMock(return_value=0)),
        patch.object(scheduler.learning_service, "decay_once", AsyncMock(return_value=0)),
        patch.object(scheduler.learning_service, "reinforce_once", AsyncMock(return_value=0)),
        patch.object(scheduler, "load_cfg", AsyncMock(return_value=_config())),
        patch.object(scheduler.jobs, "claim", side_effect=fake_claim),
    ):
        await scheduler._learning_tick(now=int(fixed_utc.timestamp()))

    claimed_keys = dict(claims)
    assert claimed_keys["learning-reflection"] == "learning-reflection-20260822"
    assert claimed_keys["learning-decay"] == "learning-decay-20260822"
    assert claimed_keys["learning-reinforce"] == "learning-reinforce-20260822"

    # Control: the UTC hour (19) must not trigger the reflection slot.
    claims.clear()
    with (
        patch.dict(os.environ, {"TAFFY_REFLECTION_HOUR": "19"}),
        patch.object(scheduler.learning_service, "learning_enabled", return_value=True),
        patch.object(scheduler.learning_service, "observe_once", AsyncMock(return_value=0)),
        patch.object(scheduler.learning_service, "decay_once", AsyncMock(return_value=0)),
        patch.object(scheduler.learning_service, "reinforce_once", AsyncMock(return_value=0)),
        patch.object(scheduler, "load_cfg", AsyncMock(return_value=_config())),
        patch.object(scheduler.jobs, "claim", side_effect=fake_claim),
    ):
        await scheduler._learning_tick(now=int(fixed_utc.timestamp()))

    assert all(name != "learning-reflection" for name, _key in claims)

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from src.core import logging_privacy
from src.core.database import SCHEMA_VERSION, StateDatabase
from src.services import resilience as resilience_module
from src.services import resource_governor as governor_module
from src.services.backup_service import BackupService
from src.services.kline_fast_renderer import HEIGHT, WIDTH, render_kline_png
from src.services.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    circuit_snapshots,
    get_circuit_breaker,
)
from src.services.resource_governor import ResourceGovernor, resource_governor
from src.services.telemetry import TelemetryRecorder
from src.services.verified import (
    DataProvenance,
    UnavailableReason,
    VerifiedResult,
    finite_number,
    payload_sha256,
)


def test_verified_result_enforces_one_trust_state() -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    provenance = DataProvenance("provider", "/endpoint", now, now, "a" * 64)
    available = VerifiedResult.available((1, 2), provenance)
    unavailable = VerifiedResult.unavailable(UnavailableReason.STALE, "expired")
    assert available.is_available and available.value == (1, 2)
    assert not unavailable.is_available and unavailable.reason is UnavailableReason.STALE
    with pytest.raises(ValueError):
        VerifiedResult(value=1, provenance=None)
    with pytest.raises(ValueError):
        VerifiedResult(value=None, provenance=None, reason=None)
    with pytest.raises(ValueError):
        DataProvenance("", "/endpoint", now, now, "a" * 64)
    with pytest.raises(ValueError):
        DataProvenance("provider", "/endpoint", now.replace(tzinfo=None), now, "bad")
    no_provider_time = DataProvenance("provider", "/snapshot", None, now, "b" * 64)
    assert no_provider_time.data_time is None


def test_canonical_payload_hash_and_finite_numbers() -> None:
    assert payload_sha256({"b": 2, "a": 1}) == payload_sha256({"a": 1, "b": 2})
    assert finite_number("1.25", positive=True) == 1.25
    for value in (True, None, "bad", float("nan"), float("inf"), -1, 0):
        assert finite_number(value, positive=True) is None
    with pytest.raises(ValueError):
        payload_sha256({"value": float("nan")})


@pytest.mark.asyncio
async def test_circuit_breaker_opens_half_opens_and_recovers() -> None:
    clock = [100.0]
    breaker = CircuitBreaker(
        "test",
        threshold=2,
        window_seconds=10,
        open_seconds=5,
        clock=lambda: clock[0],
    )
    await breaker.acquire()
    await breaker.fail()
    await breaker.fail()
    assert (await breaker.snapshot()).state == "open"
    with pytest.raises(CircuitOpenError):
        await breaker.acquire()
    clock[0] += 5
    assert (await breaker.snapshot()).state == "half_open"
    await breaker.acquire()
    with pytest.raises(CircuitOpenError):
        await breaker.acquire()
    await breaker.cancel_probe()
    await breaker.acquire()
    await breaker.succeed()
    snapshot = await breaker.snapshot()
    assert snapshot.state == "closed" and snapshot.recent_failures == 0
    assert get_circuit_breaker("registry") is get_circuit_breaker("registry")
    assert any(item.name == "registry" for item in await circuit_snapshots())


@pytest.mark.asyncio
async def test_circuit_failure_window_prunes_old_failures() -> None:
    clock = [10.0]
    breaker = CircuitBreaker("window", threshold=2, window_seconds=2, clock=lambda: clock[0])
    await breaker.fail()
    clock[0] = 13.0
    await breaker.fail()
    assert (await breaker.snapshot()).recent_failures == 1


@pytest.mark.asyncio
async def test_circuit_probe_slot_recovers_after_timeout_without_reset_call() -> None:
    """A15 regression: a cancelled half-open probe must not wedge the breaker."""

    clock = [100.0]
    breaker = CircuitBreaker(
        "probe-timeout",
        threshold=1,
        window_seconds=120,
        open_seconds=10,
        probe_timeout_seconds=30,
        clock=lambda: clock[0],
    )
    await breaker.fail()  # trip open
    clock[0] += 11.0  # past open_seconds -> half-open
    await breaker.acquire()  # probe slot taken; caller never reports back
    with pytest.raises(CircuitOpenError):
        await breaker.acquire()
    # No succeed/fail/cancel_probe call happens (the task was cancelled);
    # once the probe timeout passes the slot must free up again.
    clock[0] += 31.0
    await breaker.acquire()
    await breaker.succeed()
    assert (await breaker.snapshot()).state == "closed"


def test_breaker_registry_is_lru_bounded() -> None:
    original = dict(resilience_module._BREAKERS)
    resilience_module._BREAKERS.clear()
    try:
        for index in range(resilience_module._MAX_BREAKERS + 5):
            resilience_module.get_circuit_breaker(f"evict-{index}")

        assert len(resilience_module._BREAKERS) == resilience_module._MAX_BREAKERS
        assert "evict-0" not in resilience_module._BREAKERS
        assert "evict-4" not in resilience_module._BREAKERS
        assert f"evict-{resilience_module._MAX_BREAKERS + 4}" in resilience_module._BREAKERS

        # A re-requested name is recreated and the cap still holds.
        resilience_module.get_circuit_breaker("evict-0")
        assert "evict-0" in resilience_module._BREAKERS
        assert len(resilience_module._BREAKERS) == resilience_module._MAX_BREAKERS
    finally:
        resilience_module._BREAKERS.clear()
        resilience_module._BREAKERS.update(original)


@pytest.mark.asyncio
async def test_resource_governor_detects_game_state_transitions() -> None:
    detected = [False]
    governor = ResourceGovernor(detector=lambda: detected[0], poll_seconds=1)
    assert not await governor.poll_once()
    detected[0] = True
    assert await governor.poll_once() and governor.game_running
    detected[0] = False
    assert not await governor.poll_once() and not governor.game_running
    await governor.start()
    assert governor._task is not None
    await governor.start()
    await governor.stop()
    await governor.stop()


def test_process_detector_finds_cs2_and_fails_closed_on_process_errors() -> None:
    processes = [
        SimpleNamespace(info={"name": "other.exe"}),
        SimpleNamespace(info={"name": "CS2.EXE"}),
    ]
    with patch.object(governor_module.psutil, "process_iter", return_value=processes):
        assert governor_module._cs2_is_running()
    with patch.object(
        governor_module.psutil,
        "process_iter",
        side_effect=governor_module.psutil.AccessDenied(),
    ):
        assert not governor_module._cs2_is_running()
    with patch.object(
        governor_module.psutil,
        "process_iter",
        return_value=[SimpleNamespace(info={"name": None})],
    ):
        assert not governor_module._cs2_is_running()


def test_process_detector_skips_denied_process_and_keeps_scanning() -> None:
    class DeniedProcess:
        @property
        def info(self) -> dict[str, str]:
            raise governor_module.psutil.AccessDenied(pid=1)

    processes = [DeniedProcess(), SimpleNamespace(info={"name": "cs2.exe"})]
    with patch.object(governor_module.psutil, "process_iter", return_value=processes):
        assert governor_module._cs2_is_running()


@pytest.mark.asyncio
async def test_resource_governor_module_lifecycle_delegates_to_singleton() -> None:
    with (
        patch.object(governor_module.resource_governor, "start", AsyncMock()) as start,
        patch.object(governor_module.resource_governor, "stop", AsyncMock()) as stop,
    ):
        await governor_module.start_resource_governor()
        await governor_module.stop_resource_governor()
    start.assert_awaited_once()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_telemetry_persists_only_aggregates_and_latency_buckets(tmp_path: Path) -> None:
    clock = [1_800_000_000.0]
    recorder = TelemetryRecorder(tmp_path / "telemetry.db", wall_clock=lambda: clock[0])
    for latency in (10, 100, 500, 1_500, 4_000, 7_000):
        await recorder.record("market.index", "success", latency, cache_hit=latency == 10)
    await recorder.record("market.index", "timeout", -50)
    assert await recorder.flush() == 3
    assert await recorder.flush() == 0
    summary = (await recorder.summary(hours=24))[0]
    assert summary.total == 7
    assert summary.successes == 6 and summary.timeouts == 1
    assert summary.cache_hits == 1 and summary.p95_upper_ms == 120_000
    assert summary.success_rate == pytest.approx(6 / 7)
    with pytest.raises(ValueError):
        await recorder.record("steamid:76561199543819426", "success", 1)
    with pytest.raises(ValueError):
        await recorder.record("market.index", "unknown", 1)
    database_bytes = (tmp_path / "telemetry.db").read_bytes()
    assert b"76561199543819426" not in database_bytes


@pytest.mark.asyncio
async def test_telemetry_flush_restores_pending_rows_after_database_failure(
    tmp_path: Path,
) -> None:
    recorder = TelemetryRecorder(tmp_path / "telemetry.db", wall_clock=lambda: 1_800_000_000)
    await recorder.record("inventory", "error", 80)
    with patch.object(recorder, "_connect", side_effect=sqlite3.OperationalError("locked")):
        with pytest.raises(sqlite3.OperationalError):
            await recorder.flush()
    assert await recorder.flush() == 1


@pytest.mark.asyncio
async def test_backup_set_includes_auxiliary_databases_with_manifest(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    await source.initialize()
    catalog = tmp_path / "catalog.db"
    telemetry_db = tmp_path / "telemetry.db"
    for path in (catalog, telemetry_db):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE t (id INTEGER)")
            connection.execute("INSERT INTO t VALUES (1)")
            connection.commit()
        finally:
            connection.close()
    directory = tmp_path / "backups"
    service = BackupService(
        source,
        directory=directory,
        wall_clock=lambda: 1_800_000_000,
        extra_databases={"catalog": catalog, "telemetry": telemetry_db},
    )
    assert await service.run_if_due()
    daily = directory / "daily-2027-01-15.db"
    assert daily.is_file()
    assert (directory / "daily-2027-01-15.catalog.db").is_file()
    assert (directory / "daily-2027-01-15.telemetry.db").is_file()
    manifest = json.loads((directory / "daily-2027-01-15.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "daily"
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert set(manifest["databases"]) == {"state", "catalog", "telemetry"}
    for entry in manifest["databases"].values():
        assert entry is not None
        backup = directory / entry["file"]
        assert entry["sha256"] == service._file_sha256(backup)
        connection = sqlite3.connect(backup)
        try:
            assert connection.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        finally:
            connection.close()


@pytest.mark.asyncio
async def test_backup_set_records_absent_auxiliary_as_null(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    await source.initialize()
    service = BackupService(
        source,
        directory=tmp_path / "backups",
        wall_clock=lambda: 1_800_000_000,
        extra_databases={"catalog": tmp_path / "missing-catalog.db"},
    )
    assert await service.run_if_due()
    manifest = json.loads((service.directory / "daily-2027-01-15.json").read_text(encoding="utf-8"))
    assert manifest["databases"]["state"] is not None
    assert manifest["databases"]["catalog"] is None


@pytest.mark.asyncio
async def test_backup_run_repairs_partial_set_without_manifest(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    await source.initialize()
    directory = tmp_path / "backups"
    directory.mkdir(parents=True)
    (directory / "daily-2027-01-15.db").write_bytes(b"stale")
    service = BackupService(source, directory=directory, wall_clock=lambda: 1_800_000_000)
    assert await service.run_if_due()
    assert (directory / "daily-2027-01-15.json").is_file()
    assert (directory / "weekly-2027-W02.db").is_file()
    assert not await service.run_if_due()


@pytest.mark.asyncio
async def test_backup_retention_expires_whole_sets_and_cleans_orphans(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    service = BackupService(source, directory=tmp_path / "backups")
    service.directory.mkdir(parents=True)
    for index in range(1, 11):
        stem = f"daily-2026-07-{index:02d}"
        (service.directory / f"{stem}.db").write_bytes(b"x")
        (service.directory / f"{stem}.json").write_bytes(b"{}")
    for index in range(1, 7):
        (service.directory / f"weekly-2026-W{index:02d}.db").write_bytes(b"x")
    (service.directory / "daily-2026-07-02.db-wal").write_bytes(b"x")
    (service.directory / "daily-2026-07-08.db-wal").write_bytes(b"x")
    (service.directory / "daily-1999-01-01.db-wal").write_bytes(b"x")
    (service.directory / "weekly-2026-W04.db-shm").write_bytes(b"x")
    (service.directory / "weekly-1999-01-01.db-shm").write_bytes(b"x")
    await asyncio.to_thread(service._retention_sync)
    status = await service.status()
    assert status.daily_count == 7 and status.weekly_count == 4
    assert not (service.directory / "daily-2026-07-01.db").exists()
    assert not (service.directory / "daily-2026-07-03.json").exists()
    assert not (service.directory / "daily-2026-07-02.db-wal").exists()
    assert (service.directory / "daily-2026-07-08.db-wal").is_file()
    assert not (service.directory / "daily-1999-01-01.db-wal").exists()
    assert not (service.directory / "weekly-2026-W01.db").exists()
    assert (service.directory / "weekly-2026-W04.db-shm").is_file()
    assert not (service.directory / "weekly-1999-01-01.db-shm").exists()


@pytest.mark.asyncio
async def test_sqlite_backup_is_verified_due_aware_and_game_aware(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    await source.initialize()
    service = BackupService(
        source,
        directory=tmp_path / "backups",
        wall_clock=lambda: 1_800_000_000,
    )
    original_game_state = resource_governor._game_running
    try:
        resource_governor._game_running = True
        assert not await service.run_if_due()
        resource_governor._game_running = False
        assert await service.run_if_due()
        assert not await service.run_if_due()
    finally:
        resource_governor._game_running = original_game_state
    status = await service.status()
    assert status.daily_count == 1 and status.weekly_count == 1
    for backup in (tmp_path / "backups").glob("*.db"):
        connection = sqlite3.connect(backup)
        try:
            assert connection.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        finally:
            connection.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
@pytest.mark.asyncio
async def test_sqlite_backup_hardens_posix_permissions(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    await source.initialize()
    directory = tmp_path / "backups"
    service = BackupService(source, directory=directory, wall_clock=lambda: 1_800_000_000)

    assert await service.run_if_due()

    assert directory.stat().st_mode & 0o777 == 0o700
    for backup in directory.glob("*.db"):
        assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_backup_retention_and_missing_database_failure(tmp_path: Path) -> None:
    source = StateDatabase(tmp_path / "state.db")
    service = BackupService(source, directory=tmp_path / "backups")
    service.directory.mkdir(parents=True)
    for index in range(10):
        (service.directory / f"daily-2026-07-{index + 1:02d}.db").write_bytes(b"x")
    for index in range(6):
        (service.directory / f"weekly-2026-W{index + 1:02d}.db").write_bytes(b"x")
    await asyncio.to_thread(service._retention_sync)
    status = await service.status()
    assert status.daily_count == 7 and status.weekly_count == 4
    with pytest.raises(RuntimeError, match="unavailable"):
        await asyncio.to_thread(service._backup_sync, service.directory / "manual.db")
    valid_source = StateDatabase(tmp_path / "valid.db")
    await valid_source.initialize()
    valid_service = BackupService(valid_source, directory=service.directory)
    with pytest.raises(RuntimeError, match="escaped"):
        await asyncio.to_thread(valid_service._backup_sync, tmp_path / "outside.db")


@pytest.mark.asyncio
async def test_backup_run_records_local_error_and_empty_status(tmp_path: Path) -> None:
    service = BackupService(
        StateDatabase(tmp_path / "missing.db"),
        directory=tmp_path / "new-backups",
        wall_clock=lambda: 1_800_000_000,
    )
    assert (await service.status()).daily_count == 0
    with pytest.raises(RuntimeError):
        await service.run_if_due()
    assert (await service.status()).last_error == "RuntimeError"


def test_fast_kline_renderer_returns_full_png() -> None:
    start = 1_700_000_000
    points = []
    for index in range(60):
        opened = 900 + index
        closed = opened + (2 if index % 2 == 0 else -1)
        points.append([start + index * 86400, opened, closed, opened + 4, opened - 3])
    png = render_kline_png("塔菲 市场日K", points)
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.size == (WIDTH, HEIGHT)
    with pytest.raises(ValueError, match="insufficient"):
        render_kline_png("short", points[:29])


def test_log_privacy_filter_redacts_messages_and_identifiers() -> None:
    def record(name: str, message: str) -> dict[str, object]:
        return {"name": name, "message": message, "extra": {}}

    with patch.object(logging_privacy, "default_filter", return_value=True):
        message_record = record("nonebot.message", "hello 1243607273")
        assert logging_privacy._privacy_filter(message_record)  # type: ignore[arg-type]
        assert message_record["message"] == "OneBot message event received [content redacted]"
        normal_record = record(
            "taffy.test",
            "user_id=1243607273 group_id:'99887766' [CQ:at,qq=1243607273] "
            "https://api.example.test/x?app-key=very-secret&other=1",
        )
        assert logging_privacy._privacy_filter(normal_record)  # type: ignore[arg-type]
        rendered = str(normal_record["message"])
        assert "1243607273" not in rendered and "99887766" not in rendered
        assert "[CQ:redacted]" in rendered
        assert "very-secret" not in rendered and "app-key=[redacted]" in rendered
        actual_nonebot_record = record(
            "nonebot",
            "OneBot V11 | [message.private.friend]: Message 1 from 2 'private body'",
        )
        assert logging_privacy._privacy_filter(actual_nonebot_record)  # type: ignore[arg-type]
        assert actual_nonebot_record["message"] == (
            "OneBot message event received [content redacted]"
        )
    with patch.object(logging_privacy, "default_filter", return_value=False):
        assert not logging_privacy._privacy_filter(record("x", "y"))  # type: ignore[arg-type]

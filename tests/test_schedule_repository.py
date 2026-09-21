from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.state import schedule_repository as schedule_module
from src.state.schedule_repository import (
    DEFAULT_TIMEZONE,
    LEGACY_SOURCE_NAME,
    MAX_SCHEDULE_GROUPS,
    MAX_SCHEDULE_TIMES,
    LegacyScheduleImportError,
    ScheduleNotInitializedError,
    ScheduleRepository,
    ScheduleVersionConflictError,
)


@pytest.mark.asyncio
async def test_schedule_config_migrates_atomically_and_only_once(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    legacy = tmp_path / "schedule_config.local.json"
    legacy.write_text(
        json.dumps(
            {
                "enabled": True,
                "warm_cache_enabled": False,
                "alerts_enabled": True,
                "timezone": "Asia/Shanghai",
                "groups": [2002, "1001", "2002"],
                "times": ["20:00", "08:00", "20:00"],
            }
        ),
        encoding="utf-8",
    )
    repository = ScheduleRepository(StateDatabase(path), legacy_path=legacy)

    await repository.initialize()
    config = await repository.get()

    assert config.enabled
    assert not config.warm_cache_enabled
    assert config.alerts_enabled
    assert config.timezone == "Asia/Shanghai"
    # Repository reads are canonical and deterministic regardless of legacy order.
    assert config.groups == (1001, 2002)
    assert config.times == ("08:00", "20:00")
    assert config.version == 1
    marker = await repository.database.read(
        "SELECT source_state, imported_rows FROM legacy_imports WHERE source_name = ?",
        (LEGACY_SOURCE_NAME,),
    )
    assert marker == [{"source_state": "imported", "imported_rows": 5}]

    legacy.write_text("{broken", encoding="utf-8")
    restarted = ScheduleRepository(StateDatabase(path), legacy_path=legacy)
    await restarted.initialize()
    assert await restarted.get() == config


@pytest.mark.asyncio
async def test_absent_schedule_file_creates_safe_defaults_and_marker(tmp_path: Path) -> None:
    repository = ScheduleRepository(
        StateDatabase(tmp_path / "state.db"), legacy_path=tmp_path / "missing.json"
    )

    await repository.initialize()
    config = await repository.get()

    assert config.enabled
    assert config.warm_cache_enabled
    assert config.alerts_enabled
    assert config.timezone == DEFAULT_TIMEZONE
    assert config.groups == ()
    assert config.times == ("08:00", "12:00", "15:00", "17:30", "20:00")
    marker = await repository.database.read(
        "SELECT source_state, imported_rows FROM legacy_imports WHERE source_name = ?",
        (LEGACY_SOURCE_NAME,),
    )
    assert marker == [{"source_state": "absent", "imported_rows": 6}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"times": ["8:00"]}',
        '{"groups": [0]}',
        '{"timezone": "Mars/Olympus"}',
        '{"enabled": 1}',
        '{"unexpected": true}',
        '{"enabled": true, "enabled": false}',
        '{"groups": [NaN]}',
    ],
)
async def test_invalid_legacy_schedule_fails_closed(tmp_path: Path, payload: str) -> None:
    database = StateDatabase(tmp_path / "state.db")
    legacy = tmp_path / "schedule.json"
    legacy.write_text(payload, encoding="utf-8")
    repository = ScheduleRepository(database, legacy_path=legacy)

    with pytest.raises(LegacyScheduleImportError):
        await repository.initialize()

    assert await database.read("SELECT * FROM scheduler_settings") == []
    assert await database.read("SELECT * FROM scheduler_groups") == []
    assert await database.read("SELECT * FROM scheduler_times") == []
    assert (
        await database.read(
            "SELECT * FROM legacy_imports WHERE source_name = ?", (LEGACY_SOURCE_NAME,)
        )
        == []
    )
    with pytest.raises(ScheduleNotInitializedError):
        await repository.get()
    backups = list(tmp_path.glob("schedule.json.invalid-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == payload


@pytest.mark.asyncio
async def test_replace_uses_optimistic_version_and_never_mixes_child_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    legacy = tmp_path / "missing.json"
    first = ScheduleRepository(StateDatabase(path), legacy_path=legacy)
    second = ScheduleRepository(StateDatabase(path), legacy_path=legacy)
    await asyncio.gather(first.initialize(), second.initialize())
    original = await first.get()

    left = {
        "enabled": False,
        "warm_cache_enabled": True,
        "alerts_enabled": False,
        "timezone": "UTC",
        "groups": [111, 222],
        "times": ["01:02", "03:04"],
    }
    right = {
        "enabled": True,
        "warm_cache_enabled": False,
        "alerts_enabled": True,
        "timezone": "Asia/Shanghai",
        "groups": [333],
        "times": ["05:06"],
    }
    outcomes = await asyncio.gather(
        first.replace(left, expected_version=original.version),
        second.replace(right, expected_version=original.version),
        return_exceptions=True,
    )

    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], ScheduleVersionConflictError)
    winner = successes[0]
    current = await first.get()
    assert current == winner
    assert current.version == original.version + 1
    assert (current.groups, current.times) in {
        ((111, 222), ("01:02", "03:04")),
        ((333,), ("05:06",)),
    }

    conflict = conflicts[0]
    assert isinstance(conflict, ScheduleVersionConflictError)
    assert conflict.expected_version == original.version
    assert conflict.actual_version == current.version


@pytest.mark.asyncio
async def test_replace_rejects_invalid_values_before_touching_database(tmp_path: Path) -> None:
    repository = ScheduleRepository(
        StateDatabase(tmp_path / "state.db"), legacy_path=tmp_path / "missing.json"
    )
    await repository.initialize()
    before = await repository.get()

    with pytest.raises(ValueError, match="HH:MM"):
        await repository.replace({"times": ["24:00"]}, expected_version=before.version)
    with pytest.raises(ValueError, match="positive"):
        await repository.replace({"groups": [-1]}, expected_version=before.version)
    with pytest.raises(ValueError, match="groups cannot contain"):
        await repository.replace(
            {"groups": list(range(1, MAX_SCHEDULE_GROUPS + 2))},
            expected_version=before.version,
        )
    with pytest.raises(ValueError, match="times cannot contain"):
        await repository.replace(
            {"times": ["08:00"] * (MAX_SCHEDULE_TIMES + 1)},
            expected_version=before.version,
        )

    assert await repository.get() == before


@pytest.mark.asyncio
async def test_missing_marker_with_existing_schedule_state_fails_closed(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    await database.write(
        """
        INSERT INTO scheduler_settings
            (singleton, enabled, warm_cache_enabled, alerts_enabled, timezone, version)
        VALUES (1, 1, 1, 1, 'UTC', 1)
        """
    )
    repository = ScheduleRepository(database, legacy_path=tmp_path / "missing.json")

    with pytest.raises(LegacyScheduleImportError, match="no legacy import marker"):
        await repository.initialize()

    assert await database.read("SELECT * FROM legacy_imports") == []
    assert (await database.read("SELECT timezone FROM scheduler_settings"))[0]["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_source_change_during_schedule_migration_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "schedule.json"
    legacy.write_text('{"times": ["08:00"]}', encoding="utf-8")
    database = StateDatabase(tmp_path / "state.db")
    real_verify = schedule_module._verify_legacy_source

    def change_then_verify(path: Path, source_state: str, digest: str) -> None:
        path.write_text('{"times": ["09:00"]}', encoding="utf-8")
        real_verify(path, source_state, digest)

    monkeypatch.setattr(schedule_module, "_verify_legacy_source", change_then_verify)
    with pytest.raises(LegacyScheduleImportError, match="changed during migration"):
        await ScheduleRepository(database, legacy_path=legacy).initialize()

    assert await database.read("SELECT * FROM scheduler_settings") == []
    assert await database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
async def test_absent_marker_blocks_late_schedule_file(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    legacy = tmp_path / "late.json"
    first = ScheduleRepository(StateDatabase(path), legacy_path=legacy)
    await first.initialize()
    original = await first.get()

    legacy.write_text('{"enabled": false, "times": ["01:00"]}', encoding="utf-8")
    restarted = ScheduleRepository(StateDatabase(path), legacy_path=legacy)
    await restarted.initialize()

    assert await restarted.get() == original


@pytest.mark.asyncio
async def test_marker_without_settings_fails_instead_of_repairing_defaults(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    legacy = tmp_path / "missing.json"
    repository = ScheduleRepository(StateDatabase(path), legacy_path=legacy)
    await repository.initialize()
    await repository.database.write("DELETE FROM scheduler_settings")

    with pytest.raises(ScheduleNotInitializedError):
        await ScheduleRepository(StateDatabase(path), legacy_path=legacy).initialize()


@pytest.mark.asyncio
async def test_relative_schedule_path_is_anchored_to_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_module, "PROJECT_ROOT", tmp_path)
    legacy = tmp_path / "config" / "schedule.json"
    legacy.parent.mkdir()
    legacy.write_text('{"times": ["06:30"]}', encoding="utf-8")
    repository = ScheduleRepository(
        StateDatabase(tmp_path / "state.db"),
        legacy_path="config/schedule.json",
    )

    assert repository.legacy_path == legacy.resolve()
    await repository.initialize()
    assert (await repository.get()).times == ("06:30",)

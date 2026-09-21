from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import migrate_state
from src.state.watchlist_repository import LegacyWatchlistImportError


def _legacy_files(root: Path) -> dict[str, Path]:
    paths = {
        "sessions": root / "sessions.json",
        "alerts": root / "alerts.json",
        "watchlist": root / "watchlist.json",
        "schedule": root / "schedule.json",
    }
    paths["sessions"].write_text(
        json.dumps({"session": [{"role": "user", "content": "hello"}]}),
        encoding="utf-8",
    )
    paths["alerts"].write_text(
        json.dumps([{"name": "item", "price": 10, "qq": 1}]),
        encoding="utf-8",
    )
    paths["watchlist"].write_text(
        json.dumps({"1": ["item"]}),
        encoding="utf-8",
    )
    paths["schedule"].write_text(
        json.dumps({"groups": [1], "times": ["08:00"]}),
        encoding="utf-8",
    )
    return paths


@pytest.mark.asyncio
async def test_dry_run_validates_copies_without_touching_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _legacy_files(tmp_path)
    original = {name: path.read_bytes() for name, path in sources.items()}
    monkeypatch.setattr(migrate_state, "_configured_legacy_paths", lambda: sources)

    result = await migrate_state.dry_run()

    assert result["status"] == "valid"
    assert result["counts"] == {
        "session_messages": 1,
        "persona_memories": 0,
        "persona_style_profiles": 0,
        "persona_feedback_events": 0,
        "alerts": 1,
        "watchlist_items": 1,
        "broadcast_deliveries": 0,
        "inventory_monitor_accounts": 0,
        "inventory_monitor_subscriptions": 0,
        "inventory_monitor_events": 0,
        "inventory_monitor_deliveries": 0,
        "legacy_markers": 4,
    }
    assert {name: path.read_bytes() for name, path in sources.items()} == original
    assert not list(tmp_path.glob("*.invalid-*.bak"))


@pytest.mark.asyncio
async def test_invalid_dry_run_keeps_backup_artifacts_out_of_source_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _legacy_files(tmp_path)
    sources["watchlist"].write_bytes(b"{broken")
    monkeypatch.setattr(migrate_state, "_configured_legacy_paths", lambda: sources)

    with pytest.raises(LegacyWatchlistImportError):
        await migrate_state.dry_run()

    assert sources["watchlist"].read_bytes() == b"{broken"
    assert not list(tmp_path.glob("*.invalid-*.bak"))


def test_sqlite_backup_api_copies_committed_state(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('durable')")
        connection.commit()
    finally:
        connection.close()

    digest = migrate_state._backup_database_sync(source, destination)

    backup = sqlite3.connect(destination)
    try:
        assert backup.execute("SELECT value FROM sample").fetchone() == ("durable",)
    finally:
        backup.close()
    assert digest == migrate_state._sha256_file(destination)


@pytest.mark.asyncio
async def test_restore_replaces_database_and_preserves_pre_restore_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "data" / "taffy.db"
    source = tmp_path / "rollback.db"
    for path, value in ((target, "current"), (source, "rollback")):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES (?)", (value,))
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(
        migrate_state,
        "load_runtime_settings",
        lambda _root: {"taffy_state_db": str(target)},
    )

    result = await migrate_state.restore_database(source)

    restored = sqlite3.connect(target)
    try:
        assert restored.execute("SELECT value FROM marker").fetchone() == ("rollback",)
    finally:
        restored.close()
    previous = result["pre_restore_backup"]
    assert isinstance(previous, dict)
    previous_path = Path(previous["path"])
    preserved = sqlite3.connect(previous_path)
    try:
        assert preserved.execute("SELECT value FROM marker").fetchone() == ("current",)
    finally:
        preserved.close()
    assert result["status"] == "restored"
    assert result["schema_version"] == 2
    assert result["restored_sha256"] == migrate_state._sha256_file(target)


@pytest.mark.asyncio
async def test_restore_rejects_unversioned_database(tmp_path: Path) -> None:
    source = tmp_path / "unversioned.db"
    connection = sqlite3.connect(source)
    connection.close()

    with pytest.raises(RuntimeError, match="supported rollback range"):
        await migrate_state.restore_database(source)

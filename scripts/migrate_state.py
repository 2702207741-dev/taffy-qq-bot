"""Offline dry-run, apply, verification, and restore for transactional state."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from src.core.config import load_runtime_settings
from src.core.database import PROJECT_ROOT, SCHEMA_VERSION, StateDatabase
from src.state.alert_repository import LEGACY_SOURCE_NAME as ALERT_LEGACY_SOURCE
from src.state.alert_repository import AlertRepository
from src.state.schedule_repository import LEGACY_SOURCE_NAME as SCHEDULE_LEGACY_SOURCE
from src.state.schedule_repository import ScheduleRepository
from src.state.session_repository import LEGACY_SOURCE_NAME as SESSION_LEGACY_SOURCE
from src.state.session_repository import SessionRepository
from src.state.watchlist_repository import LEGACY_SOURCE_NAME as WATCHLIST_LEGACY_SOURCE
from src.state.watchlist_repository import WatchlistRepository


def _configured_legacy_paths() -> dict[str, Path]:
    settings = load_runtime_settings(PROJECT_ROOT)
    configured_schedule = settings.get("taffy_schedule_config")
    schedule = (
        Path(str(configured_schedule)).expanduser()
        if configured_schedule
        else PROJECT_ROOT / "schedule_config.local.json"
    )
    if not schedule.is_absolute():
        schedule = PROJECT_ROOT / schedule
    return {
        "sessions": PROJECT_ROOT / "data" / "sessions.json",
        "alerts": PROJECT_ROOT / "alerts.json",
        "watchlist": PROJECT_ROOT / "watchlist.json",
        "schedule": schedule.resolve(),
    }


async def _initialize_repositories(
    database: StateDatabase,
    paths: dict[str, Path],
) -> None:
    await SessionRepository(database, legacy_path=paths["sessions"]).initialize()
    await AlertRepository(database, legacy_path=paths["alerts"]).initialize()
    await WatchlistRepository(database, legacy_path=paths["watchlist"]).initialize()
    await ScheduleRepository(database, legacy_path=paths["schedule"]).initialize()


async def _state_counts(database: StateDatabase) -> dict[str, int]:
    tables = {
        "session_messages": "session_messages",
        "persona_memories": "persona_memories",
        "persona_style_profiles": "persona_style_profiles",
        "persona_feedback_events": "persona_feedback_events",
        "alerts": "alerts",
        "watchlist_items": "watchlist_items",
        "broadcast_deliveries": "broadcast_deliveries",
        "inventory_monitor_accounts": "inventory_monitor_accounts",
        "inventory_monitor_subscriptions": "inventory_monitor_subscriptions",
        "inventory_monitor_events": "inventory_monitor_events",
        "inventory_monitor_deliveries": "inventory_monitor_deliveries",
    }
    counts: dict[str, int] = {}
    for label, table in tables.items():
        row = (await database.read(f"SELECT COUNT(*) AS count FROM {table}"))[0]
        counts[label] = int(row["count"])
    counts["legacy_markers"] = int(
        (await database.read("SELECT COUNT(*) AS count FROM legacy_imports"))[0]["count"]
    )
    return counts


async def dry_run() -> dict[str, object]:
    """Validate copies of every legacy source in a disposable database."""

    sources = _configured_legacy_paths()
    with tempfile.TemporaryDirectory(prefix="taffy-state-dry-run-") as temporary:
        root = Path(temporary)
        copied: dict[str, Path] = {}
        for name, source in sources.items():
            destination = root / f"{name}.json"
            if await asyncio.to_thread(source.is_file):
                await asyncio.to_thread(shutil.copyfile, source, destination)
            copied[name] = destination

        database = StateDatabase(root / "dry-run.db")
        await _initialize_repositories(database, copied)
        return {
            "status": "valid",
            "mode": "dry-run",
            "counts": await _state_counts(database),
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database_sync(source: Path, destination: Path) -> str:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("backup source must be a regular SQLite file")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(destination.parent, 0o700)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"backup destination already exists: {destination}")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            check = destination_connection.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]).casefold() != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        finally:
            if destination_connection is not None:
                destination_connection.close()
            if source_connection is not None:
                source_connection.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(destination, 0o600)
    with destination.open("rb+") as file:
        os.fsync(file.fileno())
    return _sha256_file(destination)


async def apply_migration() -> dict[str, object]:
    """Back up any existing database, then atomically import each legacy store."""

    settings = load_runtime_settings(PROJECT_ROOT)
    database = StateDatabase(settings.get("taffy_state_db") or None)
    backup_info: dict[str, str] | None = None
    if await asyncio.to_thread(database.path.is_file):
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = (
            database.path.parent
            / "backups"
            / f"taffy-before-migrate-{timestamp}-{time.time_ns()}.db"
        )
        digest = await asyncio.to_thread(_backup_database_sync, database.path, backup)
        backup_info = {"path": str(backup.resolve()), "sha256": digest}

    await _initialize_repositories(database, _configured_legacy_paths())
    if not await database.healthcheck() or not await database.integrity_check():
        raise RuntimeError("SQLite verification failed after migration")
    return {
        "status": "applied",
        "mode": "apply",
        "database_backup_created": backup_info is not None,
        "database_backup": backup_info,
        "counts": await _state_counts(database),
    }


def _database_version_sync(path: Path) -> int:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("restore source must be a regular SQLite file")
    connection = sqlite3.connect(path)
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).casefold() != "ok":
            raise RuntimeError("restore source failed SQLite integrity check")
        row = connection.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
    finally:
        connection.close()
    if not 1 <= version <= SCHEMA_VERSION:
        raise RuntimeError(
            f"restore source schema {version} is outside the supported rollback range"
        )
    return version


def _replace_database_sync(source: Path, destination: Path) -> tuple[int, str]:
    """Verify *source*, copy it through SQLite, then atomically replace *destination*."""

    version = _database_version_sync(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise RuntimeError("refusing to replace a symlinked state database")
    temporary = destination.parent / f".{destination.name}.restore-{time.time_ns()}.tmp"
    try:
        digest = _backup_database_sync(source, temporary)
        if _database_version_sync(temporary) != version:
            raise RuntimeError("restored SQLite copy changed schema version")
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, destination)
        if os.name == "posix":
            os.chmod(destination, 0o600)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return version, digest
    finally:
        temporary.unlink(missing_ok=True)


async def restore_database(backup_path: Path) -> dict[str, object]:
    """Restore one verified migration backup with a pre-restore safety snapshot."""

    settings = load_runtime_settings(PROJECT_ROOT)
    database = StateDatabase(settings.get("taffy_state_db") or None)
    source = backup_path.expanduser().resolve()
    if source == database.path.resolve():
        raise RuntimeError("restore source and target database must be different files")
    await asyncio.to_thread(_database_version_sync, source)

    previous_info: dict[str, str] | None = None
    if await asyncio.to_thread(database.path.is_file):
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        previous = (
            database.path.parent
            / "backups"
            / f"taffy-before-restore-{timestamp}-{time.time_ns()}.db"
        )
        previous_digest = await asyncio.to_thread(
            _backup_database_sync,
            database.path,
            previous,
        )
        previous_info = {"path": str(previous.resolve()), "sha256": previous_digest}

    version, digest = await asyncio.to_thread(
        _replace_database_sync,
        source,
        database.path,
    )
    return {
        "status": "restored",
        "mode": "restore",
        "schema_version": version,
        "restored_sha256": digest,
        "pre_restore_backup": previous_info,
    }


async def verify() -> dict[str, object]:
    settings = load_runtime_settings(PROJECT_ROOT)
    database = StateDatabase(settings.get("taffy_state_db") or None)
    if not await asyncio.to_thread(database.path.is_file):
        raise RuntimeError("state database does not exist")
    if not await database.healthcheck() or not await database.integrity_check():
        raise RuntimeError("state database schema or integrity check failed")
    markers = await database.read(
        "SELECT source_name, source_state FROM legacy_imports ORDER BY source_name"
    )
    expected_sources = {
        SESSION_LEGACY_SOURCE,
        ALERT_LEGACY_SOURCE,
        WATCHLIST_LEGACY_SOURCE,
        SCHEDULE_LEGACY_SOURCE,
    }
    observed_sources = {str(marker["source_name"]) for marker in markers}
    if observed_sources != expected_sources:
        raise RuntimeError("legacy migration markers do not match the expected sources")
    return {
        "status": "valid",
        "mode": "verify",
        "legacy_migrations": len(markers),
        "counts": await _state_counts(database),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate copies only")
    mode.add_argument("--verify", action="store_true", help="verify an existing state database")
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply migration; requires --confirm-bot-stopped",
    )
    mode.add_argument(
        "--restore",
        type=Path,
        metavar="BACKUP_DB",
        help="restore a verified SQLite backup; requires bot-stopped confirmation",
    )
    parser.add_argument(
        "--confirm-bot-stopped",
        "--confirm-old-bot-stopped",
        dest="confirm_bot_stopped",
        action="store_true",
        help="confirm every bot process that can write state is stopped",
    )
    arguments = parser.parse_args()
    if (arguments.apply or arguments.restore) and not arguments.confirm_bot_stopped:
        parser.error("--apply/--restore requires --confirm-bot-stopped")
    return arguments


async def _main() -> int:
    arguments = _arguments()
    if arguments.dry_run:
        result = await dry_run()
    elif arguments.apply:
        result = await apply_migration()
    elif arguments.restore:
        result = await restore_database(arguments.restore)
    else:
        result = await verify()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

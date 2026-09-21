"""Verified automatic SQLite backups with bounded retention.

Backs up the state database plus every auxiliary database (market catalog,
market history and telemetry) as one atomic "backup set" per retention slot,
with a JSON manifest recording SHA-256 digests and the state schema version.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.database import StateDatabase

from .config import project_path_setting
from .resource_governor import resource_governor
from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKUP_DIRECTORY = PROJECT_ROOT / "data" / "backups" / "automatic"

_STATE_DB_NAME = "state"
_AUX_DB_NAMES = (
    # (setting, module default path, logical name)
    ("TAFFY_MARKET_CATALOG_DB", "data/market_catalog.db", "catalog"),
    ("TAFFY_MARKET_HISTORY_DB", "data/market_history.db", "history"),
    ("TAFFY_TELEMETRY_DB", "data/telemetry.db", "telemetry"),
)


@dataclass(frozen=True, slots=True)
class BackupStatus:
    last_success_at: int | None
    last_error: str | None
    daily_count: int
    weekly_count: int


class BackupService:
    def __init__(
        self,
        database: StateDatabase,
        *,
        directory: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
        extra_databases: Mapping[str, Path] | None = None,
    ) -> None:
        self.database = database
        self.directory = (
            directory
            or project_path_setting("TAFFY_AUTOMATIC_BACKUP_DIR", DEFAULT_BACKUP_DIRECTORY)
        ).resolve()
        self.extra_databases = dict(extra_databases) if extra_databases is not None else None
        self._clock = wall_clock
        self._lock = asyncio.Lock()
        self._last_success_at: int | None = None
        self._last_error: str | None = None

    def _auxiliary_databases(self) -> dict[str, Path]:
        if self.extra_databases is not None:
            return dict(self.extra_databases)
        databases: dict[str, Path] = {}
        for setting, default, name in _AUX_DB_NAMES:
            databases[name] = project_path_setting(setting, PROJECT_ROOT / default)
        return databases

    def _databases(self) -> list[tuple[str, Path]]:
        databases = self._auxiliary_databases()
        databases[_STATE_DB_NAME] = self.database.path
        return sorted(databases.items())

    def _backup_one(self, source: Path, destination: Path) -> None:
        source = source.resolve()
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("state database is unavailable")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._harden_permissions(self.directory, directory=True)
        if destination.parent.resolve() != self.directory:
            raise RuntimeError("backup destination escaped its directory")
        temporary = self.directory / f".{destination.name}.{time.time_ns()}.tmp"
        source_connection: sqlite3.Connection | None = None
        target_connection: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(source)
            target_connection = sqlite3.connect(temporary)
            self._harden_permissions(temporary)
            source_connection.backup(target_connection)
            target_connection.commit()
            row = target_connection.execute("PRAGMA quick_check(1)").fetchone()
            if row is None or str(row[0]).casefold() != "ok":
                raise RuntimeError("automatic backup integrity check failed")
            target_connection.close()
            target_connection = None
            with temporary.open("rb+") as file:
                os.fsync(file.fileno())
            os.replace(temporary, destination)
            self._harden_permissions(destination)
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None:
                source_connection.close()
            temporary.unlink(missing_ok=True)

    def _backup_sync(self, destination: Path) -> None:
        """Back up only the state database to an explicit destination."""

        self._backup_one(self.database.path, destination)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(65_536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _user_version(path: Path) -> int | None:
        try:
            connection = sqlite3.connect(path)
            try:
                row = connection.execute("PRAGMA user_version").fetchone()
                return int(row[0]) if row is not None else None
            finally:
                connection.close()
        except sqlite3.Error:
            return None

    @staticmethod
    def _manifest_path(primary: Path) -> Path:
        return primary.with_suffix(".json")

    def _manifest_sync(self, primary: Path, kind: str) -> None:
        manifest: dict[str, Any] = {
            "kind": kind,
            "created_at": int(self._clock()),
            "schema_version": None,
            "databases": {},
        }
        for name, source in self._databases():
            if name == _STATE_DB_NAME:
                destination = primary
            else:
                destination = primary.with_name(f"{primary.stem}.{name}.db")
            if not source.is_file() or source.is_symlink():
                if name == _STATE_DB_NAME:
                    raise RuntimeError("state database is unavailable")
                manifest["databases"][name] = None
                continue
            self._backup_one(source, destination)
            manifest["databases"][name] = {
                "file": destination.name,
                "sha256": self._file_sha256(destination),
            }
        manifest["schema_version"] = self._user_version(primary)
        destination = self._manifest_path(primary)
        temporary = self.directory / f".{destination.name}.{time.time_ns()}.tmp"
        try:
            temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            self._harden_permissions(temporary)
            with temporary.open("rb+") as file:
                os.fsync(file.fileno())
            os.replace(temporary, destination)
            self._harden_permissions(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _harden_permissions(path: Path, *, directory: bool = False) -> None:
        """Keep automatic backups private on POSIX regardless of the umask.

        The state database contains private chat and personal data.  Automatic
        backups must therefore default to ``0700`` directories and ``0600``
        files exactly like the offline migration snapshots do.  Windows ACLs
        are managed by the operator through the containing ``data/`` directory.
        """

        if os.name != "posix":
            return
        os.chmod(path, 0o700 if directory else 0o600)

    def _clean_orphans(self) -> None:
        """Remove stray WAL/shm sidecars whose primary backup is missing."""

        for pattern in (
            "daily-*.db-wal",
            "daily-*.db-shm",
            "weekly-*.db-wal",
            "weekly-*.db-shm",
        ):
            for path in self.directory.glob(pattern):
                if not path.is_file() or path.is_symlink():
                    continue
                primary = path.with_name(path.name[:-4])
                if not primary.is_file():
                    path.unlink(missing_ok=True)

    def _retention_sync(self) -> None:
        if not self.directory.is_dir():
            return
        self._clean_orphans()
        for prefix, keep in (("daily", 7), ("weekly", 4)):
            groups: dict[str, list[Path]] = {}
            for path in self.directory.glob(f"{prefix}-*"):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.parent.resolve() != self.directory
                ):
                    continue
                key = path.name.split(".")[0]
                groups.setdefault(key, []).append(path)
            for expired in sorted(groups, reverse=True)[keep:]:
                for path in groups[expired]:
                    path.unlink(missing_ok=True)

    async def run_if_due(self) -> bool:
        if resource_governor.game_running:
            return False
        async with self._lock:
            now = int(self._clock())
            current = datetime.fromtimestamp(now, tz=_MARKET_TZ)
            daily = self.directory / f"daily-{current:%Y-%m-%d}.db"
            weekly = self.directory / f"weekly-{current:%G-W%V}.db"
            if self._manifest_path(daily).is_file() and self._manifest_path(weekly).is_file():
                self._last_success_at = max(
                    int(daily.stat().st_mtime) if daily.is_file() else 0,
                    int(weekly.stat().st_mtime) if weekly.is_file() else 0,
                )
                self._last_error = None
                return False
            try:
                if not self._manifest_path(daily).exists():
                    await asyncio.to_thread(self._manifest_sync, daily, "daily")
                if not self._manifest_path(weekly).exists():
                    await asyncio.to_thread(self._manifest_sync, weekly, "weekly")
                await asyncio.to_thread(self._retention_sync)
            except Exception as exc:
                self._last_error = type(exc).__name__
                raise
            self._last_success_at = now
            self._last_error = None
            return True

    async def status(self) -> BackupStatus:
        def counts() -> tuple[int, int]:
            if not self.directory.is_dir():
                return 0, 0
            daily = {
                path.name.split(".")[0]
                for path in self.directory.glob("daily-*")
                if path.is_file() and not path.is_symlink()
            }
            weekly = {
                path.name.split(".")[0]
                for path in self.directory.glob("weekly-*")
                if path.is_file() and not path.is_symlink()
            }
            return len(daily), len(weekly)

        daily, weekly = await asyncio.to_thread(counts)
        return BackupStatus(self._last_success_at, self._last_error, daily, weekly)


__all__ = ["BackupService", "BackupStatus"]

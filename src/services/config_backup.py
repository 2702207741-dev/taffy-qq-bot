"""Daily configuration snapshots with SHA-256 manifests for safe rollback.

The bot never rewrites its own environment files, so a tampered or misedited
``.env`` is operator-caused.  This module keeps read-only snapshots of every
runtime config file so a bad edit can be detected by hash comparison and
rolled back in seconds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import project_path_setting
from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_DIRECTORY = PROJECT_ROOT / "data" / "config-backups"
_CONFIG_RETENTION_DAILY = 30
_MANIFEST_NAME = "manifest.json"


def config_files(root: Path) -> list[Path]:
    """Return the runtime env files that exist at ``root`` (never the template)."""

    files: list[Path] = []
    for path in root.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        if not path.name.startswith(".env"):
            continue
        if path.name == ".env.example" or path.name.endswith(".example"):
            continue
        files.append(path)
    return sorted(files)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_payload(
    root: Path,
    files: list[Path],
    created_at: int,
) -> dict[str, Any]:
    return {
        "created_at": created_at,
        "files": {path.name: file_sha256(path) for path in files},
    }


@dataclass(frozen=True, slots=True)
class ConfigSnapshotStatus:
    latest_at: int | None
    latest_dir: str | None
    count: int
    verify_ok: bool | None


class ConfigSnapshotService:
    def __init__(
        self,
        *,
        root: Path | None = None,
        directory: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = (root or PROJECT_ROOT).resolve()
        self.directory = (
            directory or project_path_setting("TAFFY_CONFIG_BACKUP_DIR", DEFAULT_SNAPSHOT_DIRECTORY)
        ).resolve()
        self._clock = wall_clock
        self._lock = asyncio.Lock()

    @staticmethod
    def _harden(path: Path, *, directory: bool = False) -> None:
        if os.name != "posix":
            return
        os.chmod(path, 0o700 if directory else 0o600)

    def _manifest_path(self, destination: Path) -> Path:
        return destination / _MANIFEST_NAME

    def snapshot_sync(self, destination: Path) -> Path:
        """Snapshot every config file into ``destination`` and return the manifest."""

        files = config_files(self.root)
        if not files:
            raise RuntimeError("no runtime config files found")
        destination.mkdir(parents=True, exist_ok=True)
        self._harden(destination, directory=True)
        manifest = _manifest_payload(self.root, files, int(self._clock()))
        for name, digest in manifest["files"].items():
            source = self.root / name
            copy = destination / name
            temporary = destination / f".{name}.{time.time_ns()}.tmp"
            try:
                temporary.write_bytes(source.read_bytes())
                self._harden(temporary)
                with temporary.open("rb+") as file:
                    os.fsync(file.fileno())
                os.replace(temporary, copy)
            finally:
                temporary.unlink(missing_ok=True)
            self._harden(copy)
            if file_sha256(copy) != digest:
                raise RuntimeError("configuration snapshot copy failed verification")
        manifest_path = self._manifest_path(destination)
        temporary = destination / f".{_MANIFEST_NAME}.{time.time_ns()}.tmp"
        try:
            temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            self._harden(temporary)
            with temporary.open("rb+") as file:
                os.fsync(file.fileno())
            os.replace(temporary, manifest_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._harden(manifest_path)
        return manifest_path

    def verify_sync(self, destination: Path) -> dict[str, Any]:
        """Compare live config files against a snapshot manifest."""

        manifest_path = self._manifest_path(destination)
        if not manifest_path.is_file():
            return {"ok": False, "missing": [_MANIFEST_NAME], "mismatched": []}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        live = {path.name: file_sha256(path) for path in config_files(self.root)}
        missing: list[str] = []
        mismatched: list[str] = []
        for name, digest in manifest["files"].items():
            if name not in live:
                missing.append(name)
            elif live[name] != digest:
                mismatched.append(name)
        return {"ok": not missing and not mismatched, "missing": missing, "mismatched": mismatched}

    async def run_if_due(self) -> bool:
        """Create one daily snapshot per calendar day; keep the newest 30."""

        async with self._lock:
            current = datetime.fromtimestamp(int(self._clock()), tz=_MARKET_TZ)
            daily = self.directory / f"config-daily-{current:%Y-%m-%d}"
            if self._manifest_path(daily).is_file():
                return False
            await asyncio.to_thread(self.snapshot_sync, daily)
            await asyncio.to_thread(self._retention_sync)
            return True

    def _retention_sync(self) -> None:
        if not self.directory.is_dir():
            return
        candidates = sorted(
            (
                path
                for path in self.directory.glob("config-daily-*")
                if path.is_dir()
                and not path.is_symlink()
                and path.parent.resolve() == self.directory
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        for expired in candidates[_CONFIG_RETENTION_DAILY:]:
            for path in sorted(expired.iterdir(), reverse=True):
                try:
                    path.unlink(missing_ok=True)
                except (IsADirectoryError, PermissionError):
                    # A stray subdirectory inside a snapshot must not make the
                    # daily cleanup fail forever; leave it and move on.
                    continue
            try:
                expired.rmdir()
            except OSError:
                continue

    async def status(self) -> ConfigSnapshotStatus:
        def latest() -> tuple[int | None, str | None, int]:
            if not self.directory.is_dir():
                return None, None, 0
            snapshots = sorted(
                (
                    path
                    for path in self.directory.glob("config-daily-*")
                    if path.is_dir() and self._manifest_path(path).is_file()
                ),
                key=lambda path: path.name,
            )
            if not snapshots:
                return None, None, 0
            newest = snapshots[-1]
            created_at = json.loads(self._manifest_path(newest).read_text(encoding="utf-8")).get(
                "created_at"
            )
            return created_at, newest.name, len(snapshots)

        created_at, name, count = await asyncio.to_thread(latest)
        return ConfigSnapshotStatus(created_at, name, count, None)


__all__ = ["ConfigSnapshotService", "ConfigSnapshotStatus", "config_files", "file_sha256"]

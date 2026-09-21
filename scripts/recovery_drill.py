"""Offline recovery drill: prove a backup set can be restored cleanly.

Read-only verification that runs entirely on the backup artifacts — it never
touches live databases or config files.  Run it after every backup rotation:

    uv run --frozen python -m scripts.recovery_drill

Options:
    --backup <dir|primary.db>   test a specific backup set (default: latest daily)
    --config-snapshot <dir>     test a specific config snapshot (default: latest)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from src.services.config_backup import ConfigSnapshotService

DEFAULT_BACKUP_DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "backups" / "automatic"
_CONFIG_MANIFEST_NAME = "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_of(manifest_path: Path) -> dict[str, object]:
    if not manifest_path.is_file():
        raise SystemExit(f"no manifest file: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("databases"), dict):
        raise SystemExit(f"malformed manifest: {manifest_path}")
    return manifest


def _latest_backup_manifest(directory: Path) -> Path:
    if not directory.is_dir():
        raise SystemExit(f"backup directory missing: {directory}")
    candidates = sorted(
        (
            path
            for path in directory.glob("daily-*.json")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.name,
    )
    if not candidates:
        raise SystemExit(f"no daily backup sets found in {directory}")
    return candidates[-1]


def _verify_databases(manifest: dict[str, object], directory: Path) -> list[str]:
    errors: list[str] = []
    databases = manifest["databases"]
    assert isinstance(databases, dict)
    for name, entry in databases.items():
        if entry is None:
            print(f"  skip {name}: absent at backup time")
            continue
        assert isinstance(entry, dict)
        file_name = entry.get("file")
        expected = entry.get("sha256")
        if not isinstance(file_name, str) or not isinstance(expected, str):
            errors.append(f"{name}: malformed manifest entry")
            continue
        backup = (directory / file_name).resolve()
        if backup.parent != directory:
            errors.append(f"{name}: escaped path")
            continue
        if not backup.is_file():
            errors.append(f"{name}: missing {file_name}")
            continue
        if _sha256(backup) != expected:
            errors.append(f"{name}: SHA-256 mismatch")
            continue
        connection = sqlite3.connect(backup)
        try:
            row = connection.execute("PRAGMA quick_check(1)").fetchone()
            if row is None or str(row[0]).casefold() != "ok":
                errors.append(f"{name}: quick_check failed")
                continue
            version = connection.execute("PRAGMA user_version").fetchone()
        finally:
            connection.close()
        print(f"  ok {name}: {file_name} (schema {version[0] if version else '?'})")
    return errors


def _verify_config_snapshot(directory: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = directory / _CONFIG_MANIFEST_NAME
    if not manifest_path.is_file():
        errors.append("config snapshot: manifest missing")
        return errors
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, dict):
        errors.append("config snapshot: malformed manifest")
        return errors
    for name, expected in files.items():
        if not isinstance(expected, str):
            errors.append(f"config {name}: malformed digest")
            continue
        copy = directory / name
        if not copy.is_file():
            errors.append(f"config {name}: missing")
            continue
        if _sha256(copy) != expected:
            errors.append(f"config {name}: SHA-256 mismatch")
            continue
        print(f"  ok config {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--config-snapshot", type=Path, default=None)
    args = parser.parse_args()

    backup_manifest = args.backup or _latest_backup_manifest(DEFAULT_BACKUP_DIRECTORY)
    if backup_manifest.is_dir():
        raise SystemExit("--backup must point at a manifest (.json) or primary .db file")
    if backup_manifest.suffix == ".db":
        backup_manifest = backup_manifest.with_suffix(".json")
    backup_directory = backup_manifest.parent.resolve()
    print(f"backup set: {backup_manifest.name} in {backup_directory}")
    errors = _verify_databases(_manifest_of(backup_manifest), backup_directory)

    config_directory = args.config_snapshot or ConfigSnapshotService().directory
    candidates = sorted(
        (
            path
            for path in config_directory.glob("config-daily-*")
            if path.is_dir() and (path / _CONFIG_MANIFEST_NAME).is_file()
        ),
        key=lambda path: path.name,
    )
    if not candidates:
        print("config snapshot: none found (skipping)")
    else:
        latest = candidates[-1]
        print(f"config snapshot: {latest}")
        errors.extend(_verify_config_snapshot(latest))

    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("recovery drill PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

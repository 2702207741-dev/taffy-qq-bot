"""Back up and clear active chat history without touching other bot state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from src.core.config import load_runtime_settings
from src.core.database import PROJECT_ROOT


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _verify_database(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("state database must be a regular file")
    with _connect(path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or str(result[0]).casefold() != "ok":
            raise RuntimeError("state database failed SQLite quick_check")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "session_messages" not in tables:
            raise RuntimeError("state database does not contain session_messages")


def inspect_memory(database_path: Path) -> dict[str, int]:
    """Return bounded metadata without reading or printing message contents."""

    _verify_database(database_path)
    with _connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS messages,
                COUNT(DISTINCT session_key) AS sessions,
                COALESCE(SUM(length(content)), 0) AS characters,
                SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS assistant_messages
            FROM session_messages
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("could not inspect chat memory")
    return {
        "messages": int(row[0]),
        "sessions": int(row[1]),
        "characters": int(row[2]),
        "assistant_messages": int(row[3] or 0),
    }


def _backup_database(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"backup destination already exists: {destination}")
    source_connection = _connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if result is None or str(result[0]).casefold() != "ok":
            raise RuntimeError("chat-memory backup failed SQLite quick_check")
    except BaseException:
        destination_connection.close()
        source_connection.close()
        destination.unlink(missing_ok=True)
        raise
    destination_connection.close()
    source_connection.close()
    return _sha256(destination)


def _clear_legacy_json(path: Path, backup_dir: Path, stamp: str) -> dict[str, str] | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("legacy session path must be a regular file")
    backup = backup_dir / f"sessions-before-memory-clean-{stamp}.json"
    shutil.copy2(path, backup)
    digest = _sha256(backup)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump({}, temporary, ensure_ascii=False)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
    return {"path": str(backup.resolve()), "sha256": digest}


def clean_active_memory(
    database_path: Path,
    *,
    legacy_path: Path | None = None,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Create verified backups, then atomically remove active chat history."""

    database_path = database_path.expanduser().resolve()
    before = inspect_memory(database_path)
    target_backup_dir = (backup_dir or database_path.parent / "backups").resolve()
    target_backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{time.time_ns()}"
    database_backup = target_backup_dir / f"taffy-before-memory-clean-{stamp}.db"
    database_digest = _backup_database(database_path, database_backup)

    with _connect(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM session_messages")
            connection.execute("DELETE FROM session_leases")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    after = inspect_memory(database_path)
    if after["messages"] != 0 or after["sessions"] != 0:
        raise RuntimeError("chat-memory cleanup verification failed")

    legacy_backup = None
    if legacy_path is not None:
        legacy_backup = _clear_legacy_json(legacy_path.resolve(), target_backup_dir, stamp)

    return {
        "status": "cleaned",
        "before": before,
        "after": after,
        "database_backup": {
            "path": str(database_backup.resolve()),
            "sha256": database_digest,
        },
        "legacy_backup": legacy_backup,
    }


def _configured_paths() -> tuple[Path, Path]:
    settings = load_runtime_settings(PROJECT_ROOT)
    database = Path(settings.get("taffy_state_db") or PROJECT_ROOT / "data" / "taffy.db")
    if not database.is_absolute():
        database = PROJECT_ROOT / database
    return database, PROJECT_ROOT / "data" / "sessions.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create verified backups and clear active chat history",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    database, legacy = _configured_paths()
    result: dict[str, Any]
    if args.apply:
        result = clean_active_memory(database, legacy_path=legacy)
    else:
        result = {"status": "dry-run", "would_clear": inspect_memory(database)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

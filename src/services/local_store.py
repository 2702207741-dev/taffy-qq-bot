"""Shared wiring for service-owned standalone SQLite database files.

Several services keep small analysis databases (market history, catalog
cache, index closes, telemetry) separate from the durable bot state.
They share one connection recipe: refuse symlinked targets, create the
parent directory, enable WAL-safe busy handling and row mapping.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")


def connect_local_database(
    path: Path,
    *,
    timeout_seconds: float,
    label: str,
) -> sqlite3.Connection:
    """Open one short-lived connection with the shared safety recipe."""

    if path.exists() and path.is_symlink():
        raise OSError(f"refusing to open a symlinked {label}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    return connection


async def run_local_call(
    path: Path,
    callback: Callable[[sqlite3.Connection], _T],
    *,
    timeout_seconds: float,
    label: str,
) -> _T:
    """Run one committing callback against a fresh worker-thread connection."""

    def run() -> _T:
        connection = connect_local_database(
            path,
            timeout_seconds=timeout_seconds,
            label=label,
        )
        try:
            result = callback(connection)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    return await asyncio.to_thread(run)

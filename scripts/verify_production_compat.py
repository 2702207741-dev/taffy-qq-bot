"""Verify rewritten state layer compatibility against a production backup copy.

Copies the newest automatic state backup into a temporary directory, opens it
with the current StateDatabase in fail-closed mode (offline migrations only),
and confirms schema version, integrity and readable rows for every durable
table.  The production database itself is never opened for writing.

Usage:
    uv run --frozen python scripts/verify_production_compat.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.database import SCHEMA_VERSION, StateDatabase  # noqa: E402

BACKUP_DIRECTORY = PROJECT_ROOT / "data" / "backups" / "automatic"

TABLES = (
    "app_meta",
    "legacy_imports",
    "session_messages",
    "session_leases",
    "persona_memories",
    "persona_style_profiles",
    "persona_feedback_events",
    "alerts",
    "watchlist_items",
    "scheduler_settings",
    "scheduler_groups",
    "scheduler_times",
    "broadcast_deliveries",
    "background_job_runs",
    "feature_permissions",
    "usage_log",
    "inventory_monitor_accounts",
    "inventory_monitor_subscriptions",
    "inventory_monitor_item_history",
    "inventory_monitor_events",
    "inventory_monitor_deliveries",
)


def latest_state_backup() -> Path:
    candidates = sorted(
        path
        for path in BACKUP_DIRECTORY.glob("*.db")
        if path.suffix == ".db" and "." not in path.stem
    )
    if not candidates:
        raise FileNotFoundError(f"no state backups found under {BACKUP_DIRECTORY}")
    return candidates[-1]


async def verify(copy: Path) -> dict[str, int]:
    # The offline migration chain runs first (exactly as scripts.migrate_state
    # --apply would in production); only then does the runtime schema apply.
    migrator = StateDatabase(copy, allow_schema_migrations=True)
    await migrator.initialize()
    database = StateDatabase(copy, allow_schema_migrations=False)
    await database.initialize()
    if not await database.integrity_check():
        raise RuntimeError("integrity check failed on the backup copy")
    if not await database.healthcheck():
        raise RuntimeError("health check failed on the backup copy")
    counts: dict[str, int] = {}
    for table in TABLES:
        rows = await database.read(f"SELECT COUNT(*) AS total FROM {table}")  # noqa: S608
        counts[table] = int(rows[0]["total"])
    return counts


def main() -> int:
    source = latest_state_backup()
    with tempfile.TemporaryDirectory(prefix="taffy-compat-") as temporary:
        copy = Path(temporary) / source.name
        shutil.copy2(source, copy)
        print(f"backup: {source.name} ({source.stat().st_size:,} bytes)")
        counts = asyncio.run(verify(copy))
    print(f"schema: v{SCHEMA_VERSION}, integrity + health: ok")
    for table, total in counts.items():
        print(f"  {table:38s} {total:>8,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

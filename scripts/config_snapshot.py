"""Snapshot, verify, and restore the bot's runtime configuration files.

Snapshots copy every ``.env*`` runtime file (never ``.env.example``) into a
directory with a SHA-256 manifest.  Verification compares live files against
the latest snapshot, and restore rolls files back from a snapshot.

Usage:
    python scripts/config_snapshot.py snapshot
    python scripts/config_snapshot.py verify
    python scripts/config_snapshot.py restore --snapshot <dir> --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from src.services.config_backup import PROJECT_ROOT, ConfigSnapshotService, file_sha256


def _latest_snapshot(service: ConfigSnapshotService) -> Path | None:
    if not service.directory.is_dir():
        return None
    snapshots = sorted(
        (
            path
            for path in service.directory.glob("config-daily-*")
            if path.is_dir() and (path / "manifest.json").is_file()
        ),
        key=lambda path: path.name,
    )
    return snapshots[-1] if snapshots else None


def _cmd_snapshot(service: ConfigSnapshotService) -> int:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    destination = service.directory / f"manual-{timestamp}"
    manifest = service.snapshot_sync(destination)
    print(f"snapshot written: {destination}")
    print(f"manifest: {manifest}")
    return 0


def _cmd_verify(service: ConfigSnapshotService) -> int:
    snapshot = _latest_snapshot(service)
    if snapshot is None:
        print("no snapshot found; run `snapshot` first", file=sys.stderr)
        return 2
    result = service.verify_sync(snapshot)
    for name in result["missing"]:
        print(f"MISSING: {name}")
    for name in result["mismatched"]:
        print(f"CHANGED: {name} (live content differs from snapshot)")
    if result["ok"]:
        print(f"OK: live config matches snapshot {snapshot.name}")
        return 0
    print("config verification FAILED", file=sys.stderr)
    return 1


def _safe_restore_destination(service: ConfigSnapshotService, name: str) -> Path | None:
    """Resolve one manifest entry to a target strictly inside the config root.

    Manifest files live in an operator-provided directory, so their names are
    untrusted input: absolute paths and ``..`` segments must never escape the
    configured root.
    """

    if not isinstance(name, str) or not name.strip():
        return None
    candidate = Path(name)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    resolved = (service.root / candidate).resolve()
    root = service.root.resolve()
    if resolved == root or root not in resolved.parents:
        return None
    return resolved


def _cmd_restore(service: ConfigSnapshotService, snapshot: Path, confirm: bool) -> int:
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.is_file():
        print(f"no manifest in snapshot: {snapshot}", file=sys.stderr)
        return 2
    if not confirm:
        print("restore requires --yes", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest["files"]:
        print("snapshot manifest is empty", file=sys.stderr)
        return 2
    destinations: dict[str, Path] = {}
    for name in manifest["files"]:
        destination = _safe_restore_destination(service, str(name))
        if destination is None:
            print(f"refusing unsafe manifest path: {name}", file=sys.stderr)
            return 2
        destinations[str(name)] = destination
    for name, _destination in destinations.items():
        copy = snapshot / name
        if not copy.is_file() or file_sha256(copy) != manifest["files"][name]:
            print(f"snapshot file failed integrity: {name}", file=sys.stderr)
            return 2
    for name, destination in destinations.items():
        relative = Path(name)
        temporary = service.root / f".{relative.name}.{time.time_ns()}.restore-tmp"
        try:
            temporary.write_bytes((snapshot / relative).read_bytes())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"restored: {destination}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("snapshot", "verify", "restore"),
        help="snapshot config, verify live config, or restore from a snapshot",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="snapshot directory to restore from (restore only)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm restore (restore only)",
    )
    args = parser.parse_args()
    service = ConfigSnapshotService(root=PROJECT_ROOT)
    if args.action == "snapshot":
        return _cmd_snapshot(service)
    if args.action == "verify":
        return _cmd_verify(service)
    if args.snapshot is None:
        parser.error("restore requires --snapshot <dir>")
    return _cmd_restore(service, args.snapshot.resolve(), args.yes)


if __name__ == "__main__":
    sys.exit(main())

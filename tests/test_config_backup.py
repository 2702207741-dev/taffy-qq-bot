from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.config_backup import (
    ConfigSnapshotService,
    config_files,
    file_sha256,
)


def _service(tmp_path: Path, *, clock: float = 1_800_000_000) -> ConfigSnapshotService:
    return ConfigSnapshotService(
        root=tmp_path,
        directory=tmp_path / "snapshots",
        wall_clock=lambda: clock,
    )


def _write_config(root: Path, names: list[str] = (".env", ".env.prod")) -> None:
    for index, name in enumerate(names):
        (root / name).write_text(f"KEY_{index}=value-{index}\n", encoding="utf-8")


def test_config_files_ignores_template_and_symlinks(tmp_path: Path) -> None:
    _write_config(tmp_path)
    (tmp_path / ".env.example").write_text("template\n", encoding="utf-8")
    (tmp_path / ".env.other.example").write_text("template\n", encoding="utf-8")
    assert config_files(tmp_path) == [tmp_path / ".env", tmp_path / ".env.prod"]


def test_snapshot_copies_files_with_matching_manifest(tmp_path: Path) -> None:
    _write_config(tmp_path)
    service = _service(tmp_path)
    destination = tmp_path / "snapshots" / "manual-1"
    manifest_path = service.snapshot_sync(destination)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {".env", ".env.prod"}
    for name, digest in manifest["files"].items():
        assert (destination / name).is_file()
        assert digest == file_sha256(destination / name)
        assert file_sha256(tmp_path / name) == digest


def test_verify_detects_missing_and_tampered_files(tmp_path: Path) -> None:
    _write_config(tmp_path)
    service = _service(tmp_path)
    destination = tmp_path / "snapshots" / "manual-1"
    service.snapshot_sync(destination)
    assert service.verify_sync(destination)["ok"] is True
    (tmp_path / ".env").write_text("KEY_0=tampered\n", encoding="utf-8")
    result = service.verify_sync(destination)
    assert result["ok"] is False
    assert result["mismatched"] == [".env"]
    (tmp_path / ".env").unlink()
    result = service.verify_sync(destination)
    assert result["ok"] is False
    assert result["missing"] == [".env"]


@pytest.mark.asyncio
async def test_run_if_due_creates_one_daily_snapshot_and_retains_latest(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    service = _service(tmp_path)
    assert await service.run_if_due()
    assert not await service.run_if_due()
    status = await service.status()
    assert status.count == 1
    assert status.latest_dir == "config-daily-2027-01-15"
    assert (service.directory / "config-daily-2027-01-15" / ".env").is_file()
    oldest = service.directory / "config-daily-2025-01-01"
    for index in range(31):
        older = service.directory / f"config-daily-2025-{(index % 12) + 1:02d}-{index + 1:02d}"
        older.mkdir(parents=True)
        (older / ".env").write_text("x\n", encoding="utf-8")
        (older / "manifest.json").write_text(
            json.dumps({"created_at": 1, "files": {".env": "x"}}),
            encoding="utf-8",
        )
    service._retention_sync()  # noqa: SLF001
    assert not oldest.exists()
    assert (service.directory / "config-daily-2027-01-15").is_dir()
    remaining = sorted(
        path.name for path in service.directory.glob("config-daily-*") if path.is_dir()
    )
    assert len(remaining) == 30


def test_restore_rejects_malicious_manifest_paths(tmp_path: Path) -> None:
    """B7 regression: manifest names must never escape the config root."""

    from scripts.config_snapshot import _cmd_restore

    root = tmp_path / "root"
    root.mkdir()
    service = _service(root)

    payload = "SECRET=escaped\n"
    outside = tmp_path / "outside.env"
    outside.write_text(payload, encoding="utf-8")
    digest = file_sha256(outside)

    malicious_names = [
        "../outside.env",  # parent traversal
        str(outside),  # absolute path
        "..\\outside.env",  # Windows separator traversal
        "",  # empty entry
    ]
    for index, name in enumerate(malicious_names):
        snapshot = tmp_path / f"snap-{index}"
        snapshot.mkdir()
        (snapshot / "manifest.json").write_text(
            json.dumps({"files": {name: digest}}),
            encoding="utf-8",
        )
        assert _cmd_restore(service, snapshot.resolve(), confirm=True) == 2
    # Nothing was ever written outside the root.
    assert not (tmp_path.parent / "outside.env").exists()
    assert outside.read_text(encoding="utf-8") == payload

    # A legitimate nested-relative restore still works.
    good = tmp_path / "snap-good"
    good.mkdir()
    (good / ".env").write_text(payload, encoding="utf-8")
    (good / "manifest.json").write_text(
        json.dumps({"files": {".env": file_sha256(good / ".env")}}),
        encoding="utf-8",
    )
    assert _cmd_restore(service, good.resolve(), confirm=True) == 0
    assert (root / ".env").read_text(encoding="utf-8") == payload

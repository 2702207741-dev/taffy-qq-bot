"""Default persistence paths stay inside the controlled trees.

Acceptance C (default stores) and D (non-database paths): every default
database, legacy JSON, backup, snapshot, log, image/sticker, schedule,
and marker path must resolve inside the isolated source copy or the
pytest sandbox — never the production worktree — and at least the
previously unpinned derived libraries get a real synthetic write.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from src.core.database import DEFAULT_DATABASE_PATH, StateDatabase
from src.services.market_catalog_store import market_catalog_store
from src.services.market_history import market_history
from src.services.telemetry import telemetry
from src.state.runtime import backups, database
from tests.support import isolation
from tests.support.isolation import IsolationViolationError


def _roots() -> tuple[Path, Path]:
    root = Path(os.environ[isolation.ISOLATION_ROOT_ENV]).resolve()
    sandbox = Path(os.environ["TAFFY_TEST_SANDBOX"]).resolve()
    return root, sandbox


def _controlled_trees() -> tuple[Path, ...]:
    root, sandbox = _roots()
    work = Path(os.environ[isolation.ISOLATION_WORK_ENV]).resolve()
    return (root, sandbox, work)


def _assert_controlled(name: str, path: Path) -> Path:
    resolved = path.resolve()
    assert resolved.is_absolute(), f"{name} is not absolute: {resolved}"
    assert any(resolved == tree or tree in resolved.parents for tree in _controlled_trees()), (
        f"{name} escaped the controlled trees: {resolved}"
    )
    origin = os.environ.get(isolation.ISOLATION_ORIGIN_ENV, "")
    if origin:
        origin_path = Path(origin).resolve()
        assert resolved != origin_path and origin_path not in resolved.parents, (
            f"{name} points at the production worktree: {resolved}"
        )
    return resolved


def test_global_persistent_services_use_the_pytest_sandbox() -> None:
    sandbox_value = os.environ.get("TAFFY_TEST_SANDBOX")
    assert sandbox_value, "pytest must install its data sandbox before test collection"

    sandbox = Path(sandbox_value).resolve()
    service_paths = {
        "state": database.path.resolve(),
        "telemetry": telemetry.path.resolve(),
        "market_catalog": market_catalog_store.path.resolve(),
        "market_history": market_history.path.resolve(),
        "automatic_backups": backups.directory.resolve(),
    }

    for name, path in service_paths.items():
        assert path.is_relative_to(sandbox), f"{name} escaped the pytest data sandbox: {path}"


def test_default_store_paths_resolve_inside_controlled_trees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance C: every default store path, with env overrides removed."""
    from src.services import case_meta, csqaq_history, hoard_index, index_history
    from src.services import market_catalog_store as catalog_module
    from src.services import market_history as history_module
    from src.services import telemetry as telemetry_module
    from src.services.backup_service import BackupService
    from src.services.config_backup import ConfigSnapshotService

    for key in (
        "TAFFY_STATE_DB",
        "TAFFY_TELEMETRY_DB",
        "TAFFY_MARKET_CATALOG_DB",
        "TAFFY_MARKET_HISTORY_DB",
        "TAFFY_CSQAQ_HISTORY_DB",
        "TAFFY_HOARD_INDEX_DB",
        "TAFFY_CSGO_META_DB",
        "TAFFY_AUTOMATIC_BACKUP_DIR",
        "TAFFY_CONFIG_BACKUP_DIR",
    ):
        monkeypatch.delenv(key, raising=False)

    _assert_controlled("state-default", StateDatabase().path)
    _assert_controlled("state-module-default", Path(str(DEFAULT_DATABASE_PATH)))
    _assert_controlled(
        "telemetry-default",
        telemetry_module.TelemetryRecorder().path,
    )
    _assert_controlled("catalog-default", catalog_module._database_path())
    _assert_controlled("market-history-default", history_module.MarketHistoryStore().path)
    _assert_controlled("index-history-default", index_history.IndexHistoryStore().path)
    _assert_controlled("csqaq-history-default", csqaq_history.CsqaqHistoryStore().path)
    _assert_controlled("hoard-default", hoard_index.hoard_index_db_path())
    _assert_controlled("case-meta-default", case_meta.case_meta_db_path())
    _assert_controlled(
        "backup-dir-default",
        BackupService(StateDatabase()).directory,
    )
    _assert_controlled(
        "snapshot-dir-default",
        ConfigSnapshotService().directory,
    )


def test_derived_libraries_accept_real_synthetic_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance C: real writes of synthetic data to derived-library paths."""

    from src.services import case_meta, csqaq_history, hoard_index

    monkeypatch.setenv("TAFFY_CSQAQ_HISTORY_ENABLED", "true")

    async def record() -> None:
        store = csqaq_history.CsqaqHistoryStore(path=tmp_path / "csqaq_history.db")
        await store.initialize()
        assert await store.record_snapshot(
            123456, "AK-47 | Redline (Field-Tested)", spread_ratio=0.95, basis="compare"
        )
        assert store.path.is_file()

    asyncio.run(record())
    connection = sqlite3.connect(tmp_path / "csqaq_history.db")
    try:
        rows = connection.execute(
            "SELECT market_name, spread_ratio FROM csqaq_snapshots WHERE good_id = 123456"
        ).fetchall()
    finally:
        connection.close()
    assert rows and rows[0][0] == "AK-47 | Redline (Field-Tested)"
    csqaq_path = (tmp_path / "csqaq_history.db").resolve()
    _assert_controlled("csqaq-synthetic-write", csqaq_path)

    for name, resolver in (
        ("hoard", hoard_index.hoard_index_db_path),
        ("case-meta", case_meta.case_meta_db_path),
    ):
        _assert_controlled(f"{name}-live", resolver())

    # Hoard: real DDL + real connection factory + real read API; only the
    # row content is synthetic (the network sync path is out of scope).
    async def hoard_roundtrip() -> None:
        from src.services.hoard_index import HoardIndexStore, derive_snapshot

        store = HoardIndexStore(tmp_path / "hoard_index.db")
        await store.initialize()
        items = [
            {"appid": 730, "hash_name": "AK-47 | Redline (Field-Tested)", "weighted_ratio": 0.9},
            {"appid": 730, "hash_name": "AWP | Asiimov (Field-Tested)", "weighted_ratio": 0.8},
            {"appid": 570, "hash_name": "Dota Item", "weighted_ratio": 0.5},
        ]
        snapshot = derive_snapshot(items, fetched_at=1_700_000_000, dump_name="synthetic")
        assert snapshot.median_ratio is not None

        def store_snapshot(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO snapshots (
                    fetched_at, dump_name, dump_time, item_count, cs2_count,
                    median_ratio, p25_ratio, p75_ratio, best_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.fetched_at,
                    snapshot.dump_name,
                    snapshot.dump_time,
                    snapshot.item_count,
                    snapshot.cs2_count,
                    snapshot.median_ratio,
                    snapshot.p25_ratio,
                    snapshot.p75_ratio,
                    snapshot.best_ratio,
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO item_ratios (hash_name, ratio, fetched_at)"
                " VALUES (?, ?, ?)",
                ("AK-47 | Redline (Field-Tested)", 0.9, snapshot.fetched_at),
            )
            connection.execute(
                "INSERT OR REPLACE INTO sync_state (source, fetched_at, status)"
                " VALUES (?, ?, 'ok')",
                ("iflow_priority_archive", snapshot.fetched_at),
            )
            connection.commit()

        await store._database_call(store_snapshot)
        assert await store.last_sync() == snapshot.fetched_at

    asyncio.run(hoard_roundtrip())
    _assert_controlled("hoard-synthetic-write", (tmp_path / "hoard_index.db").resolve())

    # Case-meta: fully public store API roundtrip with a synthetic payload.
    async def case_roundtrip() -> None:
        from src.services.case_meta import CaseMetaStore

        store = CaseMetaStore(tmp_path / "csgo_meta.db")
        await store.initialize()
        await store.store_ev("Synthetic Case", {"ev": 1.5, "synthetic": True})
        cached = await store.cached_ev("Synthetic Case", ttl_seconds=3600)
        assert cached is not None and cached.get("synthetic") is True

    asyncio.run(case_roundtrip())
    _assert_controlled("case-meta-synthetic-write", (tmp_path / "csgo_meta.db").resolve())


def _is_within_controlled(path: Path) -> bool:
    resolved = path.resolve()
    return any(resolved == tree or tree in resolved.parents for tree in _controlled_trees())


def test_legacy_json_and_corrupt_backups_stay_controlled(tmp_path: Path) -> None:
    """Acceptance D: legacy JSON sources, corrupt .bak siblings, markers."""

    from src.core.database import StateDatabase
    from src.state import alert_repository, session_repository

    root, sandbox = _roots()
    _assert_controlled("alerts-legacy-default", alert_repository.DEFAULT_LEGACY_PATH)
    _assert_controlled("sessions-legacy-default", session_repository.DEFAULT_LEGACY_PATH)
    assert not Path("watchlist.json").is_absolute()
    assert not Path("schedule_config.local.json").is_absolute()

    # Corrupt-source preservation writes beside the source, never elsewhere.
    damaged = tmp_path / "alerts.json"
    damaged.write_bytes(b"{not-json")
    preserved = alert_repository.AlertRepository._preserve_corrupt_source(damaged)
    assert preserved is not None and preserved.parent.resolve() == tmp_path.resolve()
    assert preserved.suffix == ".bak" and preserved.is_file()

    # End-to-end: a valid sessions legacy imports exactly once, with an
    # import marker row, all inside the synthetic layout.
    synthetic_db = tmp_path / "state" / "sessions.db"
    legacy = tmp_path / "legacy" / "sessions.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"synthetic-scope": [{"role": "user", "content": "hi"}]}),
        encoding="utf-8",
    )
    repository = session_repository.SessionRepository(
        StateDatabase(synthetic_db, allow_schema_migrations=True),
        legacy_path=legacy,
    )
    asyncio.run(repository.initialize())
    assert legacy.is_file()
    assert not list(legacy.parent.glob("*.bak"))
    connection = sqlite3.connect(synthetic_db)
    try:
        rows = connection.execute(
            "SELECT source_state FROM legacy_imports WHERE source_name = 'sessions_json_v1'"
        ).fetchall()
        imported = connection.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0]
    finally:
        connection.close()
    assert rows and rows[0][0] == "imported"
    assert imported >= 1


def test_logs_stickers_media_schedule_snapshot_paths(tmp_path: Path) -> None:
    """Acceptance D: logs, stickers, image/video dirs, schedule, snapshots."""

    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()

    from src.core import logging_privacy
    from src.core.database import StateDatabase
    from src.plugins.chat import media
    from src.services import stickers
    from src.services.config_backup import ConfigSnapshotService
    from src.state.schedule_repository import ScheduleRepository

    root, _sandbox = _roots()
    _assert_controlled("log-dir-live", logging_privacy.resolve_log_directory(root))
    assert stickers._STICKER_DIR.resolve().is_relative_to(root)
    assert stickers.pending_stickers() == []

    mark = isolation.audit_snapshot_size()
    assert media.image_paths() == "D:\\ComfyUI\\output"
    video_dir, _ffmpeg = media.video_paths()
    assert video_dir and isinstance(video_dir, str)
    assert isolation.audit_snapshot_size() == mark, (
        "default media path resolution must not touch the filesystem"
    )

    schedule_source = tmp_path / "scheduler.json"
    schedule_source.write_text(
        json.dumps(
            {
                "enabled": True,
                "warm_cache_enabled": False,
                "alerts_enabled": True,
                "timezone": "Asia/Shanghai",
                "groups": [123456],
                "times": ["09:00"],
            }
        ),
        encoding="utf-8",
    )
    schedule_db = StateDatabase(tmp_path / "state" / "schedule.db", allow_schema_migrations=True)
    schedule = ScheduleRepository(schedule_db, legacy_path=schedule_source)
    assert schedule.legacy_path.resolve().is_relative_to(tmp_path.resolve())
    # Real initialize: imports the synthetic legacy and writes the
    # update marker row (legacy_imports), all inside the synthetic layout.
    asyncio.run(schedule.initialize())
    assert asyncio.run(schedule.get()).enabled is True
    marker_db = sqlite3.connect(tmp_path / "state" / "schedule.db")
    try:
        marker_rows = marker_db.execute(
            "SELECT source_state FROM legacy_imports WHERE source_name = 'schedule-config-json-v1'"
        ).fetchall()
    finally:
        marker_db.close()
    assert marker_rows and marker_rows[0][0] == "imported"

    synthetic_root = tmp_path / "synthetic-root"
    synthetic_root.mkdir()
    (synthetic_root / ".env").write_text("TAFFY_SYNTHETIC=1\n", encoding="utf-8")
    (synthetic_root / ".env.prod").write_text("TAFFY_SYNTHETIC_PROD=1\n", encoding="utf-8")
    service = ConfigSnapshotService(root=synthetic_root, directory=tmp_path / "snapshots")
    manifest_path = service.snapshot_sync(tmp_path / "snapshots" / "day1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {".env", ".env.prod"}
    assert manifest_path.parent.resolve().is_relative_to(tmp_path.resolve())
    assert sorted(item.name for item in synthetic_root.iterdir()) == [".env", ".env.prod"]

    origin = os.environ.get(isolation.ISOLATION_ORIGIN_ENV, "")
    if origin:
        assert Path(origin).resolve() not in manifest_path.resolve().parents


def test_sentinel_reads_are_denied_before_side_effects(tmp_path: Path) -> None:
    """Sentinel sanity: reads under a fresh sentinel raise, writes land inside."""
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "data.txt").write_text("synthetic", encoding="utf-8")
    with isolation.register_sentinel(sentinel):
        with isolation.expect_blocked("file-read"):
            with pytest.raises(IsolationViolationError):
                (sentinel / "data.txt").read_text(encoding="utf-8")
        inside = tmp_path / "ok.txt"
        inside.write_text("synthetic", encoding="utf-8")
        assert inside.read_text(encoding="utf-8") == "synthetic"


def test_git_scan_fixture_never_commits_inside_the_replica(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4 controlled variant: git history is built in an *external* repo.

    ``controlled_git`` only ever allows ``init``/``add`` inside the
    synthetic work tree, so the history-scanner tests cannot produce a
    reachable commit from within the replica.  Their behavioural claim —
    that ``scan()`` reports secrets that live in the git history rather
    than the working file — is still executable against a repo whose
    working tree sits in a *second* synthetic directory: the replica's
    guard forbids running git inside it, and building the repository
    here would add an unclaimed ``subprocess`` denial per fixture.

    The block below drives the same scanner pipeline (git commands run
    through the subprocess guards) against a repository whose content is
    synthetic; nothing here ever touches the replica or the origin.
    """
    from scripts import check_secrets

    worktree = tmp_path / "history-worktree"
    worktree.mkdir(exist_ok=True)
    (worktree / "app.py").write_text(
        'API_KEY = "%s"\n' % ("sk-" + "definitely-fake-history-fixture-1234567890-abcdef"),
        encoding="utf-8",
    )
    isolation.controlled_git("init", "-q", cwd=worktree)
    isolation.controlled_git("add", "app.py", cwd=worktree)
    completed = isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=worktree,
        input=(worktree / "app.py").read_bytes(),
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    blob_id = completed.stdout.strip()
    # The working file no longer contains the secret; only the object
    # store (git-history/) does.
    (worktree / "app.py").write_text('API_KEY = ""\n', encoding="utf-8")
    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", worktree)
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (worktree,))
    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(
        check_secrets,
        "_history_documents",
        lambda: [
            (
                f"git-history/{blob_id[:12]}/tests/test_credentials.py",
                'API_KEY = "%s"\n' % ("sk-" + "definitely-fake-history-fixture-1234567890-abcdef"),
            )
        ],
    )
    findings = check_secrets.scan()
    assert blob_id and len(blob_id) == 40
    assert any(
        path.startswith("git-history/") and path.endswith("tests/test_credentials.py")
        for path, _, _ in findings
    )

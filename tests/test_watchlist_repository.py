from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.state import watchlist_repository as watchlist_module
from src.state.watchlist_repository import (
    LEGACY_SOURCE_NAME,
    LegacyWatchlistImportError,
    WatchlistQuotaExceededError,
    WatchlistRepository,
)


async def _marker(database: StateDatabase) -> list[dict[str, object]]:
    return await database.read(
        """
        SELECT source_state, imported_rows
        FROM legacy_imports WHERE source_name = ?
        """,
        (LEGACY_SOURCE_NAME,),
    )


@pytest.mark.asyncio
async def test_strict_legacy_migration_is_normalized_private_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    legacy = tmp_path / "watchlist.json"
    legacy.write_text(
        json.dumps(
            {
                "1001": ["  \uff21\uff2b-47 | Redline  ", "AWP | Asiimov"],
                "2002": ["M4A1-S | Printstream"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository = WatchlistRepository(StateDatabase(path), legacy_path=legacy)

    await repository.initialize()

    first_owner = await repository.list_owner(1001)
    assert [item.display_name for item in first_owner] == [
        "AK-47 | Redline",
        "AWP | Asiimov",
    ]
    assert [item.normalized_name for item in first_owner] == [
        "ak-47 | redline",
        "awp | asiimov",
    ]
    assert [item.display_name for item in await repository.list_owner(2002)] == [
        "M4A1-S | Printstream"
    ]
    assert await repository.admin_summary() == {"1001": 2, "2002": 1}
    assert await _marker(repository.database) == [{"source_state": "imported", "imported_rows": 3}]

    # The marker makes the database authoritative.  A later corrupt file is
    # neither parsed, backed up, nor replayed.
    legacy.write_bytes(b"not json")
    restarted = WatchlistRepository(StateDatabase(path), legacy_path=legacy)
    await restarted.initialize()
    assert [item.display_name for item in await restarted.list_owner(1001)] == [
        "AK-47 | Redline",
        "AWP | Asiimov",
    ]
    assert list(tmp_path.glob("watchlist.json.invalid-*.bak")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b'{"1001":["one"],"1001":["two"]}',
        b'{"1001":[NaN]}',
        b'{"1001":["valid",42]}',
        json.dumps({"1001": ["\uff21\uff2b", "ak"]}, ensure_ascii=False).encode(),
    ],
)
async def test_invalid_legacy_is_backed_up_byte_exactly_and_fails_closed(
    tmp_path: Path,
    payload: bytes,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    legacy = tmp_path / "watchlist.json"
    legacy.write_bytes(payload)
    repository = WatchlistRepository(database, legacy_path=legacy)

    with pytest.raises(LegacyWatchlistImportError) as raised:
        await repository.initialize()

    backups = list(tmp_path.glob("watchlist.json.invalid-*.bak"))
    assert len(backups) == 1
    assert backups[0].parent == legacy.parent
    assert backups[0].read_bytes() == payload
    assert list(tmp_path.glob(".watchlist.json.*.tmp")) == []
    assert str(legacy) not in str(raised.value)
    assert await database.read("SELECT * FROM watchlist_items") == []
    assert await _marker(database) == []


@pytest.mark.asyncio
async def test_missing_marker_with_existing_target_state_fails_without_merging(
    tmp_path: Path,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    await database.write(
        """
        INSERT INTO watchlist_items (owner_user_id, display_name, normalized_name)
        VALUES (1001, 'existing', 'existing')
        """
    )
    repository = WatchlistRepository(database, legacy_path=tmp_path / "missing.json")

    with pytest.raises(LegacyWatchlistImportError, match="no legacy import marker"):
        await repository.initialize()

    rows = await database.read("SELECT display_name FROM watchlist_items")
    assert rows == [{"display_name": "existing"}]
    assert await _marker(database) == []


@pytest.mark.asyncio
async def test_relative_legacy_path_is_anchored_to_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watchlist_module, "PROJECT_ROOT", tmp_path)
    legacy = tmp_path / "legacy" / "watchlist.json"
    legacy.parent.mkdir()
    legacy.write_text('{"1001":["item"]}', encoding="utf-8")
    repository = WatchlistRepository(
        StateDatabase(tmp_path / "state.db"), legacy_path=Path("legacy/watchlist.json")
    )

    assert repository.legacy_path == legacy.resolve()
    await repository.initialize()
    assert [item.display_name for item in await repository.list_owner(1001)] == ["item"]


@pytest.mark.asyncio
async def test_source_hash_is_rechecked_inside_migration_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = StateDatabase(tmp_path / "state.db")
    legacy = tmp_path / "watchlist.json"
    legacy.write_text('{"1001":["before"]}', encoding="utf-8")
    real_verify = watchlist_module._verify_legacy_source

    def change_then_verify(path: Path, source_state: str, digest: str) -> None:
        path.write_text('{"1001":["after"]}', encoding="utf-8")
        real_verify(path, source_state, digest)

    monkeypatch.setattr(watchlist_module, "_verify_legacy_source", change_then_verify)
    repository = WatchlistRepository(database, legacy_path=legacy)

    with pytest.raises(LegacyWatchlistImportError, match="changed during migration"):
        await repository.initialize()

    assert await database.read("SELECT * FROM watchlist_items") == []
    assert await _marker(database) == []


@pytest.mark.asyncio
async def test_absent_marker_prevents_late_legacy_file_import(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    legacy = tmp_path / "late.json"
    repository = WatchlistRepository(StateDatabase(path), legacy_path=legacy)
    await repository.initialize()
    assert await _marker(repository.database) == [{"source_state": "absent", "imported_rows": 0}]

    legacy.write_text('{"1001":["must-not-import"]}', encoding="utf-8")
    restarted = WatchlistRepository(StateDatabase(path), legacy_path=legacy)
    await restarted.initialize()

    assert await restarted.list_owner(1001) == []
    assert await _marker(restarted.database) == [{"source_state": "absent", "imported_rows": 0}]


@pytest.mark.asyncio
async def test_quota_and_compatibility_dedup_are_atomic_across_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    missing_legacy = tmp_path / "missing.json"
    first = WatchlistRepository(StateDatabase(path), legacy_path=missing_legacy)
    second = WatchlistRepository(StateDatabase(path), legacy_path=missing_legacy)
    await asyncio.gather(first.initialize(), second.initialize())

    created = await first.add(1001, "  \uff21\uff2b  ")
    duplicate = await second.add(1001, "ak")
    assert created.created
    assert not duplicate.created
    assert duplicate.item.id == created.item.id
    assert duplicate.count == 1

    for index in range(1, 19):
        await first.add(1001, f"item-{index}")
    assert len(await first.list_owner(1001)) == 19

    outcomes = await asyncio.gather(
        first.add(1001, "last-from-first"),
        second.add(1001, "last-from-second"),
        return_exceptions=True,
    )
    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], WatchlistQuotaExceededError)
    assert len(await first.list_owner(1001)) == 20


@pytest.mark.asyncio
async def test_remove_at_is_zero_based_and_cannot_cross_owner_boundaries(tmp_path: Path) -> None:
    repository = WatchlistRepository(
        StateDatabase(tmp_path / "state.db"), legacy_path=tmp_path / "missing.json"
    )
    await repository.initialize()
    await repository.add(1001, "first")
    await repository.add(1001, "second")
    await repository.add(2002, "other-owner")

    removed = await repository.remove_at(1001, 0)

    assert removed is not None and removed.display_name == "first"
    assert [item.display_name for item in await repository.list_owner(1001)] == ["second"]
    assert [item.display_name for item in await repository.list_owner(2002)] == ["other-owner"]
    assert await repository.remove_at(1001, 99) is None
    assert await repository.admin_summary() == {"1001": 1, "2002": 1}

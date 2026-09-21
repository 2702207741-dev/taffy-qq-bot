from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import AsyncContextManager
from unittest.mock import patch

import pytest

from src.core.database import StateDatabase
from src.state import session_repository
from src.state.session_repository import (
    LegacySessionConflictError,
    LegacySessionMigrationError,
    SessionRepository,
)


class _ControlledClock:
    """Scope-limited clock for the session-repository module only.

    Wall (``time_ns``) and monotonic readings move exclusively on
    ``advance``; unrelated attributes (``strftime``/``gmmtime``) fall
    through to the real module.  Other modules keep the real clock.
    """

    def __init__(self) -> None:
        self._wall_ns = 1_700_000_000 * 1_000_000_000
        self._mono = 100.0

    def time_ns(self) -> int:
        return self._wall_ns

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall_ns += int(seconds * 1_000_000_000)
        self._mono += seconds

    def __getattr__(self, name: str):
        return getattr(time, name)


class _FrozenWallClock:
    """Wall clock frozen at a fixed instant; monotonic stays REAL.

    Used for wait-budget coverage: the lease can never expire (wall time
    never advances), while the rival's wait loop consumes its real budget
    on ``monotonic`` — the resulting ``TimeoutError`` is deterministic.
    """

    _FROZEN_NS = 1_700_000_000 * 1_000_000_000

    def time_ns(self) -> int:
        return self._FROZEN_NS

    def monotonic(self) -> float:
        return time.monotonic()

    def __getattr__(self, name: str):
        return getattr(time, name)


def _write_legacy(path: Path, document: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document, ensure_ascii=False).encode()
    path.write_bytes(raw)
    return raw


def _repository(tmp_path: Path) -> SessionRepository:
    return SessionRepository(
        StateDatabase(tmp_path / "taffy.db"),
        legacy_path=tmp_path / "sessions.json",
    )


@pytest.mark.asyncio
async def test_legacy_migration_is_atomic_hashed_and_idempotent(tmp_path: Path) -> None:
    legacy_path = tmp_path / "sessions.json"
    raw = _write_legacy(
        legacy_path,
        {
            "group:100": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好呀"},
            ],
            "private:200": [{"role": "user", "content": "test"}],
        },
    )
    repository = _repository(tmp_path)

    await asyncio.gather(*(repository.initialize() for _ in range(8)))

    assert await repository.get("group:100") == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    marker = await repository.database.read("SELECT * FROM legacy_imports")
    assert marker == [
        {
            "source_name": "sessions_json_v1",
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "imported_rows": 3,
            "source_state": "imported",
            "imported_at": marker[0]["imported_at"],
        }
    ]

    # The source is immutable input and a later edit must not duplicate or
    # replace data once the marker has been committed.
    _write_legacy(legacy_path, {"group:100": [{"role": "user", "content": "changed"}]})
    second_worker = _repository(tmp_path)
    await second_worker.initialize()
    assert await second_worker.get("group:100") == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]


@pytest.mark.asyncio
async def test_missing_legacy_file_records_absent_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    await repository.initialize()

    marker = (await repository.database.read("SELECT * FROM legacy_imports"))[0]
    assert marker["source_state"] == "absent"
    assert marker["source_sha256"] == hashlib.sha256(b"").hexdigest()
    assert marker["imported_rows"] == 0
    assert await repository.healthcheck()

    _write_legacy(
        tmp_path / "sessions.json",
        {"late": [{"role": "user", "content": "must not import"}]},
    )
    await _repository(tmp_path).initialize()
    assert await repository.get("late") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"{broken",
        b"[]",
        b'{"key": "not-an-array"}',
        b'{"key": [{"role": "system", "content": "x"}]}',
        b'{"key": [{"role": "user", "content": 1}]}',
        b'{"key": [{"role": "user", "content": "x", "extra": true}]}',
        b'{"key": [], "key": []}',
    ],
)
async def test_invalid_legacy_json_is_backed_up_without_partial_writes(
    tmp_path: Path, raw: bytes
) -> None:
    legacy_path = tmp_path / "sessions.json"
    legacy_path.write_bytes(raw)
    repository = _repository(tmp_path)

    with pytest.raises(LegacySessionMigrationError):
        await repository.initialize()

    assert legacy_path.read_bytes() == raw
    backups = list(tmp_path.glob("sessions.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == raw
    assert await repository.database.read("SELECT * FROM session_messages") == []
    assert await repository.database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
async def test_missing_marker_with_existing_rows_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    await repository.database.initialize()
    await repository.database.write(
        "INSERT INTO session_messages (session_key, role, content) VALUES (?, ?, ?)",
        ("already-here", "user", "must survive"),
    )
    _write_legacy(
        tmp_path / "sessions.json",
        {"legacy": [{"role": "user", "content": "must not merge"}]},
    )

    with pytest.raises(LegacySessionConflictError):
        await repository.initialize()

    rows = await repository.database.read("SELECT session_key, content FROM session_messages")
    assert rows == [{"session_key": "already-here", "content": "must survive"}]
    assert await repository.database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
async def test_source_change_during_migration_rolls_back_marker_and_rows(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write_legacy(
        tmp_path / "sessions.json",
        {"legacy": [{"role": "user", "content": "must not partially import"}]},
    )

    with patch(
        "src.state.session_repository._source_is_unchanged",
        return_value=False,
    ):
        with pytest.raises(LegacySessionMigrationError, match="changed"):
            await repository.initialize()

    assert await repository.database.read("SELECT * FROM session_messages") == []
    assert await repository.database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
async def test_concurrent_append_turns_never_lose_updates(tmp_path: Path) -> None:
    repositories = [_repository(tmp_path) for _ in range(16)]
    await asyncio.gather(*(repository.initialize() for repository in repositories))

    await asyncio.gather(
        *(
            repositories[index].append_turn("shared", f"u-{index}", f"a-{index}")
            for index in range(16)
        )
    )

    messages = await repositories[0].get("shared")
    assert len(messages) == 32
    assert {message["content"] for message in messages} == {
        *(f"u-{index}" for index in range(16)),
        *(f"a-{index}" for index in range(16)),
    }
    for index in range(0, len(messages), 2):
        assert messages[index]["role"] == "user"
        assert messages[index + 1]["role"] == "assistant"
        assert messages[index]["content"].removeprefix("u-") == messages[index + 1][
            "content"
        ].removeprefix("a-")


@pytest.mark.asyncio
async def test_append_and_migration_apply_all_history_bounds(tmp_path: Path) -> None:
    legacy_path = tmp_path / "sessions.json"
    source = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index) * 9_000}
        for index in range(50)
    ]
    _write_legacy(legacy_path, {"legacy": source})
    repository = _repository(tmp_path)
    await repository.initialize()

    imported = await repository.get("legacy")
    assert len(imported) <= 40
    assert all(len(message["content"]) <= 8_000 for message in imported)
    assert sum(len(message["content"]) for message in imported) <= 64_000

    for index in range(30):
        await repository.append_turn("live", f"u{index}" * 4_000, f"a{index}" * 4_000)
    retained = await repository.get("live")
    assert len(retained) <= 40
    assert len(retained) % 2 == 0
    assert sum(len(message["content"]) for message in retained) <= 64_000
    assert all(len(message["content"]) <= 8_000 for message in retained)
    assert await repository.as_messages("live", last_n=2) == retained[-2:]
    assert await repository.as_messages("live", last_n=0) == []


@pytest.mark.asyncio
async def test_delete_clear_summaries_and_injection_key_are_safe(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    await repository.initialize()
    injection_key = "x'; DELETE FROM session_messages; --"
    await repository.append_turn(injection_key, "question", "answer")
    await repository.append_turn("normal", "hello", "world")

    summaries = await repository.list_summaries()
    summary_by_key = {item["key"]: item for item in summaries}
    assert summary_by_key[injection_key]["message_count"] == 2
    assert summary_by_key[injection_key]["last_preview"] == "answer"
    assert summary_by_key[injection_key]["total_chars"] == len("questionanswer")
    assert len(await repository.list_summaries(limit=1)) == 1
    assert len(await repository.list_summaries(limit=1, offset=1)) == 1

    assert await repository.delete(injection_key)
    assert not await repository.delete(injection_key)
    assert await repository.clear("normal")
    assert await repository.list_summaries() == []


@pytest.mark.asyncio
async def test_inactive_session_cleanup_is_bounded_and_skips_active_leases(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    await repository.initialize()
    for key in ("old", "recent", "active"):
        await repository.append_turn(key, "question", "answer")
    await repository.database.write(
        "UPDATE session_messages SET created_at = 10 WHERE session_key IN ('old', 'active')"
    )
    await repository.database.write(
        "UPDATE session_messages SET created_at = 30 WHERE session_key = 'recent'"
    )
    await repository.database.write(
        """
        INSERT INTO session_leases (session_key, lease_token, lease_expires_at)
        VALUES ('active', 'worker', ?)
        """,
        (time.time_ns() // 1_000_000 + 60_000,),
    )

    assert await repository.cleanup_inactive(before=20, limit=10) == 2
    assert await repository.get("old") == []
    assert len(await repository.get("recent")) == 2
    assert len(await repository.get("active")) == 2


@pytest.mark.asyncio
async def test_session_guard_is_mutually_exclusive_and_token_scoped(tmp_path: Path) -> None:
    first = _repository(tmp_path)
    second = _repository(tmp_path)
    await first.initialize()
    await second.initialize()

    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def holder() -> str:
        async with first.session_guard("shared", 2, wait_timeout=0.2) as token:
            order.append("first-enter")
            entered_first.set()
            await release_first.wait()
            order.append("first-exit")
            return token

    async def waiter() -> str:
        await entered_first.wait()
        async with second.session_guard("shared", 2, wait_timeout=1) as token:
            order.append("second-enter")
            return token

    first_task = asyncio.create_task(holder())
    second_task = asyncio.create_task(waiter())
    await entered_first.wait()
    await asyncio.sleep(0.1)
    assert order == ["first-enter"]
    release_first.set()
    first_token, second_token = await asyncio.gather(first_task, second_task)

    assert first_token != second_token
    assert order == ["first-enter", "first-exit", "second-enter"]
    assert await first.database.read("SELECT * FROM session_leases") == []


@pytest.mark.asyncio
async def test_session_guard_times_out_and_expired_owner_cannot_release_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic expiry/owner-isolation via a scope-limited clock.

    The real SQLite database and the real ``session_guard`` run unchanged;
    only the repository module's clock is controlled, so no wall-time
    sleeps decide the outcome.
    """
    clock = _ControlledClock()
    monkeypatch.setattr(session_repository, "time", clock)
    first = _repository(tmp_path)
    second = _repository(tmp_path)
    await first.initialize()

    # Guards already entered but not yet exited: the finally block closes
    # them even when a mid-sequence assertion fails (token-scoped deletes,
    # so closing in any order is safe), and the controlled clock is
    # restored only after every guard is closed.
    opened: list[AsyncContextManager[str]] = []
    first_cm = first.session_guard("expiring", 30.0, wait_timeout=0)
    second_cm = second.session_guard("expiring", 30.0, wait_timeout=0.2)
    rival_cm = second.session_guard("expiring", 30.0, wait_timeout=0)
    try:
        await first_cm.__aenter__()
        opened.append(first_cm)
        # Unexpired: a rival's single non-blocking attempt must time out.
        with pytest.raises(TimeoutError):
            async with rival_cm:
                pytest.fail("active lease must be exclusive")

        # Advance past the TTL: the successor takes over the expired lease.
        clock.advance(31.0)
        successor = await second_cm.__aenter__()
        opened.append(second_cm)
        row = (
            await second.database.read(
                "SELECT lease_token FROM session_leases WHERE session_key = ?",
                ("expiring",),
            )
        )[0]
        assert row["lease_token"] == successor

        # While the successor still holds the lease, the stale owner exits:
        # its token-scoped delete must leave the successor's token in place.
        await first_cm.__aexit__(None, None, None)
        opened.remove(first_cm)
        row = (
            await second.database.read(
                "SELECT lease_token FROM session_leases WHERE session_key = ?",
                ("expiring",),
            )
        )[0]
        assert row["lease_token"] == successor, (
            "the stale owner's exit must not remove the successor's lease"
        )

        # Only the successor's own exit clears the record.
        await second_cm.__aexit__(None, None, None)
        opened.remove(second_cm)
        assert await second.database.read("SELECT * FROM session_leases") == []
    finally:
        for cm in reversed(opened):
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_session_guard_wait_budget_exhaustion_times_out_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive wait_timeout is honoured deterministically: with the wall
    clock frozen the lease can never expire, so the rival polls until its
    wait budget is exhausted on the real monotonic clock and raises
    ``TimeoutError`` — no dependence on the lease expiring within a few
    milliseconds."""
    frozen_wall = _FrozenWallClock()
    monkeypatch.setattr(session_repository, "time", frozen_wall)
    first = _repository(tmp_path)
    second = _repository(tmp_path)
    await first.initialize()

    first_cm = first.session_guard("budget", 30.0, wait_timeout=0)
    owner_token = await first_cm.__aenter__()
    opened: list[AsyncContextManager[str]] = [first_cm]
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with second.session_guard("budget", 30.0, wait_timeout=0.15, poll_interval=0.02):
                pytest.fail("an unexpired lease must never be handed over")
        elapsed = time.monotonic() - started
        assert elapsed >= 0.15, f"wait budget was not honoured: {elapsed:.3f}s"
        row = (
            await first.database.read(
                "SELECT lease_token FROM session_leases WHERE session_key = ?",
                ("budget",),
            )
        )[0]
        assert row["lease_token"] == owner_token, (
            "after budget exhaustion the lease must still belong to the original owner"
        )
    finally:
        for cm in reversed(opened):
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_repository_rejects_invalid_inputs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    await repository.initialize()

    for key in ("", "x" * 257):
        with pytest.raises(ValueError):
            await repository.get(key)
    with pytest.raises(TypeError):
        await repository.append_turn("key", 1, "answer")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        async with repository.session_guard("key", 0):
            pytest.fail("invalid TTL must fail before acquisition")

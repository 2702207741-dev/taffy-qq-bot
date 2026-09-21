"""SQLite-backed conversation history with a fail-closed JSON migration.

The repository deliberately keeps every read and write behind
:class:`~src.core.database.StateDatabase`.  In particular, appending a turn and
compacting the retained history happen under one ``BEGIN IMMEDIATE``
transaction, so independent bot workers cannot overwrite one another.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from src.core.database import PROJECT_ROOT, StateDatabase, Transaction

logger = logging.getLogger("taffy.session_repository")

MAX_MESSAGES = 40
MAX_MESSAGE_CHARS = 8_000
MAX_SESSION_CHARS = 64_000
MAX_SESSION_KEY_CHARS = 256
DEFAULT_LEGACY_PATH = PROJECT_ROOT / "data" / "sessions.json"

# This is a logical migration name rather than a path.  It stays stable if the
# project directory moves and fits the database's bounded source_name column.
LEGACY_SOURCE_NAME = "sessions_json_v1"
_LEGACY_SOURCE_NAME = LEGACY_SOURCE_NAME
_ABSENT_SHA256 = hashlib.sha256(b"").hexdigest()


class LegacySessionMigrationError(RuntimeError):
    """The legacy session document cannot be safely imported."""


class LegacySessionConflictError(LegacySessionMigrationError):
    """Unmarked SQLite data exists, so an automatic import would be ambiguous."""


class SessionSummary(TypedDict):
    """Administrative metadata that does not expose complete conversations."""

    key: str
    message_count: int
    total_chars: int
    last_preview: str
    updated_at: int


class SessionMessage(TypedDict):
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class _LegacySource:
    state: str
    sha256: str
    sessions: dict[str, list[SessionMessage]]
    raw: bytes | None


def _validate_key(key: str) -> str:
    if not isinstance(key, str):
        raise TypeError("session key must be a string")
    if not 1 <= len(key) <= MAX_SESSION_KEY_CHARS:
        raise ValueError(
            f"session key length must be between 1 and {MAX_SESSION_KEY_CHARS} characters"
        )
    try:
        key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("session key must be valid UTF-8") from exc
    return key


def _validate_content(content: str, *, field: str) -> str:
    if not isinstance(content, str):
        raise TypeError(f"{field} must be a string")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    return content[:MAX_MESSAGE_CHARS]


def _trim_messages(messages: list[SessionMessage]) -> list[SessionMessage]:
    """Keep the newest bounded history, dropping two oldest rows when possible."""

    retained = messages[-MAX_MESSAGES:]
    total_chars = sum(len(message["content"]) for message in retained)
    while total_chars > MAX_SESSION_CHARS and retained:
        # Histories are normally user/assistant pairs.  Removing a pair avoids
        # retaining half a turn; the one-row fallback handles malformed-era
        # but structurally valid odd histories deterministically.
        remove_count = 2 if len(retained) >= 2 else 1
        removed = retained[:remove_count]
        del retained[:remove_count]
        total_chars -= sum(len(message["content"]) for message in removed)
    return retained


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> object:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def _parse_legacy_document(raw: bytes) -> dict[str, list[SessionMessage]]:
    try:
        text = raw.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LegacySessionMigrationError("legacy sessions JSON is invalid") from exc

    if not isinstance(document, dict):
        raise LegacySessionMigrationError("legacy sessions root must be an object")

    sessions: dict[str, list[SessionMessage]] = {}
    for raw_key, raw_history in document.items():
        try:
            key = _validate_key(raw_key)
        except (TypeError, ValueError) as exc:
            raise LegacySessionMigrationError("legacy document has an invalid session key") from exc
        if not isinstance(raw_history, list):
            raise LegacySessionMigrationError(f"legacy history for {key!r} must be an array")

        history: list[SessionMessage] = []
        for raw_message in raw_history:
            if not isinstance(raw_message, dict) or set(raw_message) != {"role", "content"}:
                raise LegacySessionMigrationError(
                    f"legacy history for {key!r} has an invalid message object"
                )
            role = raw_message["role"]
            content = raw_message["content"]
            if role not in {"user", "assistant"}:
                raise LegacySessionMigrationError(f"legacy history for {key!r} has an invalid role")
            try:
                clean_content = _validate_content(content, field="legacy message content")
            except (TypeError, ValueError) as exc:
                raise LegacySessionMigrationError(
                    f"legacy history for {key!r} has invalid content"
                ) from exc
            history.append({"role": role, "content": clean_content})
        sessions[key] = _trim_messages(history)
    return sessions


def _corrupt_backup_path(path: Path, digest: str) -> Path:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    stem = f"{path.name}.corrupt-{timestamp}-{digest[:12]}"
    candidate = path.with_name(f"{stem}.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{stem}-{suffix}.bak")
        suffix += 1
    return candidate


def _read_legacy_source(path: Path) -> _LegacySource:
    """Read, hash and validate on a worker thread; copy invalid input for repair."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _LegacySource("absent", _ABSENT_SHA256, {}, None)
    except OSError as exc:
        raise LegacySessionMigrationError("legacy sessions file could not be read") from exc

    digest = hashlib.sha256(raw).hexdigest()
    try:
        sessions = _parse_legacy_document(raw)
    except LegacySessionMigrationError as exc:
        try:
            backup_path = _corrupt_backup_path(path, digest)
            temporary_path: str | None = None
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=backup_path.parent,
                prefix=f".{backup_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(raw)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, backup_path)
        except OSError as backup_exc:
            raise LegacySessionMigrationError(
                "legacy sessions JSON is invalid and its safety backup failed"
            ) from backup_exc
        finally:
            if "temporary_path" in locals() and temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
        logger.error("Invalid legacy sessions file was copied to a safety backup")
        raise exc
    return _LegacySource("imported", digest, sessions, raw)


def _source_is_unchanged(path: Path, source: _LegacySource) -> bool:
    """Fence a stopped-old-worker migration against a concurrent legacy write."""

    try:
        current = path.read_bytes()
    except FileNotFoundError:
        return source.state == "absent"
    except OSError as exc:
        raise LegacySessionMigrationError("legacy sessions file could not be revalidated") from exc
    return source.raw is not None and secrets.compare_digest(
        hashlib.sha256(current).hexdigest(),
        source.sha256,
    )


class SessionRepository:
    """Asynchronous, multi-worker-safe conversation state."""

    def __init__(
        self,
        database: StateDatabase | None = None,
        *,
        legacy_path: str | Path | None = None,
    ) -> None:
        self.database = database or StateDatabase()
        self.legacy_path = Path(legacy_path or DEFAULT_LEGACY_PATH).expanduser()
        if not self.legacy_path.is_absolute():
            self.legacy_path = PROJECT_ROOT / self.legacy_path

    async def initialize(self) -> None:
        """Initialize SQLite and perform the legacy import exactly once."""

        await self.database.initialize()

        # Avoid reading a stale or newly corrupted legacy file after a prior
        # worker has already completed the one-time migration.
        state = await self._migration_state()
        if state == "complete":
            return
        if state == "conflict":
            raise LegacySessionConflictError(
                "session_messages is non-empty but its legacy import marker is missing"
            )

        source = await asyncio.to_thread(_read_legacy_source, self.legacy_path)

        def migrate(transaction: Transaction) -> None:
            marker = transaction.read(
                "SELECT source_state FROM legacy_imports WHERE source_name = ?",
                (_LEGACY_SOURCE_NAME,),
            )
            if marker:
                return

            # This callback already runs on the database worker thread.  Hold
            # the short write transaction while re-reading only this small
            # legacy document so a changed source cannot be committed under a
            # stale hash marker.
            if not _source_is_unchanged(self.legacy_path, source):
                raise LegacySessionMigrationError("legacy sessions file changed during migration")

            existing = transaction.read("SELECT COUNT(*) AS count FROM session_messages")[0]
            if int(existing["count"]) != 0:
                raise LegacySessionConflictError(
                    "session_messages became non-empty before legacy import was committed"
                )

            imported_rows = 0
            if source.state == "imported":
                for session_key, messages in source.sessions.items():
                    for message in messages:
                        transaction.write(
                            """
                            INSERT INTO session_messages (session_key, role, content)
                            VALUES (?, ?, ?)
                            """,
                            (session_key, message["role"], message["content"]),
                        )
                        imported_rows += 1

            transaction.write(
                """
                INSERT INTO legacy_imports
                    (source_name, source_sha256, imported_rows, source_state)
                VALUES (?, ?, ?, ?)
                """,
                (_LEGACY_SOURCE_NAME, source.sha256, imported_rows, source.state),
            )

        await self.database.transaction(migrate, immediate=True)

    async def _migration_state(self) -> str:
        def inspect_state(transaction: Transaction) -> str:
            marker = transaction.read(
                "SELECT 1 AS present FROM legacy_imports WHERE source_name = ?",
                (_LEGACY_SOURCE_NAME,),
            )
            if marker:
                return "complete"
            row = transaction.read("SELECT COUNT(*) AS count FROM session_messages")[0]
            return "conflict" if int(row["count"]) else "pending"

        return await self.database.transaction(inspect_state, immediate=True)

    async def get(self, key: str) -> list[SessionMessage]:
        """Return all retained messages for one session in conversation order."""

        clean_key = _validate_key(key)
        rows = await self.database.read(
            """
            SELECT role, content
            FROM session_messages
            WHERE session_key = ?
            ORDER BY id ASC
            """,
            (clean_key,),
        )
        return [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]

    async def as_messages(self, key: str, last_n: int = 20) -> list[SessionMessage]:
        """Return at most ``last_n`` recent OpenAI-format messages."""

        clean_key = _validate_key(key)
        if isinstance(last_n, bool) or not isinstance(last_n, int):
            raise TypeError("last_n must be an integer")
        if last_n <= 0:
            return []
        limit = min(last_n, MAX_MESSAGES)
        rows = await self.database.read(
            """
            SELECT role, content
            FROM (
                SELECT id, role, content
                FROM session_messages
                WHERE session_key = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (clean_key, limit),
        )
        return [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]

    async def id_boundary(self, key: str) -> tuple[int | None, int | None]:
        """Return ``(oldest_id, newest_id)`` of the retained messages, or Nones."""

        clean_key = _validate_key(key)
        rows = await self.database.read(
            """
            SELECT MIN(id) AS oldest, MAX(id) AS newest
            FROM session_messages
            WHERE session_key = ?
            """,
            (clean_key,),
        )
        row = rows[0] if rows else None
        oldest = int(row["oldest"]) if row and row["oldest"] is not None else None
        newest = int(row["newest"]) if row and row["newest"] is not None else None
        return oldest, newest

    async def range_messages(self, key: str, *, from_id: int, to_id: int) -> list[SessionMessage]:
        """Return retained messages with ``from_id <= id <= to_id`` in order."""

        clean_key = _validate_key(key)
        if isinstance(from_id, bool) or not isinstance(from_id, int) or from_id < 0:
            raise ValueError("from_id must be a non-negative integer")
        if isinstance(to_id, bool) or not isinstance(to_id, int) or to_id < from_id:
            raise ValueError("to_id must be an integer >= from_id")
        rows = await self.database.read(
            """
            SELECT role, content
            FROM session_messages
            WHERE session_key = ? AND id >= ? AND id <= ?
            ORDER BY id ASC
            """,
            (clean_key, from_id, to_id),
        )
        return [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]

    async def append_turn(self, key: str, user_msg: str, assistant_msg: str) -> None:
        """Atomically append a bounded pair and compact the session."""

        clean_key = _validate_key(key)
        clean_user = _validate_content(user_msg, field="user message")
        clean_assistant = _validate_content(assistant_msg, field="assistant message")

        def append_and_trim(transaction: Transaction) -> None:
            transaction.write(
                "INSERT INTO session_messages (session_key, role, content) VALUES (?, 'user', ?)",
                (clean_key, clean_user),
            )
            transaction.write(
                """
                INSERT INTO session_messages (session_key, role, content)
                VALUES (?, 'assistant', ?)
                """,
                (clean_key, clean_assistant),
            )

            rows = transaction.read(
                """
                SELECT id, length(content) AS chars
                FROM session_messages
                WHERE session_key = ?
                ORDER BY id ASC
                """,
                (clean_key,),
            )
            total_chars = sum(int(row["chars"]) for row in rows)
            delete_ids: list[int] = []
            while len(rows) - len(delete_ids) > MAX_MESSAGES or total_chars > MAX_SESSION_CHARS:
                remaining = len(rows) - len(delete_ids)
                remove_count = 2 if remaining >= 2 else 1
                start = len(delete_ids)
                for row in rows[start : start + remove_count]:
                    delete_ids.append(int(row["id"]))
                    total_chars -= int(row["chars"])

            if delete_ids:
                placeholders = ", ".join("?" for _ in delete_ids)
                transaction.write(
                    f"DELETE FROM session_messages WHERE session_key = ? AND id IN ({placeholders})",
                    (clean_key, *delete_ids),
                )

        await self.database.transaction(append_and_trim, immediate=True)

    async def delete(self, key: str) -> bool:
        """Delete one session durably and report whether it existed."""

        clean_key = _validate_key(key)
        result = await self.database.write(
            "DELETE FROM session_messages WHERE session_key = ?",
            (clean_key,),
        )
        return result.rowcount > 0

    async def clear(self, key: str) -> bool:
        """Compatibility alias for :meth:`delete`."""

        return await self.delete(key)

    async def list_summaries(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SessionSummary]:
        """List bounded per-session metadata, newest activity first."""

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("offset must be an integer")
        bounded_limit = max(1, min(limit, 100))
        bounded_offset = max(0, offset)

        rows = await self.database.read(
            """
            SELECT
                grouped.session_key,
                grouped.message_count,
                grouped.total_chars,
                grouped.updated_at,
                latest.content AS last_content
            FROM (
                SELECT
                    session_key,
                    COUNT(*) AS message_count,
                    COALESCE(SUM(length(content)), 0) AS total_chars,
                    MAX(created_at) AS updated_at,
                    MAX(id) AS last_id
                FROM session_messages
                GROUP BY session_key
            ) AS grouped
            JOIN session_messages AS latest ON latest.id = grouped.last_id
            ORDER BY grouped.last_id DESC
            LIMIT ? OFFSET ?
            """,
            (bounded_limit, bounded_offset),
        )
        return [
            {
                "key": str(row["session_key"]),
                "message_count": int(row["message_count"]),
                "total_chars": int(row["total_chars"]),
                "last_preview": str(row["last_content"])[:120],
                "updated_at": int(row["updated_at"]),
            }
            for row in rows
        ]

    async def cleanup_inactive(self, *, before: int, limit: int = 1_000) -> int:
        """Delete a bounded number of sessions with no activity after ``before``."""

        if isinstance(before, bool) or not isinstance(before, int) or before < 0:
            raise ValueError("before must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        now_ms = time.time_ns() // 1_000_000

        def cleanup(transaction: Transaction) -> int:
            stale = transaction.read(
                """
                SELECT messages.session_key
                FROM session_messages AS messages
                WHERE NOT EXISTS (
                    SELECT 1 FROM session_leases AS leases
                    WHERE leases.session_key = messages.session_key
                      AND leases.lease_expires_at > ?
                )
                GROUP BY messages.session_key
                HAVING MAX(messages.created_at) < ?
                ORDER BY MAX(messages.created_at), messages.session_key
                LIMIT ?
                """,
                (now_ms, before, limit),
            )
            if not stale:
                return 0
            keys = [str(row["session_key"]) for row in stale]
            placeholders = ",".join("?" for _ in keys)
            transaction.write(
                f"DELETE FROM session_leases WHERE session_key IN ({placeholders})",
                keys,
            )
            result = transaction.write(
                f"DELETE FROM session_messages WHERE session_key IN ({placeholders})",
                keys,
            )
            return max(0, result.rowcount)

        return await self.database.transaction(cleanup)

    async def healthcheck(self) -> bool:
        """Confirm both the shared schema and this repository's migration state."""

        if not await self.database.healthcheck():
            return False
        try:
            marker = await self.database.read(
                """
                SELECT source_state
                FROM legacy_imports
                WHERE source_name = ?
                """,
                (_LEGACY_SOURCE_NAME,),
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logger.warning("Session repository health check failed", exc_info=True)
            return False
        return len(marker) == 1 and marker[0]["source_state"] in {"absent", "imported"}

    @asynccontextmanager
    async def session_guard(
        self,
        key: str,
        ttl: float,
        *,
        wait_timeout: float | None = None,
        poll_interval: float = 0.05,
    ) -> AsyncIterator[str]:
        """Acquire a cross-worker lease and release only the token we own.

        ``ttl`` bounds stale-lock lifetime if a worker dies.  ``wait_timeout``
        defaults to the same duration and may be zero for one non-blocking
        acquisition attempt.  The yielded random token is useful for tracing;
        callers must not persist or reuse it.
        """

        clean_key = _validate_key(key)
        clean_ttl = self._positive_duration(ttl, name="ttl")
        if wait_timeout is None:
            clean_wait = clean_ttl
        else:
            clean_wait = self._nonnegative_duration(wait_timeout, name="wait_timeout")
        clean_poll = self._positive_duration(poll_interval, name="poll_interval")

        token = secrets.token_urlsafe(32)
        deadline = time.monotonic() + clean_wait
        acquired = False
        while True:
            now_ms = time.time_ns() // 1_000_000
            expires_ms = now_ms + max(1, math.ceil(clean_ttl * 1_000))
            result = await self.database.write(
                """
                INSERT INTO session_leases (session_key, lease_token, lease_expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    lease_token = excluded.lease_token,
                    lease_expires_at = excluded.lease_expires_at
                WHERE session_leases.lease_expires_at <= ?
                """,
                (clean_key, token, expires_ms, now_ms),
            )
            if result.rowcount == 1:
                acquired = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for session lease {clean_key!r}")
            await asyncio.sleep(min(clean_poll, remaining))

        try:
            yield token
        finally:
            if acquired:
                # A stale owner must never release a successor's lease after its
                # own TTL elapsed, hence both key and unique token are matched.
                await self.database.write(
                    "DELETE FROM session_leases WHERE session_key = ? AND lease_token = ?",
                    (clean_key, token),
                )

    @staticmethod
    def _positive_duration(value: float, *, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{name} must be finite and greater than zero")
        return result

    @staticmethod
    def _nonnegative_duration(value: float, *, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return result

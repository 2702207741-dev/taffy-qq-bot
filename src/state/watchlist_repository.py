"""SQLite repository for per-user market watchlists.

The repository owns validation, quota enforcement, and the one-shot import of
the legacy ``watchlist.json`` file.  All invariants that depend on current
database state are checked inside an immediate transaction so separate bot
processes cannot race past the per-owner quota.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.database import PROJECT_ROOT, StateDatabase, Transaction

from .repository import Repository, positive_int

MAX_ITEMS_PER_OWNER = 20
MAX_DISPLAY_NAME_LENGTH = 120
MAX_NORMALIZED_NAME_LENGTH = 240
MAX_LEGACY_FILE_BYTES = 1_000_000
LEGACY_SOURCE_NAME = "watchlist-json-v1"
_ABSENT_DIGEST = hashlib.sha256(b"").hexdigest()


class WatchlistRepositoryError(RuntimeError):
    """Base class for repository-level watchlist failures."""


class WatchlistQuotaExceededError(WatchlistRepositoryError):
    """An owner already has the maximum permitted number of items."""


class LegacyWatchlistImportError(WatchlistRepositoryError):
    """The legacy file exists but cannot be imported without data loss."""


class _StrictJSONError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WatchlistItem:
    id: int
    owner_user_id: int
    display_name: str
    normalized_name: str
    created_at: int


@dataclass(frozen=True, slots=True)
class WatchlistAddResult:
    item: WatchlistItem
    created: bool
    count: int


def _owner_id(value: object) -> int:
    return positive_int(value, name="owner_user_id")


def _names(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("watchlist name must be a string")
    display_name = unicodedata.normalize("NFKC", value).strip()
    normalized_name = display_name.casefold()
    if not display_name:
        raise ValueError("watchlist name must not be empty")
    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(f"watchlist name must not exceed {MAX_DISPLAY_NAME_LENGTH} characters")
    if len(normalized_name) > MAX_NORMALIZED_NAME_LENGTH:
        raise ValueError("normalized watchlist name is too long")
    return display_name, normalized_name


def _item(row: Mapping[str, Any]) -> WatchlistItem:
    return WatchlistItem(
        id=int(row["id"]),
        owner_user_id=int(row["owner_user_id"]),
        display_name=str(row["display_name"]),
        normalized_name=str(row["normalized_name"]),
        created_at=int(row["created_at"]),
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    del value
    raise _StrictJSONError("non-finite JSON number")


def _backup_invalid_source(path: Path, payload: bytes) -> None:
    """Durably preserve exact invalid bytes without disclosing the source path."""

    digest = hashlib.sha256(payload).hexdigest()
    backup = path.with_name(f"{path.name}.invalid-{digest[:16]}.bak")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, backup)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise LegacyWatchlistImportError(
            "invalid legacy watchlist could not be backed up safely"
        ) from exc


def _verify_legacy_source(path: Path, source_state: str, digest: str) -> None:
    """Recheck the validated source while the database write lock is held."""

    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        if source_state == "absent":
            return
        raise LegacyWatchlistImportError("legacy watchlist changed during migration") from None
    except OSError as exc:
        raise LegacyWatchlistImportError("legacy watchlist changed during migration") from exc
    if source_state == "absent" or hashlib.sha256(payload).hexdigest() != digest:
        raise LegacyWatchlistImportError("legacy watchlist changed during migration")


def _parse_legacy(payload: bytes) -> list[tuple[int, str, str]]:
    if len(payload) > MAX_LEGACY_FILE_BYTES:
        raise LegacyWatchlistImportError("legacy watchlist file is too large")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _StrictJSONError) as exc:
        raise LegacyWatchlistImportError("legacy watchlist file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise LegacyWatchlistImportError("legacy watchlist root must be an object")

    parsed: list[tuple[int, str, str]] = []
    seen_owners: set[int] = set()
    for raw_owner, raw_items in value.items():
        if not isinstance(raw_owner, str) or not raw_owner.isascii() or not raw_owner.isdecimal():
            raise LegacyWatchlistImportError("legacy owner ids must be decimal strings")
        owner = int(raw_owner)
        if owner <= 0 or owner in seen_owners:
            raise LegacyWatchlistImportError("legacy owner ids must be unique positive integers")
        seen_owners.add(owner)
        if not isinstance(raw_items, list):
            raise LegacyWatchlistImportError("each legacy watchlist must be an array")
        if len(raw_items) > MAX_ITEMS_PER_OWNER:
            raise LegacyWatchlistImportError("a legacy owner exceeds the watchlist quota")

        seen_names: set[str] = set()
        for raw_name in raw_items:
            try:
                display_name, normalized_name = _names(raw_name)
            except ValueError as exc:
                raise LegacyWatchlistImportError(str(exc)) from exc
            if normalized_name in seen_names:
                raise LegacyWatchlistImportError(
                    "legacy watchlist contains normalization-equivalent duplicates"
                )
            seen_names.add(normalized_name)
            parsed.append((owner, display_name, normalized_name))
    return parsed


class WatchlistRepository(Repository):
    """Concurrency-safe watchlist storage backed by :class:`StateDatabase`."""

    def __init__(
        self,
        database: StateDatabase,
        *,
        legacy_path: str | Path | None = None,
        max_items_per_owner: int = MAX_ITEMS_PER_OWNER,
    ) -> None:
        if (
            isinstance(max_items_per_owner, bool)
            or not isinstance(max_items_per_owner, int)
            or not 1 <= max_items_per_owner <= MAX_ITEMS_PER_OWNER
        ):
            raise ValueError(f"max_items_per_owner must be between 1 and {MAX_ITEMS_PER_OWNER}")
        super().__init__(database)
        configured_path = Path(legacy_path) if legacy_path is not None else Path("watchlist.json")
        if not configured_path.is_absolute():
            configured_path = PROJECT_ROOT / configured_path
        self.legacy_path = configured_path.resolve()
        self.max_items_per_owner = max_items_per_owner

    async def initialize(self) -> None:
        """Initialize schema and import the legacy JSON file exactly once."""

        await super().initialize()
        if await self._legacy_already_processed():
            return

        try:
            payload = await asyncio.to_thread(self.legacy_path.read_bytes)
        except FileNotFoundError:
            rows: list[tuple[int, str, str]] = []
            source_state = "absent"
            digest = _ABSENT_DIGEST
        except OSError as exc:
            raise LegacyWatchlistImportError("legacy watchlist file cannot be read") from exc
        else:
            legacy_payload: bytes = payload
            try:
                rows = _parse_legacy(legacy_payload)
            except LegacyWatchlistImportError:
                await asyncio.to_thread(
                    _backup_invalid_source,
                    self.legacy_path,
                    legacy_payload,
                )
                raise
            source_state = "imported"
            digest = hashlib.sha256(legacy_payload).hexdigest()

        def migrate(transaction: Transaction) -> None:
            if transaction.read(
                "SELECT 1 FROM legacy_imports WHERE source_name = ?",
                (LEGACY_SOURCE_NAME,),
            ):
                return

            _verify_legacy_source(self.legacy_path, source_state, digest)
            if transaction.read("SELECT 1 FROM watchlist_items LIMIT 1"):
                raise LegacyWatchlistImportError(
                    "existing watchlist state has no legacy import marker"
                )

            imported_rows = 0
            for owner, display_name, normalized_name in rows:
                result = transaction.write(
                    """
                    INSERT INTO watchlist_items
                        (owner_user_id, display_name, normalized_name)
                    VALUES (?, ?, ?)
                    """,
                    (owner, display_name, normalized_name),
                )
                imported_rows += result.rowcount
            transaction.write(
                """
                INSERT INTO legacy_imports
                    (source_name, source_sha256, imported_rows, source_state)
                VALUES (?, ?, ?, ?)
                """,
                (LEGACY_SOURCE_NAME, digest, imported_rows, source_state),
            )

        await self.database.transaction(migrate)

    async def _legacy_already_processed(self) -> bool:
        rows = await self.database.read(
            "SELECT 1 FROM legacy_imports WHERE source_name = ?",
            (LEGACY_SOURCE_NAME,),
        )
        return bool(rows)

    async def list_owner(self, owner_user_id: int) -> list[WatchlistItem]:
        owner = _owner_id(owner_user_id)
        rows = await self.database.read(
            """
            SELECT id, owner_user_id, display_name, normalized_name, created_at
            FROM watchlist_items
            WHERE owner_user_id = ?
            ORDER BY id
            """,
            (owner,),
        )
        return [_item(row) for row in rows]

    async def add(self, owner_user_id: int, name: str) -> WatchlistAddResult:
        """Add a name idempotently while enforcing the owner quota atomically."""

        owner = _owner_id(owner_user_id)
        display_name, normalized_name = _names(name)

        def add_in_transaction(transaction: Transaction) -> WatchlistAddResult:
            existing = transaction.read(
                """
                SELECT id, owner_user_id, display_name, normalized_name, created_at
                FROM watchlist_items
                WHERE owner_user_id = ? AND normalized_name = ?
                """,
                (owner, normalized_name),
            )
            count = int(
                transaction.read(
                    "SELECT COUNT(*) AS count FROM watchlist_items WHERE owner_user_id = ?",
                    (owner,),
                )[0]["count"]
            )
            if existing:
                return WatchlistAddResult(_item(existing[0]), False, count)
            if count >= self.max_items_per_owner:
                raise WatchlistQuotaExceededError(
                    f"owner {owner} already has {self.max_items_per_owner} watchlist items"
                )

            result = transaction.write(
                """
                INSERT INTO watchlist_items
                    (owner_user_id, display_name, normalized_name)
                VALUES (?, ?, ?)
                """,
                (owner, display_name, normalized_name),
            )
            rows = transaction.read(
                """
                SELECT id, owner_user_id, display_name, normalized_name, created_at
                FROM watchlist_items WHERE id = ?
                """,
                (result.lastrowid,),
            )
            return WatchlistAddResult(_item(rows[0]), True, count + 1)

        return await self.database.transaction(add_in_transaction)

    async def remove_at(self, owner_user_id: int, index: int) -> WatchlistItem | None:
        """Remove an owner's item at a zero-based stable display index."""

        owner = _owner_id(owner_user_id)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("index must be a non-negative integer")

        def remove_in_transaction(transaction: Transaction) -> WatchlistItem | None:
            rows = transaction.read(
                """
                SELECT id, owner_user_id, display_name, normalized_name, created_at
                FROM watchlist_items
                WHERE owner_user_id = ?
                ORDER BY id
                LIMIT 1 OFFSET ?
                """,
                (owner, index),
            )
            if not rows:
                return None
            item = _item(rows[0])
            transaction.write(
                "DELETE FROM watchlist_items WHERE id = ? AND owner_user_id = ?",
                (item.id, owner),
            )
            return item

        return await self.database.transaction(remove_in_transaction)

    async def admin_summary(self) -> dict[str, int]:
        """Return per-owner counts without exposing saved market names."""

        rows = await self.database.read(
            """
            SELECT owner_user_id, COUNT(*) AS count
            FROM watchlist_items
            GROUP BY owner_user_id
            ORDER BY owner_user_id
            """
        )
        return {str(row["owner_user_id"]): int(row["count"]) for row in rows}

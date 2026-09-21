"""SQLite repository for per-user feature permission overrides.

Absence of a row means "default allowed" so existing behaviour is preserved
for every user until an administrator explicitly denies or re-allows a
feature.  All mutations run inside immediate transactions so concurrent bot
processes cannot race past each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.database import Transaction

from .repository import Filters, Repository, page_bounds, positive_int, text_value, total_count

FEATURE_NAME_MAX_LENGTH = 64

_COLUMNS = "user_id, feature, allowed, updated_at"


class PermissionRepositoryError(RuntimeError):
    """Base class for repository-level permission failures."""


def _feature(value: object) -> str:
    feature = text_value(value, name="feature").strip()
    if not feature or len(feature) > FEATURE_NAME_MAX_LENGTH:
        raise ValueError("feature name is invalid")
    return feature


def _allowed(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("allowed must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class PermissionOverride:
    user_id: int
    feature: str
    allowed: bool
    updated_at: int


def _row_to_override(row: dict[str, Any]) -> PermissionOverride:
    return PermissionOverride(
        user_id=int(row["user_id"]),
        feature=str(row["feature"]),
        allowed=bool(row["allowed"]),
        updated_at=int(row["updated_at"]),
    )


def _filters(user_id: int | None, feature: str | None) -> Filters:
    filters = Filters()
    if user_id is not None:
        filters.equals("user_id", positive_int(user_id, name="user_id"))
    if feature is not None:
        filters.equals("feature", _feature(feature))
    return filters


class PermissionRepository(Repository):
    """Store and query feature permission overrides."""

    async def set(
        self,
        user_id: int,
        feature: str,
        allowed: bool,
    ) -> PermissionOverride:
        user_id = positive_int(user_id, name="user_id")
        feature = _feature(feature)
        allowed = _allowed(allowed)

        def mutate(transaction: Transaction) -> PermissionOverride:
            transaction.write(
                """
                INSERT INTO feature_permissions (user_id, feature, allowed)
                VALUES (?, ?, ?)
                ON CONFLICT (user_id, feature) DO UPDATE SET
                    allowed = excluded.allowed,
                    updated_at = CAST(strftime('%s', 'now') AS INTEGER)
                """,
                (user_id, feature, int(allowed)),
            )
            rows = transaction.read(
                f"""
                SELECT {_COLUMNS}
                FROM feature_permissions
                WHERE user_id = ? AND feature = ?
                """,
                (user_id, feature),
            )
            if not rows:
                raise PermissionRepositoryError("permission override was not persisted")
            return _row_to_override(rows[0])

        return await self.database.transaction(mutate)

    async def clear(self, user_id: int, feature: str) -> bool:
        """Remove an override, restoring the default-allowed behaviour."""

        user_id = positive_int(user_id, name="user_id")
        feature = _feature(feature)

        def mutate(transaction: Transaction) -> bool:
            result = transaction.write(
                "DELETE FROM feature_permissions WHERE user_id = ? AND feature = ?",
                (user_id, feature),
            )
            return result.rowcount > 0

        return await self.database.transaction(mutate)

    async def get(self, user_id: int, feature: str) -> bool | None:
        """Return the override (``None`` means default allowed)."""

        user_id = positive_int(user_id, name="user_id")
        feature = _feature(feature)
        rows = await self.database.read(
            f"SELECT {_COLUMNS} FROM feature_permissions WHERE user_id = ? AND feature = ?",
            (user_id, feature),
        )
        return _row_to_override(rows[0]).allowed if rows else None

    async def list(
        self,
        *,
        user_id: int | None = None,
        feature: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PermissionOverride]:
        filters = _filters(user_id, feature)
        bounded_limit, bounded_offset = page_bounds(limit, offset)
        rows = await self.database.read(
            f"""
            SELECT {_COLUMNS} FROM feature_permissions
            {filters.clause}
            ORDER BY user_id ASC, feature ASC
            LIMIT ? OFFSET ?
            """,
            (*filters.parameters, bounded_limit, bounded_offset),
        )
        return [_row_to_override(row) for row in rows]

    async def count(self, *, user_id: int | None = None, feature: str | None = None) -> int:
        return await total_count(self.database, "feature_permissions", _filters(user_id, feature))


__all__ = [
    "FEATURE_NAME_MAX_LENGTH",
    "PermissionOverride",
    "PermissionRepository",
    "PermissionRepositoryError",
]

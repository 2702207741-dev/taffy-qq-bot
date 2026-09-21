"""SQLite repository for full feature-usage audit records.

Every auditable command writes one row with the caller, the feature category,
the command name, the raw input text and (where available) the bot's output.
The panel reads these rows; retention is enforced by state maintenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.database import Transaction

from .repository import (
    Filters,
    Repository,
    nonnegative_int,
    page_bounds,
    positive_int,
    text_value,
    total_count,
)

INPUT_TEXT_MAX_LENGTH = 4_000
OUTPUT_TEXT_MAX_LENGTH = 8_000
COMMAND_MAX_LENGTH = 200
FEATURE_MAX_LENGTH = 64
#: Upper bound per DELETE statement in :meth:`UsageLogRepository.cleanup`.
_CLEANUP_BATCH = 10_000

_COLUMNS = "id, user_id, group_id, feature, command, input_text, output_text, created_at"


class UsageLogError(RuntimeError):
    """Base class for usage-log failures."""


def _bounded(value: object, *, name: str, maximum: int) -> str:
    return text_value(value, name=name)[:maximum]


def _feature(value: object) -> str:
    return _bounded(value, name="feature", maximum=FEATURE_MAX_LENGTH).strip()


@dataclass(frozen=True, slots=True)
class UsageRecord:
    id: int
    user_id: int
    group_id: int
    feature: str
    command: str
    input_text: str
    output_text: str | None
    created_at: int


def _row_to_record(row: dict[str, Any]) -> UsageRecord:
    return UsageRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        group_id=int(row["group_id"]),
        feature=str(row["feature"]),
        command=str(row["command"]),
        input_text=str(row["input_text"]),
        output_text=str(row["output_text"]) if row["output_text"] is not None else None,
        created_at=int(row["created_at"]),
    )


def _filters(user_id: int | None, feature: str | None) -> Filters:
    filters = Filters()
    if user_id is not None:
        filters.equals("user_id", positive_int(user_id, name="user_id"))
    if feature is not None:
        filters.equals("feature", _feature(feature))
    return filters


class UsageLogRepository(Repository):
    """Append-only audit log with bounded page queries."""

    async def record(
        self,
        *,
        user_id: int,
        group_id: int = 0,
        feature: str,
        command: str,
        input_text: str,
        output_text: str | None = None,
    ) -> int:
        user_id = positive_int(user_id, name="user_id")
        group_id = nonnegative_int(group_id, name="group_id")
        feature = _feature(feature)
        command = _bounded(command, name="command", maximum=COMMAND_MAX_LENGTH).strip()
        input_text = _bounded(input_text, name="input_text", maximum=INPUT_TEXT_MAX_LENGTH)
        output_text = (
            _bounded(output_text, name="output_text", maximum=OUTPUT_TEXT_MAX_LENGTH)
            if output_text is not None
            else None
        )
        if not feature or not command:
            raise ValueError("feature and command must not be empty")

        def mutate(transaction: Transaction) -> int:
            result = transaction.write(
                """
                INSERT INTO usage_log
                    (user_id, group_id, feature, command, input_text, output_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, group_id, feature, command, input_text, output_text),
            )
            if result.lastrowid is None:
                raise UsageLogError("usage record was not persisted")
            return int(result.lastrowid)

        return await self.database.transaction(mutate)

    async def list(
        self,
        *,
        user_id: int | None = None,
        feature: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageRecord]:
        filters = _filters(user_id, feature)
        bounded_limit, bounded_offset = page_bounds(limit, offset)
        rows = await self.database.read(
            f"""
            SELECT {_COLUMNS}
            FROM usage_log
            {filters.clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*filters.parameters, bounded_limit, bounded_offset),
        )
        return [_row_to_record(row) for row in rows]

    async def count(
        self,
        *,
        user_id: int | None = None,
        feature: str | None = None,
    ) -> int:
        return await total_count(self.database, "usage_log", _filters(user_id, feature))

    async def cleanup(self, cutoff_created_at: int) -> int:
        """Delete audit rows older than the retention cutoff; return count.

        Runs in bounded batches so one huge purge cannot hold the write lock
        for an unbounded time (same convention as alerts/broadcasts).
        """

        def mutate(transaction: Transaction) -> int:
            deleted = 0
            while True:
                result = transaction.write(
                    """
                    DELETE FROM usage_log
                    WHERE id IN (
                        SELECT id FROM usage_log WHERE created_at < ? LIMIT ?
                    )
                    """,
                    (int(cutoff_created_at), _CLEANUP_BATCH),
                )
                removed = max(result.rowcount, 0)
                deleted += removed
                if removed < _CLEANUP_BATCH:
                    return deleted

        return await self.database.transaction(mutate)


__all__ = ["UsageLogRepository", "UsageRecord", "UsageLogError"]

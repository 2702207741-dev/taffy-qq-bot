"""Durable per-session chat summaries (schema v16 ``session_summaries``).

One summary per session covers the message-id range that no longer needs to
be replayed verbatim.  Summaries survive the session trim (old rows get
deleted once the window overflows) and are always scope-isolated; they are
cleared together with the conversation and purged as orphans by
maintenance when their session disappears.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.core.database import StateDatabase, Transaction
from src.state.repository import Repository

_MAX_SUMMARY_CHARS = 2_000


def _validate_key(key: str) -> str:
    clean = key.strip()
    if not clean or len(clean) > 256:
        raise ValueError("session_key must be 1..256 characters")
    return clean


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: int
    session_key: str
    summary: str
    covers_from_id: int
    covers_to_id: int
    created_at: int


class SessionSummaryRepository(Repository):
    """Store and query the latest compacted summary for each chat session."""

    def __init__(self, database: StateDatabase) -> None:
        super().__init__(database)

    async def latest(self, session_key: str) -> SessionSummary | None:
        clean_key = _validate_key(session_key)
        rows = await self.database.read(
            """
            SELECT id, session_key, summary, covers_from_id, covers_to_id, created_at
            FROM session_summaries
            WHERE session_key = ?
            ORDER BY covers_to_id DESC LIMIT 1
            """,
            (clean_key,),
        )
        if not rows:
            return None
        row = rows[0]
        return SessionSummary(
            id=int(row["id"]),
            session_key=str(row["session_key"]),
            summary=str(row["summary"]),
            covers_from_id=int(row["covers_from_id"]),
            covers_to_id=int(row["covers_to_id"]),
            created_at=int(row["created_at"]),
        )

    async def save(
        self,
        session_key: str,
        *,
        summary: str,
        covers_from_id: int,
        covers_to_id: int,
        created_at: int | None = None,
    ) -> SessionSummary:
        """Replace the session's summary with one newer, wider range."""

        clean_key = _validate_key(session_key)
        text = summary.strip()
        if not 2 <= len(text) <= _MAX_SUMMARY_CHARS:
            raise ValueError("summary must be 2..2000 characters")
        if covers_to_id < covers_from_id:
            raise ValueError("covers_to_id must be >= covers_from_id")

        def persist(transaction: Transaction) -> SessionSummary:
            stamp = int(created_at if created_at is not None else time.time())
            transaction.write("DELETE FROM session_summaries WHERE session_key = ?", (clean_key,))
            result = transaction.write(
                """
                INSERT INTO session_summaries (
                    session_key, summary, covers_from_id, covers_to_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (clean_key, text, covers_from_id, covers_to_id, stamp),
            )
            inserted = int(result.lastrowid or 0)
            return SessionSummary(
                id=inserted,
                session_key=clean_key,
                summary=text,
                covers_from_id=covers_from_id,
                covers_to_id=covers_to_id,
                created_at=stamp,
            )

        return await self.database.transaction(persist)

    async def clear(self, session_key: str) -> int:
        """Drop every summary for one session; returns the rowcount."""

        clean_key = _validate_key(session_key)
        result = await self.database.write(
            "DELETE FROM session_summaries WHERE session_key = ?",
            (clean_key,),
        )
        return result.rowcount

    async def count_since(self, since: int) -> int:
        """Global count of summaries created after ``since`` (budget meter)."""

        rows = await self.database.read(
            "SELECT COUNT(*) AS count FROM session_summaries WHERE created_at >= ?",
            (int(since),),
        )
        return int(rows[0]["count"]) if rows else 0

    async def purge_orphans(self, *, limit: int = 500) -> int:
        """Delete summaries whose session no longer retains any messages."""

        def cleanup(transaction: Transaction) -> int:
            rows = transaction.read(
                """
                SELECT id FROM session_summaries
                WHERE session_key NOT IN (
                    SELECT DISTINCT session_key FROM session_messages
                )
                LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ", ".join("?" for _ in ids)
            result = transaction.write(
                f"DELETE FROM session_summaries WHERE id IN ({placeholders})",
                tuple(ids),
            )
            return result.rowcount

        return await self.database.transaction(cleanup)

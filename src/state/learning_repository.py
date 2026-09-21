"""SQLite repository for semi-automated learning insights.

Every insight starts as ``pending``; only an explicit user or admin
confirmation moves it to ``accepted``.  ``evidence`` stores a digest of the
context (never raw conversation text).  The decay curve is applied by
:meth:`LearningInsightRepository.decay` so forgotten insights are archived
instead of deleted, keeping the record auditable.

Schema v17 adds bi-temporal validity (``observed_at``/``valid_from``/
``valid_until``), a ``hit_count``/``last_hit_at`` access-reinforcement pair,
an ``is_protected`` flag that exempts well-attested insights from decay, and
the ``deprecated`` soft-delete status (superseded facts stay auditable).
Cross-insight ``memory_links`` rows are managed through the same repository
because they share the insight lifecycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from src.core.database import Transaction

from .repository import Filters, Repository, nonnegative_int, page_bounds, positive_int

InsightKind = Literal["preference", "style", "knowledge", "cultural"]
InsightStatus = Literal["pending", "accepted", "rejected", "archived", "deprecated"]

_KINDS = {"preference", "style", "knowledge", "cultural"}
_STATUSES = {"pending", "accepted", "rejected", "archived", "deprecated"}
DAILY_PENDING_LIMIT = 3
_PENDING_BACKLOG_LIMIT = 10
_ARCHIVE_WEIGHT = 0.3
_DAILY_DECAY = 0.99
_INACTIVE_DECAY = 0.5
_INACTIVE_DAYS = 30
_HIT_REINFORCE = 1.05
_WEIGHT_CEILING = 2.0
_PROTECT_AFTER_HITS = 3

_COLUMNS = (
    "id, user_id, scope_group_id, kind, content, evidence, confidence, "
    "status, created_at, accepted_at, updated_at, weight, "
    "observed_at, valid_from, valid_until, hit_count, last_hit_at, is_protected"
)


class LearningInsightError(RuntimeError):
    """Base class for learning-insight persistence failures."""


@dataclass(frozen=True, slots=True)
class LearningInsight:
    id: int
    user_id: int
    scope_group_id: int
    kind: InsightKind
    content: str
    evidence: str
    confidence: int
    status: InsightStatus
    created_at: int
    accepted_at: int | None
    updated_at: int
    weight: float
    observed_at: int
    valid_from: int
    valid_until: int | None
    hit_count: int
    last_hit_at: int | None
    is_protected: bool


def _kind(value: object) -> InsightKind:
    if value not in _KINDS:
        raise ValueError("kind must be preference, style, knowledge or cultural")
    return value  # type: ignore[return-value]


def _status(value: object) -> InsightStatus:
    if value not in _STATUSES:
        raise ValueError("insight status is invalid")
    return value  # type: ignore[return-value]


def _content(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("insight content must be a string")
    content = value.strip()
    if not 2 <= len(content) <= 300:
        raise ValueError("insight content must contain 2 to 300 characters")
    return content


def _evidence(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("insight evidence must be a string")
    evidence = value.strip()
    if not evidence or len(evidence) > 400:
        raise ValueError("insight evidence must contain 1 to 400 characters")
    return evidence


def _confidence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError("insight confidence must be an integer between 1 and 5")
    return value


def _row_to_insight(row: dict[str, Any]) -> LearningInsight:
    return LearningInsight(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        scope_group_id=int(row["scope_group_id"]),
        kind=_kind(row["kind"]),
        content=str(row["content"]),
        evidence=str(row["evidence"]),
        confidence=int(row["confidence"]),
        status=_status(row["status"]),
        created_at=int(row["created_at"]),
        accepted_at=int(row["accepted_at"]) if row["accepted_at"] is not None else None,
        updated_at=int(row["updated_at"]),
        weight=float(row["weight"]),
        observed_at=int(row["observed_at"]),
        valid_from=int(row["valid_from"]),
        valid_until=int(row["valid_until"]) if row["valid_until"] is not None else None,
        hit_count=int(row["hit_count"]),
        last_hit_at=int(row["last_hit_at"]) if row["last_hit_at"] is not None else None,
        is_protected=bool(row["is_protected"]),
    )


class LearningInsightRepository(Repository):
    """Confirmed-first insight storage with scope isolation and decay."""

    async def create_pending(
        self,
        user_id: int,
        scope_group_id: int,
        kind: str,
        content: str,
        evidence: str,
        *,
        confidence: int = 1,
        now: int | None = None,
    ) -> LearningInsight | None:
        """Insert one pending insight; ``None`` when a duplicate already exists."""

        user = positive_int(user_id, name="user_id")
        scope = nonnegative_int(scope_group_id, name="scope_group_id")
        clean_kind = _kind(kind)
        clean_content = _content(content)
        clean_evidence = _evidence(evidence)
        clean_confidence = _confidence(confidence)
        timestamp = int(time.time() if now is None else now)

        def insert(transaction: Transaction) -> LearningInsight | None:
            duplicate = transaction.read(
                """
                SELECT 1 AS present FROM learning_insights
                WHERE user_id = ? AND scope_group_id = ? AND kind = ? AND content = ?
                LIMIT 1
                """,
                (user, scope, clean_kind, clean_content),
            )
            if duplicate:
                return None
            result = transaction.write(
                """
                INSERT INTO learning_insights
                    (user_id, scope_group_id, kind, content, evidence, confidence,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    user,
                    scope,
                    clean_kind,
                    clean_content,
                    clean_evidence,
                    clean_confidence,
                    timestamp,
                    timestamp,
                ),
            )
            rows = transaction.read(
                f"SELECT {_COLUMNS} FROM learning_insights WHERE id = ?",
                (result.lastrowid,),
            )
            if not rows:
                raise LearningInsightError("insight was not persisted")
            return _row_to_insight(rows[0])

        return await self.database.transaction(insert)

    async def pending_count(self, user_id: int, scope_group_id: int) -> int:
        """Return the number of open pending insights for one scope."""

        user = positive_int(user_id, name="user_id")
        scope = nonnegative_int(scope_group_id, name="scope_group_id")
        rows = await self.database.read(
            """
            SELECT COUNT(*) AS total FROM learning_insights
            WHERE user_id = ? AND scope_group_id = ? AND status = 'pending'
            """,
            (user, scope),
        )
        return int(rows[0]["total"]) if rows else 0

    async def count_created_since(
        self,
        user_id: int,
        scope_group_id: int,
        *,
        since: int,
    ) -> int:
        """Count insights created after ``since`` (daily-generation throttle)."""

        user = positive_int(user_id, name="user_id")
        scope = nonnegative_int(scope_group_id, name="scope_group_id")
        rows = await self.database.read(
            """
            SELECT COUNT(*) AS total FROM learning_insights
            WHERE user_id = ? AND scope_group_id = ? AND created_at >= ?
            """,
            (user, scope, int(since)),
        )
        return int(rows[0]["total"]) if rows else 0

    async def list_scope(
        self,
        user_id: int,
        scope_group_id: int,
        *,
        status: str | None = None,
        kind: str | None = None,
    ) -> tuple[LearningInsight, ...]:
        """List one scope's insights (accepted by default)."""

        user = positive_int(user_id, name="user_id")
        scope = nonnegative_int(scope_group_id, name="scope_group_id")
        clean_status = _status(status) if status is not None else "accepted"
        kind_clause = ""
        parameters: tuple[object, ...] = (user, scope, clean_status)
        if kind is not None:
            kind_clause = " AND kind = ?"
            parameters = (user, scope, clean_status, _kind(kind))
        rows = await self.database.read(
            f"""
            SELECT {_COLUMNS} FROM learning_insights
            WHERE user_id = ? AND scope_group_id = ? AND status = ?{kind_clause}
            ORDER BY created_at DESC, id DESC
            """,
            parameters,
        )
        return tuple(_row_to_insight(row) for row in rows)

    async def confirm(
        self,
        insight_id: int,
        user_id: int,
        scope_group_id: int,
        *,
        now: int | None = None,
    ) -> bool:
        """Move one pending insight to accepted (only within its own scope)."""

        timestamp = int(time.time() if now is None else now)
        result = await self.database.write(
            """
            UPDATE learning_insights
            SET status = 'accepted', accepted_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND scope_group_id = ? AND status = 'pending'
            """,
            (timestamp, timestamp, insight_id, user_id, scope_group_id),
        )
        return result.rowcount == 1

    async def reject(
        self,
        insight_id: int,
        user_id: int,
        scope_group_id: int,
        *,
        now: int | None = None,
    ) -> bool:
        """Move one pending insight to rejected."""

        timestamp = int(time.time() if now is None else now)
        result = await self.database.write(
            """
            UPDATE learning_insights
            SET status = 'rejected', updated_at = ?
            WHERE id = ? AND user_id = ? AND scope_group_id = ? AND status = 'pending'
            """,
            (timestamp, insight_id, user_id, scope_group_id),
        )
        return result.rowcount == 1

    async def delete(self, insight_id: int, user_id: int, scope_group_id: int) -> bool:
        """Physically delete one accepted insight (user undo)."""

        result = await self.database.write(
            """
            DELETE FROM learning_insights
            WHERE id = ? AND user_id = ? AND scope_group_id = ? AND status = 'accepted'
            """,
            (insight_id, user_id, scope_group_id),
        )
        return result.rowcount == 1

    async def supersede(
        self,
        insight_id: int,
        user_id: int,
        scope_group_id: int,
        *,
        now: int | None = None,
    ) -> bool:
        """Soft-delete one accepted insight as superseded (bi-temporal close)."""

        timestamp = int(time.time() if now is None else now)
        result = await self.database.write(
            """
            UPDATE learning_insights
            SET status = 'deprecated', valid_until = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND scope_group_id = ? AND status = 'accepted'
            """,
            (timestamp, timestamp, insight_id, user_id, scope_group_id),
        )
        return result.rowcount == 1

    async def record_hit(self, insight_id: int, *, now: int | None = None) -> bool:
        """Reinforce one accepted insight because retrieval used it.

        Each hit nudges the weight back up (capped) so the forgetting curve
        never erases memories that keep proving useful.
        """

        timestamp = int(time.time() if now is None else now)
        result = await self.database.write(
            """
            UPDATE learning_insights
            SET hit_count = hit_count + 1,
                last_hit_at = ?,
                weight = MIN(weight * ?, ?),
                is_protected = CASE WHEN hit_count + 1 >= ? THEN 1 ELSE is_protected END,
                updated_at = ?
            WHERE id = ? AND status = 'accepted'
            """,
            (
                timestamp,
                _HIT_REINFORCE,
                _WEIGHT_CEILING,
                _PROTECT_AFTER_HITS,
                timestamp,
                insight_id,
            ),
        )
        return result.rowcount == 1

    async def merge_update(
        self,
        insight_id: int,
        *,
        content: str | None = None,
        evidence: str | None = None,
        confidence: int | None = None,
        weight: float | None = None,
        now: int | None = None,
    ) -> bool:
        """Rewrite one insight in place (consolidation merge target)."""

        timestamp = int(time.time() if now is None else now)
        assignments: list[str] = ["updated_at = ?"]
        parameters: list[object] = [timestamp]
        if content is not None:
            assignments.append("content = ?")
            parameters.append(_content(content))
        if evidence is not None:
            assignments.append("evidence = ?")
            parameters.append(_evidence(evidence))
        if confidence is not None:
            assignments.append("confidence = ?")
            parameters.append(_confidence(confidence))
        if weight is not None:
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
                raise ValueError("insight weight must be a non-negative number")
            assignments.append("weight = ?")
            parameters.append(float(weight))
        parameters.append(insight_id)
        result = await self.database.write(
            f"UPDATE learning_insights SET {', '.join(assignments)} WHERE id = ?",
            tuple(parameters),
        )
        return result.rowcount == 1

    async def add_link(
        self,
        user_id: int,
        scope_group_id: int,
        *,
        from_insight_id: int,
        to_insight_id: int,
        relation: str,
        now: int | None = None,
    ) -> bool:
        """Insert one cross-insight link; ``False`` when it already exists."""

        user = positive_int(user_id, name="user_id")
        scope = nonnegative_int(scope_group_id, name="scope_group_id")
        clean_relation = relation.strip()
        if not 2 <= len(clean_relation) <= 60:
            raise ValueError("relation must contain 2 to 60 characters")
        timestamp = int(time.time() if now is None else now)
        result = await self.database.write(
            """
            INSERT OR IGNORE INTO memory_links
                (user_id, scope_group_id, from_insight_id, to_insight_id, relation, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user, scope, from_insight_id, to_insight_id, clean_relation, timestamp),
        )
        return result.rowcount == 1

    async def links_for_scope(
        self,
        user_id: int,
        scope_group_id: int,
        *,
        limit: int = 50,
    ) -> tuple[tuple[int, int, str], ...]:
        """Return ``(from_id, to_id, relation)`` links for one scope."""

        user = positive_int(user_id, name="user_id")
        scope = nonnegative_int(scope_group_id, name="scope_group_id")
        rows = await self.database.read(
            """
            SELECT from_insight_id, to_insight_id, relation FROM memory_links
            WHERE user_id = ? AND scope_group_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user, scope, max(1, min(int(limit), 200))),
        )
        return tuple(
            (int(row["from_insight_id"]), int(row["to_insight_id"]), str(row["relation"]))
            for row in rows
        )

    async def admin_list(
        self,
        *,
        user_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[tuple[LearningInsight, ...], int]:
        filters = Filters()
        if user_id is not None:
            filters.equals("user_id", positive_int(user_id, name="user_id"))
        if status is not None:
            filters.equals("status", _status(status))
        bounded_limit, bounded_offset = page_bounds(limit, offset)

        def page(transaction: Transaction) -> tuple[tuple[LearningInsight, ...], int]:
            # Page and total must come from one consistent snapshot; two
            # separate transactions could straddle a concurrent insert.
            rows = transaction.read(
                f"""
                SELECT {_COLUMNS} FROM learning_insights
                {filters.clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*filters.parameters, bounded_limit, bounded_offset),
            )
            total_rows = transaction.read(
                f"SELECT COUNT(*) AS total FROM learning_insights {filters.clause}",
                tuple(filters.parameters),
            )
            return (
                tuple(_row_to_insight(row) for row in rows),
                int(total_rows[0]["total"]) if total_rows else 0,
            )

        return await self.database.transaction(page, immediate=False)

    async def stats(self) -> dict[str, int]:
        rows = await self.database.read(
            """
            SELECT status, COUNT(*) AS total FROM learning_insights
            GROUP BY status
            """
        )
        counts = {str(row["status"]): int(row["total"]) for row in rows}
        return {
            status: counts.get(status, 0)
            for status in ("pending", "accepted", "rejected", "archived", "deprecated")
        }

    async def bump_confidence(
        self,
        insight_id: int,
        *,
        now: int | None = None,
    ) -> bool:
        """Raise one accepted insight's confidence by one point (cap 5)."""

        timestamp = int(time.time() if now is None else now)
        result = await self.database.write(
            """
            UPDATE learning_insights
            SET confidence = MIN(confidence + 1, 5), updated_at = ?
            WHERE id = ? AND status = 'accepted' AND confidence < 5
            """,
            (timestamp, insight_id),
        )
        return result.rowcount == 1

    async def decay(
        self,
        *,
        now: int | None = None,
        daily_decay: float = _DAILY_DECAY,
        inactive_decay: float = _INACTIVE_DECAY,
        inactive_days: int = _INACTIVE_DAYS,
        archive_weight: float = _ARCHIVE_WEIGHT,
    ) -> int:
        """Decay accepted weights, archive the forgotten; return archived count.

        ``inactive`` means the user produced no audited interaction (usage_log
        row) in the last ``inactive_days`` days; inactive insights decay twice
        as fast.  Higher-confidence memories decay more slowly and protected
        memories (``is_protected``) do not decay at all.  Archived rows stay
        stored for auditability.  Both steps run as set-based statements so
        statement count does not scale with rows.
        """

        timestamp = int(time.time() if now is None else now)
        inactive_cutoff = timestamp - inactive_days * 86400

        def run(transaction: Transaction) -> int:
            transaction.write(
                """
                UPDATE learning_insights
                SET weight = weight * ? * CASE
                        WHEN confidence >= 4 THEN 1.005
                        WHEN confidence <= 2 THEN 0.98
                        ELSE 1.0
                    END * CASE
                        WHEN EXISTS (
                            SELECT 1 FROM usage_log
                            WHERE usage_log.user_id = learning_insights.user_id
                              AND usage_log.created_at >= ?
                        ) THEN 1.0
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE status = 'accepted' AND is_protected = 0
                """,
                (daily_decay, inactive_cutoff, inactive_decay, timestamp),
            )
            result = transaction.write(
                """
                UPDATE learning_insights
                SET status = 'archived', updated_at = ?
                WHERE status = 'accepted' AND weight < ? AND is_protected = 0
                """,
                (timestamp, archive_weight),
            )
            return max(result.rowcount, 0)

        return await self.database.transaction(run)


__all__ = [
    "DAILY_PENDING_LIMIT",
    "LearningInsight",
    "LearningInsightError",
    "LearningInsightRepository",
]

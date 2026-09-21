"""Per-scope relationship intimacy derived only from confirmed signals.

The score aggregates three already-persisted, already-consensual sources —
``usage_log`` interaction counts, accepted ``learning_insights`` and
``persona_feedback_events`` — applies a documented idle-decay and never
stores chat content.  Users can query it (``塔菲我们熟吗``) and wipe it
with the existing ``忘记我`` / ``清空记忆`` flows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.core.database import StateDatabase, Transaction
from src.state.repository import Repository

MAX_INTIMACY = 100.0
#: Bands are deliberately neutral; no ranking-style praise.
BAND_STRANGER = "陌生"
BAND_ACQUAINTANCE = "点头之交"
BAND_FAMILIAR = "熟络"
BAND_CLOSE = "老友"


def band_for(intimacy: float) -> str:
    if intimacy < 15:
        return BAND_STRANGER
    if intimacy < 40:
        return BAND_ACQUAINTANCE
    if intimacy < 70:
        return BAND_FAMILIAR
    return BAND_CLOSE


def compute_intimacy(
    *,
    interaction_count: int,
    accepted_insight_count: int,
    style_feedback_count: int,
    last_interaction_at: int | None,
    now: int | None = None,
) -> float:
    """Transparent scoring formula; all inputs are confirmed-signal counts.

    ``2`` points per recorded interaction (cap 40), ``7`` per accepted
    impression (cap 35) and ``5`` per style feedback (cap 25), then a
    ``0.98`` idle-decay per full day since the last interaction (capped at
    180 days so long absence converges instead of reaching zero).
    """

    interaction_pts = min(40.0, 2.0 * max(0, interaction_count))
    insight_pts = min(35.0, 7.0 * max(0, accepted_insight_count))
    feedback_pts = min(25.0, 5.0 * max(0, style_feedback_count))
    total = interaction_pts + insight_pts + feedback_pts
    if last_interaction_at is not None:
        current = int(time.time()) if now is None else int(now)
        days_idle = max(0, (current - int(last_interaction_at)) // 86400)
        total *= 0.98 ** min(days_idle, 180)
    return round(min(MAX_INTIMACY, total), 1)


@dataclass(frozen=True, slots=True)
class PersonaRelationship:
    user_id: int
    scope_group_id: int
    intimacy: float
    interaction_count: int
    accepted_insight_count: int
    style_feedback_count: int
    last_interaction_at: int | None
    updated_at: int

    @property
    def band(self) -> str:
        return band_for(self.intimacy)


def _row_to_relationship(row: dict[str, Any]) -> PersonaRelationship:
    return PersonaRelationship(
        user_id=int(row["user_id"]),
        scope_group_id=int(row["scope_group_id"]),
        intimacy=float(row["intimacy"]),
        interaction_count=int(row["interaction_count"]),
        accepted_insight_count=int(row["accepted_insight_count"]),
        style_feedback_count=int(row["style_feedback_count"]),
        last_interaction_at=(
            int(row["last_interaction_at"]) if row["last_interaction_at"] is not None else None
        ),
        updated_at=int(row["updated_at"]),
    )


class PersonaRelationshipRepository(Repository):
    """Read-through intimacy state for one ``(user, scope)`` pair."""

    def __init__(self, database: StateDatabase) -> None:
        super().__init__(database)

    async def get(self, user_id: int, scope_group_id: int) -> PersonaRelationship | None:
        rows = await self.database.read(
            """
            SELECT user_id, scope_group_id, intimacy, interaction_count,
                   accepted_insight_count, style_feedback_count,
                   last_interaction_at, updated_at
            FROM persona_relationships
            WHERE user_id = ? AND scope_group_id = ?
            """,
            (user_id, scope_group_id),
        )
        return _row_to_relationship(rows[0]) if rows else None

    async def refresh(
        self, user_id: int, scope_group_id: int, *, now: int | None = None
    ) -> PersonaRelationship:
        """Recompute the score from confirmed signals and persist it."""

        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if (
            isinstance(scope_group_id, bool)
            or not isinstance(scope_group_id, int)
            or scope_group_id < 0
        ):
            raise ValueError("scope_group_id must be a non-negative integer")

        def collect(transaction: Transaction) -> tuple[int, int, int, int | None]:
            interactions = transaction.read(
                "SELECT COUNT(*) AS count FROM usage_log WHERE user_id = ? AND group_id = ?",
                (user_id, scope_group_id),
            )[0]["count"]
            insights = transaction.read(
                """
                SELECT COUNT(*) AS count FROM learning_insights
                WHERE user_id = ? AND scope_group_id = ? AND status = 'accepted'
                """,
                (user_id, scope_group_id),
            )[0]["count"]
            feedback = transaction.read(
                """
                SELECT COUNT(*) AS count FROM persona_feedback_events
                WHERE owner_user_id = ? AND scope_group_id = ?
                """,
                (user_id, scope_group_id),
            )[0]["count"]
            last = transaction.read(
                """
                SELECT MAX(created_at) AS last_at FROM usage_log
                WHERE user_id = ? AND group_id = ?
                """,
                (user_id, scope_group_id),
            )[0]["last_at"]
            return (
                int(interactions),
                int(insights),
                int(feedback),
                int(last) if last is not None else None,
            )

        interactions, insights, feedback, last_at = await self.database.transaction(collect)
        intimacy = compute_intimacy(
            interaction_count=interactions,
            accepted_insight_count=insights,
            style_feedback_count=feedback,
            last_interaction_at=last_at,
            now=now,
        )

        def persist(transaction: Transaction) -> PersonaRelationship:
            stamp = int(time.time()) if now is None else int(now)
            transaction.write(
                """
                INSERT INTO persona_relationships (
                    user_id, scope_group_id, intimacy, interaction_count,
                    accepted_insight_count, style_feedback_count,
                    last_interaction_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, scope_group_id) DO UPDATE SET
                    intimacy = excluded.intimacy,
                    interaction_count = excluded.interaction_count,
                    accepted_insight_count = excluded.accepted_insight_count,
                    style_feedback_count = excluded.style_feedback_count,
                    last_interaction_at = excluded.last_interaction_at,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    scope_group_id,
                    intimacy,
                    interactions,
                    insights,
                    feedback,
                    last_at,
                    stamp,
                ),
            )
            return PersonaRelationship(
                user_id=user_id,
                scope_group_id=scope_group_id,
                intimacy=intimacy,
                interaction_count=interactions,
                accepted_insight_count=insights,
                style_feedback_count=feedback,
                last_interaction_at=last_at,
                updated_at=stamp,
            )

        return await self.database.transaction(persist)

    async def clear(self, user_id: int, scope_group_id: int) -> int:
        result = await self.database.write(
            "DELETE FROM persona_relationships WHERE user_id = ? AND scope_group_id = ?",
            (user_id, scope_group_id),
        )
        return result.rowcount

"""Group-taught expression dictionary (schema v16 ``persona_slang``).

Terms enter as ``pending`` via the explicit ``塔菲学话`` command and only
reach the persona prompt after an explicit confirm, honouring the project
rule that every learned thing is confirmable, reviewable and deletable.
Scopes are strictly isolated: a group's dictionary never leaks to another
group or to private chats.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.core.database import StateDatabase, Transaction
from src.state.repository import Repository


class SlangAlreadyExistsError(ValueError):
    """The same term already exists in this scope."""


@dataclass(frozen=True, slots=True)
class PersonaSlang:
    id: int
    scope_group_id: int
    created_by: int
    term: str
    meaning: str
    status: str
    confidence: int
    created_at: int
    accepted_at: int | None
    enabled: bool


def _clean_term(term: str) -> str:
    value = term.strip()
    if not 1 <= len(value) <= 24:
        raise ValueError("词条长度必须在 1~24 个字之间")
    return value


def _clean_meaning(meaning: str) -> str:
    value = meaning.strip()
    if not 1 <= len(value) <= 200:
        raise ValueError("含义长度必须在 1~200 个字之间")
    return value


def _row(row: dict[str, Any]) -> PersonaSlang:
    return PersonaSlang(
        id=int(row["id"]),
        scope_group_id=int(row["scope_group_id"]),
        created_by=int(row["created_by"]),
        term=str(row["term"]),
        meaning=str(row["meaning"]),
        status=str(row["status"]),
        confidence=int(row["confidence"]),
        created_at=int(row["created_at"]),
        accepted_at=int(row["accepted_at"]) if row["accepted_at"] is not None else None,
        enabled=bool(row["enabled"]),
    )


#: ``scope_group_id == 0`` is the **private** bucket.  Unlike
#: ``persona_relationships`` (whose primary key contains ``user_id``), the slang
#: table's key has no user column, so private scope is *collapsed across users*:
#: ``created_by`` is the only discriminator and MUST be part of the predicate.
#: In a group scope, "who may confirm/delete this group's entries" is a
#: **permission** question (group admin? the teacher?) — using ``created_by``
#: there would turn "others can't act for the group admin" into "only whoever
#: taught the row can act", which is a different error.
_PRIVATE_SCOPE = 0


def _scope_clause(scope_group_id: int, created_by: int | None) -> tuple[str, tuple[object, ...]]:
    """SQL fragment + parameters pinning an id-keyed operation to one scope.

    Every id-keyed mutation needs this: without it a caller-supplied id reaches
    rows in **any** scope (the desktop endpoints pass a client-supplied
    ``entry_id`` straight through, so an id-only predicate is a cross-scope
    mutation primitive).
    """

    if scope_group_id == _PRIVATE_SCOPE:
        if created_by is None:
            raise ValueError(
                "private scope (0) is shared across users; created_by is required "
                "as the ownership predicate"
            )
        return " AND scope_group_id = ? AND created_by = ?", (scope_group_id, int(created_by))
    return " AND scope_group_id = ?", (scope_group_id,)


def _owner_clause(created_by: int | None) -> tuple[str, tuple[object, ...]]:
    """Optional owner predicate for scope listings.

    Mutations already refuse to run on private scope without ``created_by``
    (see ``_scope_clause``).  Listings need the same knob because
    ``scope_group_id == 0`` collapses every user's private rows into one
    bucket: omitting the filter hands one user another user's taught terms and
    author QQ id.  ``None`` means "every user" and is reserved for the
    operator console, so a forgotten filter cannot quietly become the default.
    """

    if created_by is None:
        return "", ()
    return " AND created_by = ?", (int(created_by),)


class PersonaSlangRepository(Repository):
    """Pending/accepted expression dictionary for one scope."""

    def __init__(self, database: StateDatabase) -> None:
        super().__init__(database)

    async def teach(
        self,
        scope_group_id: int,
        *,
        created_by: int,
        term: str,
        meaning: str,
    ) -> tuple[PersonaSlang, bool]:
        """Insert one pending term; ``(existing, False)`` on duplicates."""

        clean_term = _clean_term(term)
        clean_meaning = _clean_meaning(meaning)

        def persist(transaction: Transaction) -> tuple[PersonaSlang, bool]:
            existing = transaction.read(
                """
                SELECT id FROM persona_slang
                WHERE scope_group_id = ? AND term = ? AND status != 'rejected'
                """,
                (scope_group_id, clean_term),
            )
            if existing:
                row = transaction.read(
                    "SELECT * FROM persona_slang WHERE id = ?",
                    (int(existing[0]["id"]),),
                )[0]
                return _row(row), False
            result = transaction.write(
                """
                INSERT INTO persona_slang (
                    scope_group_id, created_by, term, meaning, status, confidence
                ) VALUES (?, ?, ?, ?, 'pending', 2)
                """,
                (scope_group_id, created_by, clean_term, clean_meaning),
            )
            inserted = int(result.lastrowid or 0)
            row = transaction.read("SELECT * FROM persona_slang WHERE id = ?", (inserted,))[0]
            return _row(row), True

        return await self.database.transaction(persist)

    async def pending(
        self, scope_group_id: int, *, created_by: int | None = None
    ) -> list[PersonaSlang]:
        clause, owner_params = _owner_clause(created_by)
        rows = await self.database.read(
            f"""
            SELECT * FROM persona_slang
            WHERE scope_group_id = ? AND status = 'pending'{clause}
            ORDER BY id DESC
            """,
            (scope_group_id, *owner_params),
        )
        return [_row(row) for row in rows]

    async def decide(
        self,
        entry_id: int,
        *,
        scope_group_id: int,
        status: str,
        created_by: int | None = None,
    ) -> PersonaSlang | None:
        """Confirm or reject one pending term, **inside the caller's scope**."""

        if status not in {"accepted", "rejected"}:
            raise ValueError("status must be accepted or rejected")
        clause, params = _scope_clause(scope_group_id, created_by)
        now = int(time.time())

        def persist(transaction: Transaction) -> PersonaSlang | None:
            rows = transaction.read(
                f"SELECT * FROM persona_slang WHERE id = ?{clause}", (entry_id, *params)
            )
            if not rows or str(rows[0]["status"]) != "pending":
                return None
            transaction.write(
                f"""
                UPDATE persona_slang
                SET status = ?, accepted_at = ?, updated_at = ?
                WHERE id = ?{clause}
                """,
                (status, now, now, entry_id, *params),
            )
            updated = transaction.read(
                f"SELECT * FROM persona_slang WHERE id = ?{clause}", (entry_id, *params)
            )[0]
            return _row(updated)

        return await self.database.transaction(persist)

    async def delete(
        self, entry_id: int, *, scope_group_id: int, created_by: int | None = None
    ) -> PersonaSlang | None:
        """Delete one term, **inside the caller's scope**."""

        clause, params = _scope_clause(scope_group_id, created_by)

        def persist(transaction: Transaction) -> PersonaSlang | None:
            rows = transaction.read(
                f"SELECT * FROM persona_slang WHERE id = ?{clause}", (entry_id, *params)
            )
            if not rows:
                return None
            transaction.write(
                f"DELETE FROM persona_slang WHERE id = ?{clause}", (entry_id, *params)
            )
            return _row(rows[0])

        return await self.database.transaction(persist)

    async def accepted_for_injection(
        self,
        scope_group_id: int,
        *,
        limit: int = 20,
        created_by: int | None = None,
    ) -> list[PersonaSlang]:
        """Accepted, still-enabled terms ranked by confidence then recency.

        Pass ``created_by`` for a person-scoped read; private scope is shared
        across users, so leaving it out here injects other people's private
        terms into this user's prompt.
        """

        clause, owner_params = _owner_clause(created_by)
        rows = await self.database.read(
            f"""
            SELECT * FROM persona_slang
            WHERE scope_group_id = ? AND status = 'accepted' AND enabled = 1{clause}
            ORDER BY confidence DESC, accepted_at DESC
            LIMIT ?
            """,
            (scope_group_id, *owner_params, max(1, min(int(limit), 50))),
        )
        return [_row(row) for row in rows]

    async def accepted_all(
        self, scope_group_id: int, *, limit: int = 50, created_by: int | None = None
    ) -> list[PersonaSlang]:
        """All accepted terms including disabled ones (management listing)."""

        clause, owner_params = _owner_clause(created_by)
        rows = await self.database.read(
            f"""
            SELECT * FROM persona_slang
            WHERE scope_group_id = ? AND status = 'accepted'{clause}
            ORDER BY confidence DESC, accepted_at DESC
            LIMIT ?
            """,
            (scope_group_id, *owner_params, max(1, min(int(limit), 50))),
        )
        return [_row(row) for row in rows]

    async def set_enabled(
        self,
        entry_id: int,
        *,
        enabled: bool,
        scope_group_id: int,
        created_by: int | None = None,
    ) -> PersonaSlang | None:
        """Flip one accepted term's user-facing enablement flag.

        Negative feedback ("别用XX") disables without destroying the record;
        re-enabling restores injection.  Only accepted rows are affected so
        the pending review queue keeps its own lifecycle.
        """

        clause, params = _scope_clause(scope_group_id, created_by)

        def persist(transaction: Transaction) -> PersonaSlang | None:
            rows = transaction.read(
                f"SELECT * FROM persona_slang WHERE id = ?{clause}", (entry_id, *params)
            )
            if not rows or str(rows[0]["status"]) != "accepted":
                return None
            transaction.write(
                f"""
                UPDATE persona_slang
                SET enabled = ?, updated_at = ?
                WHERE id = ? AND status = 'accepted'{clause}
                """,
                (1 if enabled else 0, int(time.time()), entry_id, *params),
            )
            updated = transaction.read(
                f"SELECT * FROM persona_slang WHERE id = ?{clause}", (entry_id, *params)
            )[0]
            return _row(updated)

        return await self.database.transaction(persist)

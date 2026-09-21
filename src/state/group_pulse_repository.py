"""SQLite repository for the opt-in group interjection ledger (schema v19).

One table keeps both the per-group opt-in switch (rows flip ``enabled``
between 0 and 1, the latest row per group wins) and the interjection audit
ledger (``reason`` + ``fingerprint`` rows).  The table never stores message
text — the privacy red line of the group-pulse feature is enforced here at
the persistence boundary.

Daily cap accounting uses local-day boundaries so a group cannot be pushed
more than ``TAFFY_GROUP_PULSE_DAILY_CAP`` times per calendar day.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from src.core.database import StateDatabase

from .repository import Repository

_MARKET_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

TriggerReason = Literal["willing", "cold_start"]

#: The table mixes two row kinds and ``reason`` is the ONLY discriminator: switch
#: rows hold the per-group opt-in state (``reason``/``fingerprint`` NULL, ``enabled``
#: 0/1), audit rows record one interjection (``reason`` set, ``enabled`` always 0).
#: Every reader must go through this predicate rather than re-deriving the check —
#: a reader that forgets it silently mixes the kinds (M-107 in
#: ``is_group_enabled``/``enabled_groups``, M-109 in ``recent``/``count``).
SWITCH_ROW_PREDICATE = "reason IS NULL"
AUDIT_ROW_PREDICATE = "reason IS NOT NULL"


def row_kind(reason: str | None) -> str:
    """Classify one row by its ``reason`` — the single source of truth for kind.

    Reader DTOs expose this result instead of letting callers re-test
    ``reason is None``; that hand-written test is the shared root cause of M-107
    and M-109 (the judgement scattered across callers).
    """

    return "switch" if reason is None else "interjection"


def _local_day_start(current: int) -> int:
    local = datetime.fromtimestamp(current, _MARKET_TIMEZONE)
    midnight = datetime(local.year, local.month, local.day, tzinfo=_MARKET_TIMEZONE)
    return int(midnight.timestamp())


@dataclass(frozen=True, slots=True)
class GroupPulseLogEntry:
    id: int
    group_id: int
    enabled: int
    reason: TriggerReason | None
    fingerprint: str | None
    created_at: int
    #: ``switch``（开关行）或 ``interjection``（审计行），取自 ``row_kind``。
    kind: str


class GroupPulseLogRepository(Repository):
    """Persist opt-in flips and interjection audits, never message content."""

    def __init__(self, database: StateDatabase) -> None:
        super().__init__(database)

    async def set_group_enabled(self, group_id: int, enabled: bool) -> None:
        """Record an opt-in flip; the latest row per group is the source of truth."""

        await self.database.write(
            """
            INSERT INTO group_pulse_log (group_id, enabled, reason, fingerprint, created_at)
            VALUES (?, ?, NULL, NULL, ?)
            """,
            (int(group_id), 1 if enabled else 0, int(time.time())),
        )

    async def is_group_enabled(self, group_id: int) -> bool:
        """Whether the group's newest **switch** row says opted in.

        The table mixes two row kinds: switch rows (``reason IS NULL``, the
        ``enabled`` column flips 0/1) and interjection audit rows (``reason``
        set, ``enabled`` always 0).  "Newest row wins" therefore only applies
        to switch rows — without the filter, one interjection would make the
        newest row an ``enabled=0`` audit row and silently unsubscribe the
        group forever.
        """

        rows = await self.database.read(
            f"""
            SELECT enabled FROM group_pulse_log
            WHERE group_id = ? AND {SWITCH_ROW_PREDICATE}
            ORDER BY id DESC LIMIT 1
            """,
            (int(group_id),),
        )
        return bool(rows and int(rows[0]["enabled"]) == 1)

    async def record(
        self,
        group_id: int,
        reason: TriggerReason,
        fingerprint: str,
        *,
        at: int | None = None,
    ) -> None:
        """Append one interjection audit row (never the message itself)."""

        await self.database.write(
            """
            INSERT INTO group_pulse_log (group_id, enabled, reason, fingerprint, created_at)
            VALUES (?, 0, ?, ?, ?)
            """,
            (int(group_id), reason, fingerprint[:64], int(time.time() if at is None else at)),
        )

    async def count_for_group(self, group_id: int, day_key: str, now: int) -> int:
        """Interjections for one group since local midnight (daily cap)."""

        rows = await self.database.read(
            f"""
            SELECT COUNT(*) AS n FROM group_pulse_log
            WHERE group_id = ? AND {AUDIT_ROW_PREDICATE} AND created_at >= ?
            """,
            (int(group_id), _local_day_start(now)),
        )
        return int(rows[0]["n"]) if rows else 0

    async def recent(
        self, group_id: int | None = None, limit: int = 50
    ) -> list[GroupPulseLogEntry]:
        """Activity rows for the panel, newest first — **both kinds**.

        Not an "audit ledger" (the previous wording): the rows include opt-in
        switch rows as well as interjection audit rows, each tagged with
        ``kind``.  Consumers that need only interjections must filter on
        :data:`AUDIT_ROW_PREDICATE`, not on a re-derived ``reason is None`` test.
        """

        if group_id is None:
            rows = await self.database.read(
                """
                SELECT id, group_id, enabled, reason, fingerprint, created_at
                FROM group_pulse_log
                ORDER BY id DESC LIMIT ?
                """,
                (int(limit),),
            )
        else:
            rows = await self.database.read(
                """
                SELECT id, group_id, enabled, reason, fingerprint, created_at
                FROM group_pulse_log
                WHERE group_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (int(group_id), int(limit)),
            )
        return [
            GroupPulseLogEntry(
                id=int(row["id"]),
                group_id=int(row["group_id"]),
                enabled=int(row["enabled"]),
                reason=row["reason"],
                fingerprint=row["fingerprint"],
                created_at=int(row["created_at"]),
                kind=row_kind(row["reason"]),
            )
            for row in rows
        ]

    async def counts(self, *, group_id: int | None = None) -> dict[str, int]:
        """Row counts split by kind, via the shared predicates.

        The panel used to report ``len(recent(...))`` as the "interjection audit
        ledger" count, which also counted every opt-in switch row (M-109).  Both
        numbers are returned so no caller has to guess which one it has.
        """

        where = "" if group_id is None else "WHERE group_id = ?"
        parameters = () if group_id is None else (int(group_id),)
        rows = await self.database.read(
            f"""
            SELECT
                SUM(CASE WHEN {AUDIT_ROW_PREDICATE} THEN 1 ELSE 0 END) AS interjections,
                SUM(CASE WHEN {SWITCH_ROW_PREDICATE} THEN 1 ELSE 0 END) AS switches
            FROM group_pulse_log
            {where}
            """,
            parameters,
        )
        row = rows[0] if rows else {}
        return {
            "interjection_count": int(row["interjections"] or 0),
            "switch_count": int(row["switches"] or 0),
        }

    async def enabled_groups(self) -> list[int]:
        """Groups currently opted in (newest **switch** row per group wins).

        Same row-kind filter as :meth:`is_group_enabled`: interjection audit
        rows carry ``enabled = 0`` and must not be mistaken for a switch.
        """

        rows = await self.database.read(
            f"""
            SELECT group_id FROM group_pulse_log
            WHERE {SWITCH_ROW_PREDICATE}
              AND enabled = 1
              AND id = (SELECT MAX(id) FROM group_pulse_log AS g2
                        WHERE g2.group_id = group_pulse_log.group_id
                          AND {SWITCH_ROW_PREDICATE})
            ORDER BY group_id
            """
        )
        return [int(row["group_id"]) for row in rows]


__all__ = [
    "AUDIT_ROW_PREDICATE",
    "SWITCH_ROW_PREDICATE",
    "GroupPulseLogEntry",
    "GroupPulseLogRepository",
    "TriggerReason",
    "_local_day_start",
    "row_kind",
]

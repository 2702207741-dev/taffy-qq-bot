"""SQLite repository for opt-in proactive deliveries.

One table keeps both the per-user morning opt-in switch (``morning_optin``
rows flipped between ``on``/``off``) and the deduplicated delivery ledger
(``morning``/``dip`` rows keyed by day, or day plus item).  The unique
(user, kind, dedupe_key) constraint makes every send idempotent across
worker restarts and keeps the ledger auditable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from src.core.database import Transaction

from .repository import Filters, Repository, positive_int

DeliveryKind = Literal["morning_optin", "morning", "dip"]
DeliveryStatus = Literal["on", "off", "delivered", "skipped", "failed"]

_KINDS = {"morning_optin", "morning", "dip"}
_STATUSES = {"on", "off", "delivered", "skipped", "failed"}

_COLUMNS = "id, user_id, kind, dedupe_key, status, detail, created_at, updated_at"


class ProactiveDeliveryError(RuntimeError):
    """Base class for proactive-delivery persistence failures."""


@dataclass(frozen=True, slots=True)
class ProactiveDelivery:
    id: int
    user_id: int
    kind: DeliveryKind
    dedupe_key: str
    status: DeliveryStatus
    detail: str | None
    created_at: int
    updated_at: int


def _kind(value: object) -> DeliveryKind:
    if value not in _KINDS:
        raise ValueError("kind must be morning_optin, morning or dip")
    return value  # type: ignore[return-value]


def _status(value: object) -> DeliveryStatus:
    if value not in _STATUSES:
        raise ValueError("proactive delivery status is invalid")
    return value  # type: ignore[return-value]


def _dedupe_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("dedupe_key must be a string")
    key = value.strip()
    if not 1 <= len(key) <= 160:
        raise ValueError("dedupe_key must contain 1 to 160 characters")
    return key


def _detail(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("detail must be a string or null")
    detail = value.strip()
    if not detail or len(detail) > 400:
        raise ValueError("detail must contain 1 to 400 characters")
    return detail


def _row_to_delivery(row: dict[str, Any]) -> ProactiveDelivery:
    return ProactiveDelivery(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kind=_kind(row["kind"]),
        dedupe_key=str(row["dedupe_key"]),
        status=_status(row["status"]),
        detail=str(row["detail"]) if row["detail"] is not None else None,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


class ProactiveDeliveryRepository(Repository):
    """Opt-in switches plus an idempotent, auditable delivery ledger."""

    async def set_optin(
        self,
        user_id: int,
        enabled: bool,
        *,
        now: int | None = None,
    ) -> None:
        """Flip one user's morning push switch (idempotent upsert)."""

        user = positive_int(user_id, name="user_id")
        timestamp = int(time.time() if now is None else now)

        def upsert(transaction: Transaction) -> None:
            transaction.write(
                """
                INSERT INTO proactive_deliveries
                    (user_id, kind, dedupe_key, status, created_at, updated_at)
                VALUES (?, 'morning_optin', 'morning', ?, ?, ?)
                ON CONFLICT(user_id, kind, dedupe_key) DO UPDATE SET
                    status = excluded.status, updated_at = excluded.updated_at
                """,
                (user, "on" if enabled else "off", timestamp, timestamp),
            )

        await self.database.transaction(upsert)

    async def is_optedin(self, user_id: int) -> bool:
        user = positive_int(user_id, name="user_id")
        rows = await self.database.read(
            """
            SELECT 1 AS present FROM proactive_deliveries
            WHERE user_id = ? AND kind = 'morning_optin' AND status = 'on'
            """,
            (user,),
        )
        return bool(rows)

    async def list_optedin(self) -> tuple[int, ...]:
        rows = await self.database.read(
            """
            SELECT user_id FROM proactive_deliveries
            WHERE kind = 'morning_optin' AND status = 'on'
            ORDER BY user_id
            """
        )
        return tuple(int(row["user_id"]) for row in rows)

    async def has_delivery(
        self,
        user_id: int,
        kind: str,
        dedupe_key: str,
    ) -> bool:
        """Whether this exact push was already delivered.

        Only ``delivered`` rows occupy the dedupe slot: a transient network
        failure recorded as ``failed`` (or a conditional ``skipped``) must not
        silently suppress the whole day's push.
        """

        user = positive_int(user_id, name="user_id")
        clean_kind = _kind(kind)
        clean_key = _dedupe_key(dedupe_key)
        rows = await self.database.read(
            """
            SELECT 1 AS present FROM proactive_deliveries
            WHERE user_id = ? AND kind = ? AND dedupe_key = ?
              AND status = 'delivered'
            """,
            (user, clean_kind, clean_key),
        )
        return bool(rows)

    async def count_delivered_since(self, user_id: int, *, since: int) -> int:
        """Count this user's delivered pushes (morning + dip) since ``since``."""

        user = positive_int(user_id, name="user_id")
        rows = await self.database.read(
            """
            SELECT COUNT(*) AS total FROM proactive_deliveries
            WHERE user_id = ? AND kind IN ('morning', 'dip')
              AND status = 'delivered' AND created_at >= ?
            """,
            (user, int(since)),
        )
        return int(rows[0]["total"]) if rows else 0

    async def record(
        self,
        user_id: int,
        kind: str,
        dedupe_key: str,
        status: str,
        *,
        detail: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Append one delivery outcome; ``False`` when the row already exists.

        A ``delivered`` row is final and never rewritten.  Because only
        ``delivered`` rows occupy the dedupe slot (see :meth:`has_delivery`),
        a later successful retry must be able to upgrade an earlier
        ``failed``/``skipped`` row instead of being silently dropped.
        """

        user = positive_int(user_id, name="user_id")
        clean_kind = _kind(kind)
        if clean_kind == "morning_optin":
            raise ValueError("use set_optin for the morning switch")
        clean_key = _dedupe_key(dedupe_key)
        clean_status = _status(status)
        clean_detail = _detail(detail)
        timestamp = int(time.time() if now is None else now)

        def insert(transaction: Transaction) -> bool:
            duplicate = transaction.read(
                """
                SELECT status FROM proactive_deliveries
                WHERE user_id = ? AND kind = ? AND dedupe_key = ?
                """,
                (user, clean_kind, clean_key),
            )
            if duplicate:
                if clean_status == "delivered" and str(duplicate[0]["status"]) != "delivered":
                    upgraded = transaction.write(
                        """
                        UPDATE proactive_deliveries
                        SET status = 'delivered', detail = ?, updated_at = ?
                        WHERE user_id = ? AND kind = ? AND dedupe_key = ?
                          AND status != 'delivered'
                        """,
                        (clean_detail, timestamp, user, clean_kind, clean_key),
                    )
                    return upgraded.rowcount == 1
                return False
            transaction.write(
                """
                INSERT INTO proactive_deliveries
                    (user_id, kind, dedupe_key, status, detail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user,
                    clean_kind,
                    clean_key,
                    clean_status,
                    clean_detail,
                    timestamp,
                    timestamp,
                ),
            )
            return True

        return await self.database.transaction(insert)

    async def admin_list(
        self,
        *,
        user_id: int | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[tuple[ProactiveDelivery, ...], int]:
        filters = Filters()
        if user_id is not None:
            filters.equals("user_id", positive_int(user_id, name="user_id"))
        if kind is not None:
            filters.equals("kind", _kind(kind))
        bounded = [
            max(1, min(int(limit), 1_000)),
            max(0, int(offset)),
        ]

        def page(transaction: Transaction) -> tuple[tuple[ProactiveDelivery, ...], int]:
            # Page and total must come from one consistent snapshot.
            rows = transaction.read(
                f"""
                SELECT {_COLUMNS} FROM proactive_deliveries
                {filters.clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*filters.parameters, *bounded),
            )
            total_rows = transaction.read(
                f"SELECT COUNT(*) AS total FROM proactive_deliveries {filters.clause}",
                tuple(filters.parameters),
            )
            return (
                tuple(_row_to_delivery(row) for row in rows),
                int(total_rows[0]["total"]) if total_rows else 0,
            )

        return await self.database.transaction(page, immediate=False)

    async def stats(self) -> dict[str, int]:
        rows = await self.database.read(
            """
            SELECT kind, status, COUNT(*) AS total FROM proactive_deliveries
            GROUP BY kind, status
            """
        )
        counts = {(str(row["kind"]), str(row["status"])): int(row["total"]) for row in rows}
        return {
            f"{kind}:{status}": counts.get((kind, status), 0)
            for kind in ("morning_optin", "morning", "dip")
            for status in ("on", "off", "delivered", "skipped", "failed")
        }


__all__ = [
    "ProactiveDelivery",
    "ProactiveDeliveryError",
    "ProactiveDeliveryRepository",
]

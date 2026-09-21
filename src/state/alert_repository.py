"""Transactional SQLite repository for price alerts.

The repository owns persistence and lease state only.  Price lookups and QQ
network calls deliberately stay in the plugin/service layer so a lease is
committed only after the caller knows the real outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import Any, Literal, cast

from src.core.database import PROJECT_ROOT, StateDatabase, Transaction

logger = logging.getLogger("taffy.alert_repository")

MAX_ALERTS_PER_OWNER = 20
MAX_MARKET_NAME_LENGTH = 100
MAX_ERROR_LENGTH = 1_000
MAX_MESSAGE_ID_LENGTH = 200
MAX_SQLITE_INTEGER = 2**63 - 1
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_ADMIN_PAGE_SIZE = 50
MAX_ADMIN_PAGE_SIZE = 200
LEGACY_SOURCE_NAME = "legacy:alerts.json"
DEFAULT_LEGACY_PATH = PROJECT_ROOT / "alerts.json"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

AlertStatus = Literal["pending", "claimed", "delivered", "failed"]
AlertDirection = Literal["above", "below"]


class AlertRepositoryError(RuntimeError):
    """Base class for alert persistence failures."""


class LegacyAlertMigrationError(AlertRepositoryError):
    """Legacy JSON cannot be imported without risking data loss."""

    def __init__(self, message: str, *, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """Database representation of one alert.

    ``lease_token`` is present only while ``status`` is ``claimed``.  Callers
    must present that exact token when completing or releasing the claim.
    """

    id: str
    owner_user_id: int
    group_id: int | None
    market_name: str
    target_price_cents: int
    direction: AlertDirection
    status: AlertStatus
    attempts: int
    evaluation_failures: int
    next_retry_at: int | None
    lease_token: str | None
    lease_expires_at: int | None
    last_error: str | None
    last_evaluation_error: str | None
    last_evaluated_at: int | None
    last_observed_price_cents: int | None
    created_at: int
    updated_at: int
    delivered_at: int | None
    failed_at: int | None
    message_id: str | None


@dataclass(frozen=True, slots=True)
class CreateAlertResult:
    created: bool
    alert: AlertRecord | None
    quota_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class AlertMutationResult:
    applied: bool
    alert: AlertRecord | None


@dataclass(frozen=True, slots=True)
class AdminAlertPage:
    items: tuple[AlertRecord, ...]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class LegacyMigrationResult:
    state: Literal["absent", "imported", "already_imported"]
    imported_rows: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class _LegacyPayload:
    source_sha256: str
    rows: tuple[dict[str, object], ...]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> object:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def _strict_positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError(f"{field} must be a positive integer")
    if parsed <= 0 or parsed > MAX_SQLITE_INTEGER:
        raise ValueError(f"{field} is outside the supported range")
    return parsed


def _optional_group_id(value: object) -> int | None:
    if value is None or value == "":
        return None
    return _strict_positive_integer(value, "group_id")


def _market_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("market_name must be text")
    value = value.strip()
    if not value or len(value) > MAX_MARKET_NAME_LENGTH:
        raise ValueError(f"market_name must contain 1 to {MAX_MARKET_NAME_LENGTH} characters")
    return value


def _direction(value: object) -> AlertDirection:
    if value not in {"above", "below"}:
        raise ValueError("direction must be 'above' or 'below'")
    return value


def price_to_cents(value: Decimal | int | float | str) -> int:
    """Convert a currency amount to integer cents using financial half-up rounding."""

    if isinstance(value, bool):
        raise ValueError("price must be a finite positive decimal")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value <= 0:
            raise ValueError("price must be a finite positive decimal")
        cents = int(
            (decimal_value * Decimal(100)).quantize(
                Decimal(1),
                rounding=ROUND_HALF_UP,
            )
        )
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError("price must be a finite positive decimal") from exc
    if cents <= 0 or cents > MAX_SQLITE_INTEGER:
        raise ValueError("price is outside the supported cent range")
    return cents


def _positive_cents(value: object, field: str) -> int:
    return _strict_positive_integer(value, field)


def _epoch(value: datetime | int | None = None) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, bool):
        raise ValueError("timestamp must be a non-negative integer or aware datetime")
    if isinstance(value, int):
        result = value
    elif isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime timestamps must include a timezone")
        result = math.floor(value.timestamp())
    else:
        raise ValueError("timestamp must be a non-negative integer or aware datetime")
    if result < 0 or result > MAX_SQLITE_INTEGER:
        raise ValueError("timestamp is outside the supported range")
    return result


def _legacy_epoch(value: object, field: str, *, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    if isinstance(value, int):
        return _epoch(value)
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _epoch(parsed)


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError(f"{field} must be a non-negative integer")
    if parsed < 0 or parsed > MAX_SQLITE_INTEGER:
        raise ValueError(f"{field} is outside the supported range")
    return parsed


def _row_to_alert(row: dict[str, Any]) -> AlertRecord:
    raw_status = str(row["status"])
    if raw_status not in {"pending", "claimed", "delivered", "failed"}:
        raise AlertRepositoryError(f"unsupported alert status in database: {raw_status!r}")
    status = cast(AlertStatus, raw_status)
    return AlertRecord(
        id=str(row["id"]),
        owner_user_id=int(row["owner_user_id"]),
        group_id=int(row["group_id"]) if row["group_id"] is not None else None,
        market_name=str(row["market_name"]),
        target_price_cents=int(row["target_price_cents"]),
        direction=_direction(row["direction"]),
        status=status,
        attempts=int(row["attempts"]),
        evaluation_failures=int(row["evaluation_failures"]),
        next_retry_at=(int(row["next_retry_at"]) if row["next_retry_at"] is not None else None),
        lease_token=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            int(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        last_evaluation_error=(
            str(row["last_evaluation_error"]) if row["last_evaluation_error"] is not None else None
        ),
        last_evaluated_at=(
            int(row["last_evaluated_at"]) if row["last_evaluated_at"] is not None else None
        ),
        last_observed_price_cents=(
            int(row["last_observed_price_cents"])
            if row["last_observed_price_cents"] is not None
            else None
        ),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        delivered_at=int(row["delivered_at"]) if row["delivered_at"] is not None else None,
        failed_at=int(row["failed_at"]) if row["failed_at"] is not None else None,
        message_id=str(row["message_id"]) if row["message_id"] is not None else None,
    )


class AlertRepository:
    """Async, multi-process-safe alert persistence backed by :class:`StateDatabase`."""

    #: After this many consecutive evaluation failures an alert is dead-lettered
    #: instead of ping-ponging back into the pending queue forever.
    max_evaluation_failures = 100

    def __init__(
        self,
        database: StateDatabase,
        *,
        legacy_path: str | os.PathLike[str] | None = DEFAULT_LEGACY_PATH,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.database = database
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer between 1 and 100")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be an integer between 1 and 100")
        self.max_attempts = max_attempts

    async def initialize(self) -> LegacyMigrationResult | None:
        """Initialize SQLite and strictly import legacy ``alerts.json`` once."""

        await self.database.initialize()
        if self.legacy_path is None:
            return None
        return await self._migrate_legacy()

    async def create(
        self,
        owner_user_id: int | str,
        market_name: str,
        target_price: Decimal | int | float | str,
        *,
        direction: AlertDirection = "above",
        group_id: int | str | None = None,
        now: datetime | int | None = None,
    ) -> CreateAlertResult:
        owner = _strict_positive_integer(owner_user_id, "owner_user_id")
        group = _optional_group_id(group_id)
        name = _market_name(market_name)
        target_cents = price_to_cents(target_price)
        alert_direction = _direction(direction)
        timestamp = _epoch(now)
        alert_id = uuid.uuid4().hex

        def create_alert(transaction: Transaction) -> CreateAlertResult:
            count = int(
                transaction.read(
                    """
                    SELECT COUNT(*) AS count FROM alerts
                    WHERE owner_user_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (owner,),
                )[0]["count"]
            )
            if count >= MAX_ALERTS_PER_OWNER:
                return CreateAlertResult(created=False, alert=None, quota_exceeded=True)
            transaction.write(
                """
                INSERT INTO alerts
                    (id, owner_user_id, group_id, market_name, target_price_cents,
                     direction, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    owner,
                    group,
                    name,
                    target_cents,
                    alert_direction,
                    timestamp,
                    timestamp,
                ),
            )
            row = transaction.read("SELECT * FROM alerts WHERE id = ?", (alert_id,))[0]
            return CreateAlertResult(created=True, alert=_row_to_alert(row))

        return await self.database.transaction(create_alert)

    async def list_owner(self, owner_user_id: int | str) -> tuple[AlertRecord, ...]:
        owner = _strict_positive_integer(owner_user_id, "owner_user_id")
        rows = await self.database.read(
            """
            SELECT * FROM alerts
            WHERE owner_user_id = ?
            ORDER BY created_at, id
            """,
            (owner,),
        )
        return tuple(_row_to_alert(row) for row in rows)

    async def cancel_at(
        self,
        owner_user_id: int | str,
        index: int,
    ) -> AlertMutationResult:
        """Delete the zero-based item in only the requesting owner's view."""

        owner = _strict_positive_integer(owner_user_id, "owner_user_id")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("index must be a non-negative integer")

        def cancel(transaction: Transaction) -> AlertMutationResult:
            rows = transaction.read(
                """
                SELECT * FROM alerts
                WHERE owner_user_id = ?
                ORDER BY created_at, id
                LIMIT 1 OFFSET ?
                """,
                (owner, index),
            )
            if not rows:
                return AlertMutationResult(applied=False, alert=None)
            alert = _row_to_alert(rows[0])
            result = transaction.write(
                "DELETE FROM alerts WHERE id = ? AND owner_user_id = ?",
                (alert.id, owner),
            )
            return AlertMutationResult(applied=result.rowcount == 1, alert=alert)

        return await self.database.transaction(cancel)

    async def claim_due(
        self,
        limit: int,
        lease_seconds: int,
        *,
        now: datetime | int | None = None,
    ) -> tuple[AlertRecord, ...]:
        """Atomically claim due pending alerts and recover expired claims."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        timestamp = _epoch(now)
        if lease_seconds > MAX_SQLITE_INTEGER - timestamp:
            raise ValueError("lease expiry is outside the supported range")
        expires_at = timestamp + lease_seconds

        def claim(transaction: Transaction) -> tuple[AlertRecord, ...]:
            # Fresh pending work gets the full budget first; expired-lease
            # recoveries may only fill leftover slots so a large recovery
            # backlog cannot starve never-checked alerts.
            pending = transaction.read(
                """
                SELECT id, status, attempts FROM alerts
                WHERE status = 'pending'
                    AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY COALESCE(next_retry_at, created_at), created_at, id
                LIMIT ?
                """,
                (timestamp, limit),
            )
            remaining = max(limit - len(pending), 0)
            recoveries: list[dict[str, Any]] = []
            if remaining > 0:
                recoveries = transaction.read(
                    """
                    SELECT id, status, attempts FROM alerts
                    WHERE status = 'claimed' AND lease_expires_at <= ?
                    ORDER BY lease_expires_at, created_at, id
                    LIMIT ?
                    """,
                    (timestamp, remaining),
                )
            claimed_ids: list[str] = []
            for candidate in (*pending, *recoveries):
                alert_id = str(candidate["id"])
                token = uuid.uuid4().hex
                if candidate["status"] == "claimed":
                    update = transaction.write(
                        """
                        UPDATE alerts
                        SET evaluation_failures = evaluation_failures + 1,
                            lease_owner = ?, lease_expires_at = ?,
                            last_evaluation_error = 'previous claim lease expired',
                            last_evaluated_at = ?, updated_at = ?
                        WHERE id = ? AND status = 'claimed'
                          AND lease_expires_at <= ?
                        """,
                        (token, expires_at, timestamp, timestamp, alert_id, timestamp),
                    )
                else:
                    update = transaction.write(
                        """
                        UPDATE alerts
                        SET status = 'claimed', lease_owner = ?, lease_expires_at = ?,
                            updated_at = ?
                        WHERE id = ? AND status = 'pending'
                          AND (next_retry_at IS NULL OR next_retry_at <= ?)
                        """,
                        (token, expires_at, timestamp, alert_id, timestamp),
                    )
                if update.rowcount != 1:
                    continue
                claimed_ids.append(alert_id)
            # One consolidated read-back instead of a SELECT per claim.
            if not claimed_ids:
                return ()
            placeholders = ",".join("?" for _ in claimed_ids)
            rows = transaction.read(
                f"SELECT * FROM alerts WHERE id IN ({placeholders})",  # noqa: S608
                tuple(claimed_ids),
            )
            rows_by_id = {str(row["id"]): row for row in rows}
            return tuple(
                _row_to_alert(rows_by_id[alert_id])
                for alert_id in claimed_ids
                if alert_id in rows_by_id
            )

        return await self.database.transaction(claim)

    async def defer_unmet_target(
        self,
        alert_id: str,
        lease_token: str,
        observed_price_cents: int,
        *,
        next_check_at: datetime | int | None = None,
        now: datetime | int | None = None,
    ) -> AlertMutationResult:
        """Release a claimed alert after observing that its directional target is unmet."""

        identity, token = self._claim_identity(alert_id, lease_token)
        observed = _positive_cents(observed_price_cents, "observed_price_cents")
        timestamp = _epoch(now)
        retry_at = _epoch(next_check_at) if next_check_at is not None else None
        return await self._claimed_update(
            identity,
            token,
            """
            UPDATE alerts
            SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = ?, last_error = NULL,
                evaluation_failures = 0, last_evaluation_error = NULL,
                last_evaluated_at = ?,
                last_observed_price_cents = ?, updated_at = ?
            WHERE id = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (retry_at, timestamp, observed, timestamp, identity, token),
        )

    async def record_evaluation_failure(
        self,
        alert_id: str,
        lease_token: str,
        error: BaseException | str,
        next_retry_at: datetime | int,
        *,
        now: datetime | int | None = None,
    ) -> AlertMutationResult:
        """Defer transient market-data failure without consuming delivery attempts.

        Consecutive evaluation failures are bounded: once the counter reaches
        :attr:`max_evaluation_failures` the alert is dead-lettered (``failed``)
        instead of being re-queued forever; ``retry_failed`` is the manual
        recovery path back to the pending queue.
        """

        identity, token = self._claim_identity(alert_id, lease_token)
        timestamp = _epoch(now)
        retry_at = _epoch(next_retry_at)
        if isinstance(error, BaseException):
            error_text = f"{type(error).__name__}: {error}"
        else:
            error_text = str(error)
        error_text = error_text[:MAX_ERROR_LENGTH]
        return await self._claimed_update(
            identity,
            token,
            """
            UPDATE alerts
            SET status = CASE
                    WHEN evaluation_failures + 1 >= ? THEN 'failed'
                    ELSE 'pending'
                END,
                lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = CASE
                    WHEN evaluation_failures + 1 >= ? THEN NULL
                    ELSE ?
                END,
                evaluation_failures = evaluation_failures + 1,
                last_evaluation_error = ?, last_evaluated_at = ?,
                last_error = NULL,
                failed_at = CASE WHEN evaluation_failures + 1 >= ? THEN ? ELSE NULL END,
                updated_at = ?
            WHERE id = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (
                self.max_evaluation_failures,
                self.max_evaluation_failures,
                retry_at,
                error_text,
                timestamp,
                self.max_evaluation_failures,
                timestamp,
                timestamp,
                identity,
                token,
            ),
        )

    async def defer_below_target(
        self,
        alert_id: str,
        lease_token: str,
        observed_price_cents: int,
        *,
        next_check_at: datetime | int | None = None,
        now: datetime | int | None = None,
    ) -> AlertMutationResult:
        """Compatibility alias for callers created before directional alerts."""

        return await self.defer_unmet_target(
            alert_id,
            lease_token,
            observed_price_cents,
            next_check_at=next_check_at,
            now=now,
        )

    async def record_failure(
        self,
        alert_id: str,
        lease_token: str,
        error: BaseException | str,
        next_retry_at: datetime | int,
        *,
        now: datetime | int | None = None,
    ) -> AlertMutationResult:
        """Release a claim after failure or move it to the dead-letter state."""

        identity, token = self._claim_identity(alert_id, lease_token)
        timestamp = _epoch(now)
        retry_at = _epoch(next_retry_at)
        if isinstance(error, BaseException):
            error_text = f"{type(error).__name__}: {error}"
        else:
            error_text = str(error)
        error_text = error_text[:MAX_ERROR_LENGTH]
        return await self._claimed_update(
            identity,
            token,
            """
            UPDATE alerts
            SET status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END,
                attempts = attempts + 1,
                next_retry_at = CASE WHEN attempts + 1 >= ? THEN NULL ELSE ? END,
                lease_owner = NULL, lease_expires_at = NULL,
                last_error = ?,
                failed_at = CASE WHEN attempts + 1 >= ? THEN ? ELSE NULL END,
                updated_at = ?
            WHERE id = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (
                self.max_attempts,
                self.max_attempts,
                retry_at,
                error_text,
                self.max_attempts,
                timestamp,
                timestamp,
                identity,
                token,
            ),
        )

    async def mark_delivered(
        self,
        alert_id: str,
        lease_token: str,
        *,
        observed_price_cents: int | None = None,
        message_id: str | int | None = None,
        delivered_at: datetime | int | None = None,
    ) -> AlertMutationResult:
        """Commit successful delivery only for the current claim token."""

        identity, token = self._claim_identity(alert_id, lease_token)
        timestamp = _epoch(delivered_at)
        observed = (
            _positive_cents(observed_price_cents, "observed_price_cents")
            if observed_price_cents is not None
            else None
        )
        stored_message_id = str(message_id) if message_id is not None else None
        if stored_message_id is not None and len(stored_message_id) > MAX_MESSAGE_ID_LENGTH:
            raise ValueError(f"message_id cannot exceed {MAX_MESSAGE_ID_LENGTH} characters")
        return await self._claimed_update(
            identity,
            token,
            """
            UPDATE alerts
            SET status = 'delivered', lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = NULL, last_error = NULL,
                evaluation_failures = 0, last_evaluation_error = NULL,
                last_observed_price_cents = COALESCE(?, last_observed_price_cents),
                delivered_at = ?, failed_at = NULL, message_id = ?, updated_at = ?
            WHERE id = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (observed, timestamp, stored_message_id, timestamp, identity, token),
        )

    async def admin_delete(self, alert_id: str) -> AlertRecord | None:
        """Physically delete one alert by id (administration); ``None`` if absent."""

        identity = str(alert_id).strip()
        if not 1 <= len(identity) <= 128:
            raise ValueError("alert_id must contain 1 to 128 characters")

        def delete(transaction: Transaction) -> AlertRecord | None:
            rows = transaction.read("SELECT * FROM alerts WHERE id = ?", (identity,))
            if not rows:
                return None
            alert = _row_to_alert(rows[0])
            transaction.write("DELETE FROM alerts WHERE id = ?", (identity,))
            return alert

        return await self.database.transaction(delete)

    async def admin_page(
        self,
        *,
        offset: int = 0,
        limit: int = DEFAULT_ADMIN_PAGE_SIZE,
        owner_user_id: int | str | None = None,
        status: AlertStatus | None = None,
    ) -> AdminAlertPage:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_ADMIN_PAGE_SIZE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_ADMIN_PAGE_SIZE}")
        clauses: list[str] = []
        parameters: list[object] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            parameters.append(_strict_positive_integer(owner_user_id, "owner_user_id"))
        if status is not None:
            if status not in {"pending", "claimed", "delivered", "failed"}:
                raise ValueError("status is invalid")
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        def page(transaction: Transaction) -> AdminAlertPage:
            total = int(
                transaction.read(
                    f"SELECT COUNT(*) AS count FROM alerts{where}",  # noqa: S608
                    tuple(parameters),
                )[0]["count"]
            )
            rows = transaction.read(
                f"""
                SELECT * FROM alerts{where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                (*parameters, limit, offset),
            )
            return AdminAlertPage(
                items=tuple(_row_to_alert(row) for row in rows),
                total=total,
                offset=offset,
                limit=limit,
            )

        return await self.database.transaction(page, immediate=False)

    async def retry_failed(
        self,
        alert_id: str,
        *,
        owner_user_id: int | str | None = None,
        now: datetime | int | None = None,
    ) -> AlertMutationResult:
        """Move one dead-lettered alert back to the pending queue."""

        identity = str(alert_id).strip()
        if not 1 <= len(identity) <= 128:
            raise ValueError("alert_id must contain 1 to 128 characters")
        owner = (
            _strict_positive_integer(owner_user_id, "owner_user_id")
            if owner_user_id is not None
            else None
        )
        timestamp = _epoch(now)

        def retry(transaction: Transaction) -> AlertMutationResult:
            owner_clause = " AND owner_user_id = ?" if owner is not None else ""
            parameters: tuple[object, ...] = (
                (timestamp, timestamp, identity, owner)
                if owner is not None
                else (timestamp, timestamp, identity)
            )
            result = transaction.write(
                f"""
                UPDATE alerts
                SET status = 'pending', attempts = 0, next_retry_at = ?,
                    evaluation_failures = 0, last_evaluation_error = NULL,
                    failed_at = NULL, last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'failed'{owner_clause}
                """,  # noqa: S608 - owner_clause is a fixed internal fragment
                parameters,
            )
            rows = transaction.read("SELECT * FROM alerts WHERE id = ?", (identity,))
            return AlertMutationResult(
                applied=result.rowcount == 1,
                alert=_row_to_alert(rows[0]) if rows else None,
            )

        return await self.database.transaction(retry)

    async def cleanup_failed(
        self,
        *,
        failed_before: datetime | int,
        limit: int = 1_000,
    ) -> int:
        """Delete a bounded batch of expired alert dead letters."""

        timestamp = _epoch(failed_before)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await self.database.write(
            """
            DELETE FROM alerts
            WHERE id IN (
                SELECT id FROM alerts
                WHERE status = 'failed' AND failed_at < ?
                ORDER BY failed_at, id
                LIMIT ?
            )
            """,
            (timestamp, limit),
        )
        return max(0, result.rowcount)

    async def _claimed_update(
        self,
        alert_id: str,
        lease_token: str,
        sql: str,
        parameters: Sequence[object],
    ) -> AlertMutationResult:
        def update(transaction: Transaction) -> AlertMutationResult:
            result = transaction.write(sql, parameters)
            if result.rowcount != 1:
                return AlertMutationResult(applied=False, alert=None)
            row = transaction.read("SELECT * FROM alerts WHERE id = ?", (alert_id,))[0]
            return AlertMutationResult(applied=True, alert=_row_to_alert(row))

        return await self.database.transaction(update)

    @staticmethod
    def _claim_identity(alert_id: str, lease_token: str) -> tuple[str, str]:
        if not isinstance(alert_id, str) or not alert_id or len(alert_id) > 128:
            raise ValueError("alert_id is invalid")
        if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 200:
            raise ValueError("lease_token is invalid")
        return alert_id, lease_token

    async def _migrate_legacy(self) -> LegacyMigrationResult:
        legacy_path = self.legacy_path
        assert legacy_path is not None

        marker = await self._migration_marker()
        if marker is not None:
            return LegacyMigrationResult(
                state=("already_imported" if marker["source_state"] == "imported" else "absent"),
                imported_rows=int(marker["imported_rows"]),
                source_sha256=str(marker["source_sha256"]),
            )
        if marker is None and await self._alert_count() != 0:
            raise LegacyAlertMigrationError(
                "refusing legacy import: alerts table is non-empty without an import marker"
            )

        try:
            payload = await asyncio.to_thread(self._load_legacy_payload, legacy_path)
        except FileNotFoundError:
            return await self.database.transaction(self._mark_legacy_absent)
        except LegacyAlertMigrationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            backup = await asyncio.to_thread(self._preserve_corrupt_source, legacy_path)
            raise LegacyAlertMigrationError(
                "legacy alerts file failed strict validation",
                backup_path=backup,
            ) from exc

        def import_payload(transaction: Transaction) -> LegacyMigrationResult:
            existing = transaction.read(
                "SELECT * FROM legacy_imports WHERE source_name = ?",
                (LEGACY_SOURCE_NAME,),
            )
            if existing and existing[0]["source_state"] == "imported":
                row = existing[0]
                return LegacyMigrationResult(
                    state="already_imported",
                    imported_rows=int(row["imported_rows"]),
                    source_sha256=str(row["source_sha256"]),
                )
            try:
                current_sha256 = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
            except OSError as exc:
                raise LegacyAlertMigrationError(
                    "legacy alerts file changed during migration"
                ) from exc
            if current_sha256 != payload.source_sha256:
                raise LegacyAlertMigrationError("legacy alerts file changed during migration")
            target_count = int(transaction.read("SELECT COUNT(*) AS count FROM alerts")[0]["count"])
            if target_count:
                raise LegacyAlertMigrationError(
                    "refusing legacy import: alerts table is non-empty without an import marker"
                )
            for row in payload.rows:
                transaction.write(
                    """
                    INSERT INTO alerts
                        (id, owner_user_id, group_id, market_name, target_price_cents,
                         status, attempts, next_retry_at, last_error,
                         last_observed_price_cents, created_at, updated_at,
                         delivered_at, message_id)
                    VALUES
                        (:id, :owner_user_id, :group_id, :market_name,
                         :target_price_cents, :status, :attempts, :next_retry_at,
                         :last_error, :last_observed_price_cents, :created_at,
                         :updated_at, :delivered_at, :message_id)
                    """,
                    row,
                )
            transaction.write(
                """
                INSERT INTO legacy_imports
                    (source_name, source_sha256, imported_rows, source_state)
                VALUES (?, ?, ?, 'imported')
                ON CONFLICT(source_name) DO UPDATE SET
                    source_sha256 = excluded.source_sha256,
                    imported_rows = excluded.imported_rows,
                    source_state = 'imported',
                    imported_at = CAST(strftime('%s', 'now') AS INTEGER)
                """,
                (LEGACY_SOURCE_NAME, payload.source_sha256, len(payload.rows)),
            )
            return LegacyMigrationResult(
                state="imported",
                imported_rows=len(payload.rows),
                source_sha256=payload.source_sha256,
            )

        return await self.database.transaction(import_payload)

    async def _migration_marker(self) -> dict[str, Any] | None:
        rows = await self.database.read(
            "SELECT * FROM legacy_imports WHERE source_name = ?",
            (LEGACY_SOURCE_NAME,),
        )
        return rows[0] if rows else None

    async def _alert_count(self) -> int:
        return int((await self.database.read("SELECT COUNT(*) AS count FROM alerts"))[0]["count"])

    def _mark_legacy_absent(self, transaction: Transaction) -> LegacyMigrationResult:
        existing = transaction.read(
            "SELECT * FROM legacy_imports WHERE source_name = ?",
            (LEGACY_SOURCE_NAME,),
        )
        if existing:
            row = existing[0]
            if row["source_state"] == "imported":
                return LegacyMigrationResult(
                    state="already_imported",
                    imported_rows=int(row["imported_rows"]),
                    source_sha256=str(row["source_sha256"]),
                )
            return LegacyMigrationResult(
                state="absent",
                imported_rows=0,
                source_sha256=str(row["source_sha256"]),
            )
        if self.legacy_path is not None and self.legacy_path.exists():
            raise LegacyAlertMigrationError("legacy alerts file appeared during migration")
        target_count = int(transaction.read("SELECT COUNT(*) AS count FROM alerts")[0]["count"])
        if target_count:
            raise LegacyAlertMigrationError(
                "refusing legacy migration: alerts table is non-empty without an import marker"
            )
        transaction.write(
            """
            INSERT INTO legacy_imports
                (source_name, source_sha256, imported_rows, source_state)
            VALUES (?, ?, 0, 'absent')
            """,
            (LEGACY_SOURCE_NAME, _EMPTY_SHA256),
        )
        return LegacyMigrationResult(
            state="absent",
            imported_rows=0,
            source_sha256=_EMPTY_SHA256,
        )

    @classmethod
    def _load_legacy_payload(cls, path: Path) -> _LegacyPayload:
        raw_bytes = path.read_bytes()
        source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        document = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
        if not isinstance(document, list):
            raise ValueError("root value must be a list")
        rows: list[dict[str, object]] = []
        occurrence_counts: Counter[str] = Counter()
        explicit_ids: set[str] = set()
        owner_counts: Counter[int] = Counter()
        for entry in document:
            if not isinstance(entry, dict):
                raise ValueError("every alert entry must be an object")
            row = cls._normalize_legacy_entry(entry, occurrence_counts)
            alert_id = str(row["id"])
            if alert_id in explicit_ids:
                raise ValueError(f"duplicate alert id: {alert_id}")
            explicit_ids.add(alert_id)
            owner = _strict_positive_integer(row["owner_user_id"], "owner_user_id")
            owner_counts[owner] += 1
            if owner_counts[owner] > MAX_ALERTS_PER_OWNER:
                raise ValueError(f"owner {owner} exceeds the alert quota")
            rows.append(row)
        return _LegacyPayload(source_sha256=source_sha256, rows=tuple(rows))

    @classmethod
    def _normalize_legacy_entry(
        cls,
        raw: dict[str, Any],
        occurrence_counts: Counter[str],
    ) -> dict[str, object]:
        name = _market_name(raw.get("name", raw.get("market_name")))
        if "owner_user_id" in raw and "qq" in raw:
            owner = _strict_positive_integer(raw["owner_user_id"], "owner_user_id")
            if owner != _strict_positive_integer(raw["qq"], "qq"):
                raise ValueError("owner_user_id and qq disagree")
        else:
            owner = _strict_positive_integer(
                raw.get("owner_user_id", raw.get("qq")),
                "owner_user_id",
            )
        if "group_id" in raw and "gid" in raw:
            group = _optional_group_id(raw["group_id"])
            if group != _optional_group_id(raw["gid"]):
                raise ValueError("group_id and gid disagree")
        else:
            group = _optional_group_id(raw.get("group_id", raw.get("gid")))

        if "target_price_cents" in raw:
            target_cents = _positive_cents(raw["target_price_cents"], "target_price_cents")
        else:
            legacy_price = cast(Decimal | int | float | str, raw.get("price"))
            target_cents = price_to_cents(legacy_price)

        raw_status = raw.get("status")
        triggered_value = raw.get("triggered")
        triggered: bool | None
        if triggered_value is None:
            triggered = None
        elif isinstance(triggered_value, bool):
            triggered = triggered_value
        elif triggered_value in (0, 1):
            triggered = bool(triggered_value)
        else:
            raise ValueError("triggered must be boolean")
        if raw_status is None:
            status = "delivered" if triggered else "pending"
        elif raw_status in {"pending", "delivered"}:
            status = str(raw_status)
            if triggered is not None and triggered != (status == "delivered"):
                raise ValueError("status and triggered disagree")
        else:
            raise ValueError(f"unsupported legacy status: {raw_status!r}")

        created_value = raw.get("created_at", raw.get("time"))
        created_at = _legacy_epoch(created_value, "created_at", default=0)
        assert created_at is not None
        next_retry_at = _legacy_epoch(
            raw.get("next_retry_at", raw.get("next_retry")),
            "next_retry_at",
            default=None,
        )
        delivered_at = _legacy_epoch(raw.get("delivered_at"), "delivered_at", default=None)
        if status == "delivered" and delivered_at is None:
            delivered_at = created_at
        if status == "pending":
            delivered_at = None

        attempts = _nonnegative_integer(raw.get("attempts", 0), "attempts")
        last_error_value = raw.get("last_error")
        if last_error_value is not None and not isinstance(last_error_value, str):
            raise ValueError("last_error must be text or null")
        last_error = last_error_value[:MAX_ERROR_LENGTH] if last_error_value else None
        observed_value = raw.get("last_observed_price_cents")
        observed = (
            _positive_cents(observed_value, "last_observed_price_cents")
            if observed_value is not None
            else None
        )
        message_value = raw.get("message_id")
        if message_value is not None and not isinstance(message_value, (str, int)):
            raise ValueError("message_id must be text, an integer, or null")
        message_id = str(message_value) if message_value is not None else None
        if message_id is not None and len(message_id) > MAX_MESSAGE_ID_LENGTH:
            raise ValueError("message_id exceeds the safety limit")

        raw_id = raw.get("id")
        if raw_id is not None:
            if not isinstance(raw_id, str) or not raw_id.strip() or len(raw_id.strip()) > 128:
                raise ValueError("alert id is invalid")
            alert_id = raw_id.strip()
        else:
            identity = json.dumps(
                {
                    "name": name,
                    "price_cents": target_cents,
                    "owner": owner,
                    "gid": group,
                    "created_at": created_at,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            occurrence = occurrence_counts[identity]
            occurrence_counts[identity] += 1
            digest = hashlib.sha256(f"{identity}:{occurrence}".encode()).hexdigest()
            alert_id = f"legacy-{digest[:40]}"

        return {
            "id": alert_id,
            "owner_user_id": owner,
            "group_id": group,
            "market_name": name,
            "target_price_cents": target_cents,
            "status": status,
            "attempts": attempts,
            "next_retry_at": None if status == "delivered" else next_retry_at,
            "last_error": None if status == "delivered" else last_error,
            "last_observed_price_cents": observed,
            "created_at": created_at,
            "updated_at": created_at,
            "delivered_at": delivered_at,
            "message_id": message_id,
        }

    @staticmethod
    def _preserve_corrupt_source(path: Path) -> Path | None:
        temporary_path: str | None = None
        try:
            damaged = path.read_bytes()
            destination = path.with_name(f"{path.name}.corrupt-{time.time_ns()}.bak")
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as backup:
                temporary_path = backup.name
                backup.write(damaged)
                backup.flush()
                os.fsync(backup.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            return destination
        except OSError:
            logger.exception("Could not preserve corrupt legacy alert file")
            return None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

"""Persistent lease/outbox state for scheduled group broadcasts."""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from src.core.database import StateDatabase, Transaction

from .repository import Repository

DeliveryStatus = Literal["pending", "claimed", "delivered", "failed"]
MAX_GROUPS_PER_CLAIM = 1_000
DEFAULT_RETRY_BASE_SECONDS = 30
DEFAULT_RETRY_MAX_SECONDS = 3_600
DEFAULT_MAX_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class BroadcastDelivery:
    schedule_key: str
    group_id: int
    status: DeliveryStatus
    attempts: int
    next_retry_at: int | None
    lease_owner: str | None
    lease_expires_at: int | None
    last_error: str | None
    payload_text: str | None
    created_at: int
    updated_at: int
    delivered_at: int | None
    failed_at: int | None
    message_id: str | None


def _delivery(row: Mapping[str, Any]) -> BroadcastDelivery:
    status = str(row["status"])
    if status not in {"pending", "claimed", "delivered", "failed"}:
        raise RuntimeError(f"unknown broadcast delivery status: {status}")
    return BroadcastDelivery(
        schedule_key=str(row["schedule_key"]),
        group_id=int(row["group_id"]),
        status=cast(DeliveryStatus, status),
        attempts=int(row["attempts"]),
        next_retry_at=(int(row["next_retry_at"]) if row["next_retry_at"] is not None else None),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            int(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        payload_text=(str(row["payload_text"]) if row["payload_text"] is not None else None),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        delivered_at=(int(row["delivered_at"]) if row["delivered_at"] is not None else None),
        failed_at=int(row["failed_at"]) if row["failed_at"] is not None else None,
        message_id=str(row["message_id"]) if row["message_id"] is not None else None,
    )


def _key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("schedule_key must be a string")
    result = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(result) <= 200:
        raise ValueError("schedule_key must contain between 1 and 200 characters")
    return result


def _token(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("lease_token must be a string")
    result = value.strip()
    if not 1 <= len(result) <= 200:
        raise ValueError("lease_token must contain between 1 and 200 characters")
    return result


def _timestamp(value: int | None) -> int:
    result = int(time.time()) if value is None else value
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("timestamp must be a non-negative integer")
    return result


def _positive_seconds(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _group_ids(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise ValueError("group_ids must be a sequence")
    if len(values) > MAX_GROUPS_PER_CLAIM:
        raise ValueError(f"at most {MAX_GROUPS_PER_CLAIM} groups may be claimed at once")
    result: list[int] = []
    seen: set[int] = set()
    for group_id in values:
        if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0:
            raise ValueError("group ids must be positive integers")
        if group_id not in seen:
            seen.add(group_id)
            result.append(group_id)
    return tuple(result)


def _optional_message_id(value: object | None) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if len(result) > 200:
        raise ValueError("message_id must not exceed 200 characters")
    return result


class BroadcastRepository(Repository):
    """Coordinates broadcast attempts across processes through durable leases."""

    def __init__(
        self,
        database: StateDatabase,
        *,
        retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: int = DEFAULT_RETRY_MAX_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        super().__init__(database)
        self.retry_base_seconds = _positive_seconds(retry_base_seconds, field="retry_base_seconds")
        self.retry_max_seconds = _positive_seconds(retry_max_seconds, field="retry_max_seconds")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds must not exceed retry_max_seconds")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer between 1 and 100")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be an integer between 1 and 100")
        self.max_attempts = max_attempts

    async def claim(
        self,
        schedule_key: str,
        group_ids: Sequence[int],
        *,
        lease_token: str,
        lease_seconds: int,
        now: int | None = None,
    ) -> list[BroadcastDelivery]:
        """Claim due target groups, creating their unique delivery rows if needed."""

        key = _key(schedule_key)
        groups = _group_ids(group_ids)
        token = _token(lease_token)
        lease_duration = _positive_seconds(lease_seconds, field="lease_seconds")
        timestamp = _timestamp(now)
        if not groups:
            return []
        lease_expires_at = timestamp + lease_duration

        def claim_in_transaction(transaction: Transaction) -> list[BroadcastDelivery]:
            # Statement counts stay flat in the number of groups: every
            # per-group parameter except the group_id itself is shared, so the
            # loops collapse into set statements whose IN lists are built from
            # positional ``?`` placeholders only.
            placeholders = ",".join("?" for _ in groups)
            value_rows = ",".join("(?, ?, 'pending', ?, ?)" for _ in groups)
            insert_parameters: list[object] = []
            for group_id in groups:
                insert_parameters.extend((key, group_id, timestamp, timestamp))
            transaction.write(
                f"""
                INSERT OR IGNORE INTO broadcast_deliveries
                    (schedule_key, group_id, status, created_at, updated_at)
                VALUES {value_rows}
                """,
                tuple(insert_parameters),
            )

            # A process that died while holding a lease must not permanently
            # strand the group.  This transition is protected by the same write
            # lock as the following claims, so only one successor can win.
            transaction.write(
                f"""
                UPDATE broadcast_deliveries
                SET status = CASE
                        WHEN attempts + 1 >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    attempts = attempts + 1,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_retry_at = CASE
                        WHEN attempts + 1 >= ? THEN NULL
                        ELSE ?
                    END,
                    last_error = COALESCE(last_error, 'previous claim lease expired'),
                    failed_at = CASE
                        WHEN attempts + 1 >= ? THEN ?
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE schedule_key = ? AND group_id IN ({placeholders})
                  AND status = 'claimed' AND lease_expires_at <= ?
                """,
                (
                    self.max_attempts,
                    self.max_attempts,
                    timestamp,
                    self.max_attempts,
                    timestamp,
                    timestamp,
                    key,
                    *groups,
                    timestamp,
                ),
            )
            transaction.write(
                f"""
                UPDATE broadcast_deliveries
                SET status = 'claimed', lease_owner = ?, lease_expires_at = ?,
                    next_retry_at = NULL, updated_at = ?
                WHERE schedule_key = ? AND group_id IN ({placeholders})
                  AND status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                """,
                (token, lease_expires_at, timestamp, key, *groups, timestamp),
            )

            rows = transaction.read(
                f"""
                SELECT schedule_key, group_id, status, attempts, next_retry_at,
                       lease_owner, lease_expires_at, last_error, payload_text, created_at,
                       updated_at, delivered_at, failed_at, message_id
                FROM broadcast_deliveries
                WHERE schedule_key = ? AND lease_owner = ?
                  AND group_id IN ({placeholders})
                ORDER BY group_id
                """,
                (key, token, *groups),
            )
            # Contract: lease tokens are freshly generated uuid4 hex values
            # unique to this claim call, so the read-back above can only see
            # rows this call just claimed — never rows claimed by another
            # worker (past or concurrent).
            return [_delivery(row) for row in rows]

        return await self.database.transaction(claim_in_transaction)

    async def claim_due(
        self,
        *,
        lease_token: str,
        lease_seconds: int,
        limit: int = 100,
        now: int | None = None,
    ) -> list[BroadcastDelivery]:
        """Claim existing failed or expired deliveries across schedule slots."""

        token = _token(lease_token)
        lease_duration = _positive_seconds(lease_seconds, field="lease_seconds")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        timestamp = _timestamp(now)
        lease_expires_at = timestamp + lease_duration

        def claim_in_transaction(transaction: Transaction) -> list[BroadcastDelivery]:
            transaction.write(
                """
                WITH expired AS (
                    SELECT schedule_key, group_id
                    FROM broadcast_deliveries
                    WHERE status = 'claimed' AND lease_expires_at <= ?
                    ORDER BY lease_expires_at, schedule_key, group_id
                    LIMIT ?
                )
                UPDATE broadcast_deliveries
                SET status = CASE
                        WHEN attempts + 1 >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    attempts = attempts + 1,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_retry_at = CASE
                        WHEN attempts + 1 >= ? THEN NULL
                        ELSE ?
                    END,
                    last_error = COALESCE(last_error, 'previous claim lease expired'),
                    failed_at = CASE
                        WHEN attempts + 1 >= ? THEN ?
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE (schedule_key, group_id) IN (
                    SELECT schedule_key, group_id FROM expired
                )
                """,
                (
                    timestamp,
                    limit,
                    self.max_attempts,
                    self.max_attempts,
                    timestamp,
                    self.max_attempts,
                    timestamp,
                    timestamp,
                ),
            )
            candidates = transaction.read(
                """
                SELECT schedule_key, group_id
                FROM broadcast_deliveries
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY COALESCE(next_retry_at, created_at), schedule_key, group_id
                LIMIT ?
                """,
                (timestamp, limit),
            )
            for candidate in candidates:
                transaction.write(
                    """
                    UPDATE broadcast_deliveries
                    SET status = 'claimed', lease_owner = ?, lease_expires_at = ?,
                        next_retry_at = NULL, updated_at = ?
                    WHERE schedule_key = ? AND group_id = ? AND status = 'pending'
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    """,
                    (
                        token,
                        lease_expires_at,
                        timestamp,
                        candidate["schedule_key"],
                        candidate["group_id"],
                        timestamp,
                    ),
                )
            rows = transaction.read(
                """
                SELECT schedule_key, group_id, status, attempts, next_retry_at,
                       lease_owner, lease_expires_at, last_error, payload_text, created_at,
                       updated_at, delivered_at, failed_at, message_id
                FROM broadcast_deliveries
                WHERE status = 'claimed' AND lease_owner = ?
                ORDER BY schedule_key, group_id
                """,
                (token,),
            )
            return [_delivery(row) for row in rows]

        return await self.database.transaction(claim_in_transaction)

    async def get_payload(self, schedule_key: str) -> str | None:
        """Return the immutable payload already associated with a schedule slot."""

        rows = await self.database.read(
            """
            SELECT payload_text FROM broadcast_deliveries
            WHERE schedule_key = ? AND payload_text IS NOT NULL
            ORDER BY group_id LIMIT 1
            """,
            (_key(schedule_key),),
        )
        return str(rows[0]["payload_text"]) if rows else None

    async def set_payload(
        self,
        schedule_key: str,
        *,
        lease_token: str,
        payload_text: str,
    ) -> str | None:
        """Freeze one payload for all rows currently owned by this claim token."""

        key = _key(schedule_key)
        token = _token(lease_token)
        if not isinstance(payload_text, str) or not 1 <= len(payload_text) <= 4_000:
            raise ValueError("payload_text must contain between 1 and 4000 characters")

        def freeze(transaction: Transaction) -> str | None:
            owned = transaction.read(
                """
                SELECT 1 FROM broadcast_deliveries
                WHERE schedule_key = ? AND status = 'claimed' AND lease_owner = ?
                LIMIT 1
                """,
                (key, token),
            )
            if not owned:
                return None
            existing = transaction.read(
                """
                SELECT payload_text FROM broadcast_deliveries
                WHERE schedule_key = ? AND payload_text IS NOT NULL
                ORDER BY group_id LIMIT 1
                """,
                (key,),
            )
            frozen = str(existing[0]["payload_text"]) if existing else payload_text
            transaction.write(
                """
                UPDATE broadcast_deliveries
                SET payload_text = COALESCE(payload_text, ?)
                WHERE schedule_key = ? AND status = 'claimed' AND lease_owner = ?
                """,
                (frozen, key, token),
            )
            return frozen

        return await self.database.transaction(freeze)

    async def mark_delivered(
        self,
        schedule_key: str,
        group_id: int,
        *,
        lease_token: str,
        message_id: object | None = None,
        delivered_at: int | None = None,
    ) -> bool:
        """Commit success only when the caller still owns the current claim."""

        key = _key(schedule_key)
        group = _group_ids((group_id,))[0]
        token = _token(lease_token)
        timestamp = _timestamp(delivered_at)
        canonical_message_id = _optional_message_id(message_id)
        result = await self.database.write(
            """
            UPDATE broadcast_deliveries
            SET status = 'delivered', lease_owner = NULL, lease_expires_at = NULL,
                next_retry_at = NULL, last_error = NULL, delivered_at = ?,
                failed_at = NULL, message_id = ?, updated_at = ?
            WHERE schedule_key = ? AND group_id = ?
              AND status = 'claimed' AND lease_owner = ?
            """,
            (timestamp, canonical_message_id, timestamp, key, group, token),
        )
        return result.rowcount == 1

    def _record_failure_in_transaction(
        self,
        transaction: Transaction,
        *,
        key: str,
        group: int,
        token: str,
        error_text: str,
        timestamp: int,
        retry_delay_seconds: int | None,
    ) -> bool:
        rows = transaction.read(
            """
            SELECT attempts FROM broadcast_deliveries
            WHERE schedule_key = ? AND group_id = ?
              AND status = 'claimed' AND lease_owner = ?
            """,
            (key, group, token),
        )
        if not rows:
            return False
        attempts = int(rows[0]["attempts"])
        next_attempt = attempts + 1
        if next_attempt >= self.max_attempts:
            result = transaction.write(
                """
                UPDATE broadcast_deliveries
                SET status = 'failed', attempts = ?, next_retry_at = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, failed_at = ?, updated_at = ?
                WHERE schedule_key = ? AND group_id = ?
                  AND status = 'claimed' AND lease_owner = ?
                """,
                (next_attempt, error_text, timestamp, timestamp, key, group, token),
            )
            return result.rowcount == 1
        delay = retry_delay_seconds
        if delay is None:
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** min(attempts, 20)),
            )
        result = transaction.write(
            """
            UPDATE broadcast_deliveries
            SET status = 'pending', attempts = ?,
                next_retry_at = ?, lease_owner = NULL, lease_expires_at = NULL,
                last_error = ?, updated_at = ?
            WHERE schedule_key = ? AND group_id = ?
              AND status = 'claimed' AND lease_owner = ?
            """,
            (next_attempt, timestamp + delay, error_text, timestamp, key, group, token),
        )
        return result.rowcount == 1

    async def record_failure(
        self,
        schedule_key: str,
        group_id: int,
        *,
        lease_token: str,
        error: object,
        failed_at: int | None = None,
        retry_delay_seconds: int | None = None,
    ) -> bool:
        """Release a failed claim with bounded exponential retry metadata."""

        key = _key(schedule_key)
        group = _group_ids((group_id,))[0]
        token = _token(lease_token)
        timestamp = _timestamp(failed_at)
        error_text = unicodedata.normalize("NFKC", str(error)).strip() or "delivery failed"
        error_text = error_text[:1000]
        if retry_delay_seconds is not None:
            retry_delay_seconds = _positive_seconds(
                retry_delay_seconds, field="retry_delay_seconds"
            )

        def fail_in_transaction(transaction: Transaction) -> bool:
            return self._record_failure_in_transaction(
                transaction,
                key=key,
                group=group,
                token=token,
                error_text=error_text,
                timestamp=timestamp,
                retry_delay_seconds=retry_delay_seconds,
            )

        return await self.database.transaction(fail_in_transaction)

    async def record_claim_failures(
        self,
        schedule_key: str,
        *,
        lease_token: str,
        error: object,
        failed_at: int | None = None,
    ) -> int:
        """Release all rows from one failed payload claim in a single transaction."""

        key = _key(schedule_key)
        token = _token(lease_token)
        timestamp = _timestamp(failed_at)
        error_text = unicodedata.normalize("NFKC", str(error)).strip() or "delivery failed"
        error_text = error_text[:1000]

        def fail_claim(transaction: Transaction) -> int:
            rows = transaction.read(
                """
                SELECT group_id FROM broadcast_deliveries
                WHERE schedule_key = ? AND status = 'claimed' AND lease_owner = ?
                ORDER BY group_id
                """,
                (key, token),
            )
            return sum(
                self._record_failure_in_transaction(
                    transaction,
                    key=key,
                    group=int(row["group_id"]),
                    token=token,
                    error_text=error_text,
                    timestamp=timestamp,
                    retry_delay_seconds=None,
                )
                for row in rows
            )

        return await self.database.transaction(fail_claim)

    async def list(
        self,
        *,
        schedule_key: str | None = None,
        status: DeliveryStatus | None = None,
        limit: int = 1_000,
    ) -> list[BroadcastDelivery]:
        """List delivery state for operations and diagnostics."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        parameters: list[object] = []
        conditions: list[str] = []
        if schedule_key is not None:
            conditions.append("schedule_key = ?")
            parameters.append(_key(schedule_key))
        if status is not None:
            if status not in {"pending", "claimed", "delivered", "failed"}:
                raise ValueError("status is invalid")
            conditions.append("status = ?")
            parameters.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        rows = await self.database.read(
            """
            SELECT schedule_key, group_id, status, attempts, next_retry_at,
                   lease_owner, lease_expires_at, last_error, payload_text, created_at,
                   updated_at, delivered_at, failed_at, message_id
            FROM broadcast_deliveries
            """
            + where
            + " ORDER BY created_at, schedule_key, group_id LIMIT ?",
            parameters,
        )
        return [_delivery(row) for row in rows]

    async def retry_failed(
        self,
        schedule_key: str,
        group_id: int,
        *,
        now: int | None = None,
    ) -> bool:
        """Move one dead-lettered delivery back to the retry queue."""

        key = _key(schedule_key)
        group = _group_ids((group_id,))[0]
        timestamp = _timestamp(now)
        result = await self.database.write(
            """
            UPDATE broadcast_deliveries
            SET status = 'pending', attempts = 0, next_retry_at = ?,
                failed_at = NULL, updated_at = ?
            WHERE schedule_key = ? AND group_id = ? AND status = 'failed'
            """,
            (timestamp, timestamp, key, group),
        )
        return result.rowcount == 1

    async def discard_failed(self, schedule_key: str, group_id: int) -> bool:
        """Permanently discard one dead-lettered delivery."""

        key = _key(schedule_key)
        group = _group_ids((group_id,))[0]
        result = await self.database.write(
            """
            DELETE FROM broadcast_deliveries
            WHERE schedule_key = ? AND group_id = ? AND status = 'failed'
            """,
            (key, group),
        )
        return result.rowcount == 1

    async def cleanup_history(
        self,
        *,
        delivered_before: int,
        limit: int = 1_000,
    ) -> int:
        """Delete a bounded batch of old successful rows; pending work is retained."""

        timestamp = _timestamp(delivered_before)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await self.database.write(
            """
            DELETE FROM broadcast_deliveries
            WHERE (schedule_key, group_id) IN (
                SELECT schedule_key, group_id
                FROM broadcast_deliveries
                WHERE status = 'delivered' AND delivered_at < ?
                ORDER BY delivered_at, schedule_key, group_id
                LIMIT ?
            )
            """,
            (timestamp, limit),
        )
        return result.rowcount

    async def cleanup_failed(self, *, failed_before: int, limit: int = 1_000) -> int:
        """Delete a bounded batch of old dead letters after their retention period."""

        timestamp = _timestamp(failed_before)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await self.database.write(
            """
            DELETE FROM broadcast_deliveries
            WHERE (schedule_key, group_id) IN (
                SELECT schedule_key, group_id
                FROM broadcast_deliveries
                WHERE status = 'failed' AND failed_at < ?
                ORDER BY failed_at, schedule_key, group_id
                LIMIT ?
            )
            """,
            (timestamp, limit),
        )
        return max(0, result.rowcount)

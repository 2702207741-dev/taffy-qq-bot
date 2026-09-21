"""Durable inventory-monitor baselines, subscriptions, and QQ deliveries."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from src.core.database import StateDatabase, Transaction

from ..services.float_resolver import ItemFloatAnchors, serialize_stickers
from .repository import Repository

DestinationType = Literal["group", "private"]

MAX_MONITORS_PER_OWNER = 20
MAX_TOTAL_MONITORED_ACCOUNTS = 500
MAX_TOTAL_SUBSCRIPTIONS = 5_000
MAX_SNAPSHOT_JSON_LENGTH = 4_000_000
MAX_ITEM_JSON_LENGTH = 4_000
MAX_MESSAGE_LENGTH = 4_000
MAX_ERROR_LENGTH = 1_000
MAX_MESSAGE_ID_LENGTH = 200
MAX_SQLITE_INTEGER = 2**63 - 1
#: Rows per multi-row INSERT for item history upserts.
_ITEM_INSERT_CHUNK = 200
DAILY_INTERVAL_SECONDS = 24 * 60 * 60
ACCOUNT_FAILURE_RETRY_SECONDS = 15 * 60
ACCOUNT_FAILURE_BACKOFF_CAP_SECONDS = 2 * 60 * 60
#: After this many consecutive failed checks an account is parked for a full
#: day instead of hitting the upstream every poll cycle; a successful check or
#: a manual resubscribe clears the counter and resumes normal polling.
ACCOUNT_MAX_CONSECUTIVE_FAILURES = 10
ACCOUNT_DISABLED_PARK_SECONDS = 24 * 60 * 60


class InventoryMonitorRepositoryError(RuntimeError):
    """Inventory monitor state could not be safely read or changed."""


@dataclass(frozen=True, slots=True)
class InventoryMonitorAccount:
    steam_id: str
    nickname: str
    snapshot_json: str
    snapshot_hash: str
    source_time: int
    last_success_at: int
    next_check_at: int
    last_error: str | None
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class InventoryMonitorSubscription:
    owner_user_id: int
    destination_type: DestinationType
    destination_id: int
    steam_id: str
    nickname: str
    source_time: int
    last_success_at: int
    next_check_at: int
    created_at: int


@dataclass(frozen=True, slots=True)
class InventoryAccountClaim:
    account: InventoryMonitorAccount
    lease_token: str
    lease_expires_at: int


@dataclass(frozen=True, slots=True)
class InventoryDelivery:
    event_id: str
    owner_user_id: int
    destination_type: DestinationType
    destination_id: int
    message_text: str
    attempts: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class SubscribeResult:
    created: bool
    quota_exceeded: bool
    account: InventoryMonitorAccount | None
    global_quota_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class CommitResult:
    applied: bool
    event_id: str | None
    queued_deliveries: int


def _epoch(value: datetime | int | None = None) -> int:
    if value is None:
        result = int(time.time())
    elif isinstance(value, bool):
        raise ValueError("timestamp must be a non-negative integer or aware datetime")
    elif isinstance(value, int):
        result = value
    elif isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime timestamps must include a timezone")
        result = int(value.timestamp())
    else:
        raise ValueError("timestamp must be a non-negative integer or aware datetime")
    if not 0 <= result <= MAX_SQLITE_INTEGER:
        raise ValueError("timestamp is outside the supported range")
    return result


def _positive_integer(value: int | str, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError(f"{field} must be a positive integer")
    if not 0 < parsed <= MAX_SQLITE_INTEGER:
        raise ValueError(f"{field} is outside the supported range")
    return parsed


def _steam_id(value: str) -> str:
    result = value.strip()
    if len(result) != 17 or not result.isascii() or not result.isdecimal():
        raise ValueError("steam_id must be a 17-digit identifier")
    return result


def _destination_type(value: str) -> DestinationType:
    if value not in {"group", "private"}:
        raise ValueError("destination_type must be group or private")
    return cast(DestinationType, value)


def _bounded_text(value: str, field: str, maximum: int) -> str:
    result = value.strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{field} must contain 1 to {maximum} characters")
    return result


def _hash(value: str, field: str) -> str:
    result = value.strip().casefold()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return result


def _snapshot_json(value: str) -> str:
    if not 2 <= len(value) <= MAX_SNAPSHOT_JSON_LENGTH:
        raise ValueError("snapshot_json exceeds the supported size")
    return value


def _item_rows(items: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if len(items) > 20_000:
        raise ValueError("inventory contains too many item rows")
    rows: list[tuple[str, str]] = []
    for raw_key, raw_json in items.items():
        key = _bounded_text(str(raw_key), "item_key", 200)
        item_json = str(raw_json)
        if not 2 <= len(item_json) <= MAX_ITEM_JSON_LENGTH:
            raise ValueError("item_json exceeds the supported size")
        rows.append((key, item_json))
    return tuple(rows)


#: Persisted anchor tuple: (market_hash_name, paint_index, paintwear,
#: pattern_index, stickers_json) keyed by item_key.
ItemAnchorValues = tuple[str | None, int | None, float | None, int | None, str | None]

_EMPTY_ANCHORS: ItemAnchorValues = (None, None, None, None, None)

_MAX_FLOAT_CACHE_BATCH = 200


@dataclass(frozen=True, slots=True)
class InventoryHistoryTrajectoryRow:
    """Raw history projection served to the service-layer aggregator."""

    item_key: str
    name: str
    market_hash_name: str | None
    paintwear: float | None
    first_seen_at: int
    last_seen_at: int
    last_missing_at: int | None


def _anchor_rows(
    anchors: Mapping[str, ItemAnchorValues] | None,
) -> dict[str, ItemAnchorValues]:
    """Validate optional per-item anchor columns before persistence."""

    if not anchors:
        return {}
    rows: dict[str, ItemAnchorValues] = {}
    for item_key, values in anchors.items():
        bounded_key = _bounded_text(str(item_key), "item_key", 200)
        name, paint_index, paintwear, pattern_index, stickers = values
        if name is not None:
            name = _bounded_text(str(name), "market_hash_name", 256)
        elif any(value is not None for value in (paint_index, paintwear, pattern_index, stickers)):
            # Wear without a display name cannot become a usable anchor; keep
            # the row NULL-anchored rather than half-populated.
            continue
        if paint_index is not None and (isinstance(paint_index, bool) or paint_index < 0):
            raise ValueError("paint_index must be a non-negative integer or None")
        if pattern_index is not None and (isinstance(pattern_index, bool) or pattern_index < 0):
            raise ValueError("pattern_index must be a non-negative integer or None")
        if stickers is not None and not 0 < len(stickers) <= 2000:
            raise ValueError("stickers_json exceeds the supported size")
        rows[bounded_key] = (name, paint_index, paintwear, pattern_index, stickers)
    return rows


def _row_to_account(row: Mapping[str, Any]) -> InventoryMonitorAccount:
    return InventoryMonitorAccount(
        steam_id=str(row["steam_id"]),
        nickname=str(row["nickname"]),
        snapshot_json=str(row["snapshot_json"]),
        snapshot_hash=str(row["snapshot_hash"]),
        source_time=int(row["source_time"]),
        last_success_at=int(row["last_success_at"]),
        next_check_at=int(row["next_check_at"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        consecutive_failures=int(row.get("consecutive_failures") or 0),
    )


def _row_to_subscription(row: Mapping[str, Any]) -> InventoryMonitorSubscription:
    return InventoryMonitorSubscription(
        owner_user_id=int(row["owner_user_id"]),
        destination_type=_destination_type(str(row["destination_type"])),
        destination_id=int(row["destination_id"]),
        steam_id=str(row["steam_id"]),
        nickname=str(row["nickname"]),
        source_time=int(row["source_time"]),
        last_success_at=int(row["last_success_at"]),
        next_check_at=int(row["next_check_at"]),
        created_at=int(row["created_at"]),
    )


class InventoryMonitorRepository(Repository):
    """Multi-process-safe persistence for one shared baseline per Steam account."""

    def __init__(
        self,
        database: StateDatabase,
        *,
        max_attempts: int = 8,
        retry_base_seconds: int = 60,
        retry_max_seconds: int = 3_600,
        max_total_accounts: int = MAX_TOTAL_MONITORED_ACCOUNTS,
        max_total_subscriptions: int = MAX_TOTAL_SUBSCRIPTIONS,
    ) -> None:
        super().__init__(database)
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("delivery retry settings are invalid")
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        if not 1 <= max_total_accounts <= 100_000:
            raise ValueError("max_total_accounts must be between 1 and 100000")
        if not 1 <= max_total_subscriptions <= 1_000_000:
            raise ValueError("max_total_subscriptions must be between 1 and 1000000")
        self.max_total_accounts = max_total_accounts
        self.max_total_subscriptions = max_total_subscriptions

    async def subscribe(
        self,
        owner_user_id: int | str,
        destination_type: str,
        destination_id: int | str,
        *,
        steam_id: str,
        nickname: str,
        snapshot_json: str,
        snapshot_hash: str,
        source_time: int,
        items: Mapping[str, str],
        anchors: Mapping[str, ItemAnchorValues] | None = None,
        now: datetime | int | None = None,
    ) -> SubscribeResult:
        owner = _positive_integer(owner_user_id, "owner_user_id")
        kind = _destination_type(destination_type)
        destination = _positive_integer(destination_id, "destination_id")
        account_id = _steam_id(steam_id)
        account_name = _bounded_text(nickname, "nickname", 100)
        payload = _snapshot_json(snapshot_json)
        digest = _hash(snapshot_hash, "snapshot_hash")
        source_timestamp = _epoch(source_time)
        timestamp = _epoch(now)
        item_rows = _item_rows(items)
        anchor_rows = _anchor_rows(anchors)
        next_check = timestamp + DAILY_INTERVAL_SECONDS

        def create(transaction: Transaction) -> SubscribeResult:
            existing = transaction.read(
                """
                SELECT a.*
                FROM inventory_monitor_accounts AS a
                JOIN inventory_monitor_subscriptions AS s ON s.steam_id = a.steam_id
                WHERE s.owner_user_id = ? AND s.destination_type = ?
                    AND s.destination_id = ? AND s.steam_id = ?
                """,
                (owner, kind, destination, account_id),
            )
            if existing:
                return SubscribeResult(False, False, _row_to_account(existing[0]))

            count = int(
                transaction.read(
                    """
                    SELECT COUNT(*) AS count
                    FROM inventory_monitor_subscriptions
                    WHERE owner_user_id = ?
                    """,
                    (owner,),
                )[0]["count"]
            )
            if count >= MAX_MONITORS_PER_OWNER:
                return SubscribeResult(False, True, None)

            total_subscriptions = int(
                transaction.read("SELECT COUNT(*) AS count FROM inventory_monitor_subscriptions")[
                    0
                ]["count"]
            )
            if total_subscriptions >= self.max_total_subscriptions:
                return SubscribeResult(False, False, None, True)

            account_rows = transaction.read(
                "SELECT * FROM inventory_monitor_accounts WHERE steam_id = ?",
                (account_id,),
            )
            if not account_rows:
                total_accounts = int(
                    transaction.read("SELECT COUNT(*) AS count FROM inventory_monitor_accounts")[0][
                        "count"
                    ]
                )
                if total_accounts >= self.max_total_accounts:
                    return SubscribeResult(False, False, None, True)
                transaction.write(
                    """
                    INSERT INTO inventory_monitor_accounts (
                        steam_id, nickname, snapshot_json, snapshot_hash,
                        source_time, last_success_at, next_check_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        account_name,
                        payload,
                        digest,
                        source_timestamp,
                        timestamp,
                        next_check,
                        timestamp,
                        timestamp,
                    ),
                )
                for item_key, item_json in item_rows:
                    anchor = anchor_rows.get(item_key, _EMPTY_ANCHORS)
                    transaction.write(
                        """
                        INSERT INTO inventory_monitor_item_history (
                            steam_id, item_key, item_json,
                            first_seen_at, last_seen_at, last_missing_at,
                            market_hash_name, paint_index, paintwear,
                            pattern_index, stickers_json
                        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            item_key,
                            item_json,
                            timestamp,
                            timestamp,
                            *anchor,
                        ),
                    )
            transaction.write(
                """
                INSERT INTO inventory_monitor_subscriptions (
                    owner_user_id, destination_type, destination_id,
                    steam_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (owner, kind, destination, account_id, timestamp),
            )
            row = transaction.read(
                "SELECT * FROM inventory_monitor_accounts WHERE steam_id = ?",
                (account_id,),
            )[0]
            return SubscribeResult(True, False, _row_to_account(row))

        return await self.database.transaction(create)

    async def list_destination(
        self,
        owner_user_id: int | str,
        destination_type: str,
        destination_id: int | str,
    ) -> tuple[InventoryMonitorSubscription, ...]:
        owner = _positive_integer(owner_user_id, "owner_user_id")
        kind = _destination_type(destination_type)
        destination = _positive_integer(destination_id, "destination_id")
        rows = await self.database.read(
            """
            SELECT s.*, a.nickname, a.source_time, a.last_success_at, a.next_check_at
            FROM inventory_monitor_subscriptions AS s
            JOIN inventory_monitor_accounts AS a ON a.steam_id = s.steam_id
            WHERE s.owner_user_id = ? AND s.destination_type = ?
                AND s.destination_id = ?
            ORDER BY s.created_at, s.steam_id
            """,
            (owner, kind, destination),
        )
        return tuple(_row_to_subscription(row) for row in rows)

    async def has_subscription(
        self,
        owner_user_id: int | str,
        destination_type: str,
        destination_id: int | str,
        steam_id: str,
    ) -> bool:
        owner = _positive_integer(owner_user_id, "owner_user_id")
        kind = _destination_type(destination_type)
        destination = _positive_integer(destination_id, "destination_id")
        account_id = _steam_id(steam_id)
        rows = await self.database.read(
            """
            SELECT 1 AS found FROM inventory_monitor_subscriptions
            WHERE owner_user_id = ? AND destination_type = ?
                AND destination_id = ? AND steam_id = ? LIMIT 1
            """,
            (owner, kind, destination, account_id),
        )
        return bool(rows)

    async def unsubscribe(
        self,
        owner_user_id: int | str,
        destination_type: str,
        destination_id: int | str,
        steam_id: str,
    ) -> bool:
        owner = _positive_integer(owner_user_id, "owner_user_id")
        kind = _destination_type(destination_type)
        destination = _positive_integer(destination_id, "destination_id")
        account_id = _steam_id(steam_id)

        def remove(transaction: Transaction) -> bool:
            result = transaction.write(
                """
                DELETE FROM inventory_monitor_subscriptions
                WHERE owner_user_id = ? AND destination_type = ?
                    AND destination_id = ? AND steam_id = ?
                """,
                (owner, kind, destination, account_id),
            )
            if result.rowcount != 1:
                return False
            remaining = int(
                transaction.read(
                    """
                    SELECT COUNT(*) AS count FROM inventory_monitor_subscriptions
                    WHERE steam_id = ?
                    """,
                    (account_id,),
                )[0]["count"]
            )
            if remaining == 0:
                transaction.write(
                    "DELETE FROM inventory_monitor_accounts WHERE steam_id = ?",
                    (account_id,),
                )
            return True

        return await self.database.transaction(remove)

    async def claim_due_accounts(
        self,
        limit: int,
        lease_seconds: int,
        *,
        now: datetime | int | None = None,
    ) -> tuple[InventoryAccountClaim, ...]:
        if not 1 <= limit <= 1_000 or lease_seconds <= 0:
            raise ValueError("account claim settings are invalid")
        timestamp = _epoch(now)
        expires_at = timestamp + lease_seconds

        def claim(transaction: Transaction) -> tuple[InventoryAccountClaim, ...]:
            candidates = transaction.read(
                """
                SELECT a.* FROM inventory_monitor_accounts AS a
                WHERE a.next_check_at <= ?
                    AND (a.lease_owner IS NULL OR a.lease_expires_at <= ?)
                    AND EXISTS (
                        SELECT 1 FROM inventory_monitor_subscriptions AS s
                        WHERE s.steam_id = a.steam_id
                    )
                ORDER BY a.next_check_at, a.steam_id
                LIMIT ?
                """,
                (timestamp, timestamp, limit),
            )
            claims: list[InventoryAccountClaim] = []
            for candidate in candidates:
                token = uuid.uuid4().hex
                updated = transaction.write(
                    """
                    UPDATE inventory_monitor_accounts
                    SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE steam_id = ?
                        AND (lease_owner IS NULL OR lease_expires_at <= ?)
                    """,
                    (token, expires_at, timestamp, candidate["steam_id"], timestamp),
                )
                if updated.rowcount != 1:
                    continue
                row = transaction.read(
                    "SELECT * FROM inventory_monitor_accounts WHERE steam_id = ?",
                    (candidate["steam_id"],),
                )[0]
                claims.append(InventoryAccountClaim(_row_to_account(row), token, expires_at))
            return tuple(claims)

        return await self.database.transaction(claim)

    async def claim_account(
        self,
        steam_id: str,
        lease_seconds: int,
        *,
        now: datetime | int | None = None,
    ) -> InventoryAccountClaim | None:
        account_id = _steam_id(steam_id)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = _epoch(now)
        expires_at = timestamp + lease_seconds

        def claim(transaction: Transaction) -> InventoryAccountClaim | None:
            rows = transaction.read(
                "SELECT * FROM inventory_monitor_accounts WHERE steam_id = ?",
                (account_id,),
            )
            if not rows:
                return None
            row = rows[0]
            if row["lease_owner"] is not None and int(row["lease_expires_at"]) > timestamp:
                return None
            token = uuid.uuid4().hex
            updated = transaction.write(
                """
                UPDATE inventory_monitor_accounts
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE steam_id = ?
                    AND (lease_owner IS NULL OR lease_expires_at <= ?)
                """,
                (token, expires_at, timestamp, account_id, timestamp),
            )
            if updated.rowcount != 1:
                return None
            current = transaction.read(
                "SELECT * FROM inventory_monitor_accounts WHERE steam_id = ?",
                (account_id,),
            )[0]
            return InventoryAccountClaim(_row_to_account(current), token, expires_at)

        return await self.database.transaction(claim)

    async def missing_item_keys(self, steam_id: str) -> frozenset[str]:
        account_id = _steam_id(steam_id)
        rows = await self.database.read(
            """
            SELECT item_key FROM inventory_monitor_item_history
            WHERE steam_id = ? AND last_missing_at IS NOT NULL
            """,
            (account_id,),
        )
        return frozenset(str(row["item_key"]) for row in rows)

    async def commit_snapshot(
        self,
        claim: InventoryAccountClaim,
        *,
        nickname: str,
        snapshot_json: str,
        snapshot_hash: str,
        source_time: int,
        items: Mapping[str, str],
        removed_item_keys: Sequence[str],
        message_text: str | None,
        anchors: Mapping[str, ItemAnchorValues] | None = None,
        reattach_keys: Sequence[tuple[str, str]] = (),
        exclude_delivery: tuple[int, DestinationType, int] | None = None,
        now: datetime | int | None = None,
    ) -> CommitResult:
        account_id = _steam_id(claim.account.steam_id)
        account_name = _bounded_text(nickname, "nickname", 100)
        payload = _snapshot_json(snapshot_json)
        digest = _hash(snapshot_hash, "snapshot_hash")
        source_timestamp = _epoch(source_time)
        timestamp = _epoch(now)
        item_rows = _item_rows(items)
        anchor_rows = _anchor_rows(anchors)
        removed = tuple(_bounded_text(key, "item_key", 200) for key in removed_item_keys)
        message = None
        if message_text is not None:
            message = _bounded_text(message_text, "message_text", MAX_MESSAGE_LENGTH)
        # Reattachment pairs: (brand-new item_key, original history item_key).
        reattachments = tuple(
            (
                _bounded_text(new_key, "item_key", 200),
                _bounded_text(original_key, "item_key", 200),
            )
            for new_key, original_key in reattach_keys
        )
        next_check = timestamp + DAILY_INTERVAL_SECONDS

        def commit(transaction: Transaction) -> CommitResult:
            rows = transaction.read(
                """
                SELECT * FROM inventory_monitor_accounts
                WHERE steam_id = ? AND lease_owner = ?
                """,
                (account_id, claim.lease_token),
            )
            if not rows:
                return CommitResult(False, None, 0)

            event_id: str | None = None
            queued = 0
            if message is not None:
                event_id = uuid.uuid4().hex
                transaction.write(
                    """
                    INSERT INTO inventory_monitor_events (
                        id, steam_id, previous_hash, new_hash,
                        message_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        account_id,
                        claim.account.snapshot_hash,
                        digest,
                        message,
                        timestamp,
                    ),
                )
                subscriptions = transaction.read(
                    """
                    SELECT owner_user_id, destination_type, destination_id
                    FROM inventory_monitor_subscriptions
                    WHERE steam_id = ?
                    """,
                    (account_id,),
                )
                for subscription in subscriptions:
                    destination_tuple = (
                        int(subscription["owner_user_id"]),
                        _destination_type(str(subscription["destination_type"])),
                        int(subscription["destination_id"]),
                    )
                    if exclude_delivery == destination_tuple:
                        continue
                    transaction.write(
                        """
                        INSERT INTO inventory_monitor_deliveries (
                            event_id, owner_user_id, destination_type,
                            destination_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            destination_tuple[0],
                            destination_tuple[1],
                            destination_tuple[2],
                            timestamp,
                            timestamp,
                        ),
                    )
                    queued += 1

            for item_key in removed:
                transaction.write(
                    """
                    UPDATE inventory_monitor_item_history
                    SET last_missing_at = ?
                    WHERE steam_id = ? AND item_key = ?
                        AND last_missing_at IS NULL
                    """,
                    (timestamp, account_id, item_key),
                )
            # Reattach brand-new keys onto their original missing history row:
            # the old row revives (missing cleared) and keeps its first_seen,
            # so one physical item never splits into two trajectory records.
            # Freshly resolved anchors ride along onto the revived row.
            for new_key, original_key in reattachments:
                anchor = anchor_rows.get(new_key, _EMPTY_ANCHORS)
                revived = transaction.write(
                    """
                    UPDATE inventory_monitor_item_history
                    SET last_missing_at = NULL, last_seen_at = ?,
                        market_hash_name = COALESCE(market_hash_name, ?),
                        paint_index = COALESCE(paint_index, ?),
                        paintwear = COALESCE(paintwear, ?),
                        pattern_index = COALESCE(pattern_index, ?),
                        stickers_json = COALESCE(stickers_json, ?)
                    WHERE steam_id = ? AND item_key = ?
                        AND last_missing_at IS NOT NULL
                    """,
                    (
                        timestamp,
                        *anchor,
                        account_id,
                        original_key,
                    ),
                )
                if revived.rowcount == 1:
                    transaction.write(
                        "DELETE FROM inventory_monitor_item_history "
                        "WHERE steam_id = ? AND item_key = ?",
                        (account_id, new_key),
                    )
            # One multi-row upsert per chunk keeps statement count flat in the
            # number of items (up to 20k rows per snapshot); the IN-list and
            # VALUES placeholders are positional ``?`` markers only.  Anchor
            # columns keep their previous value when a fresh round has none.
            # Reattached sources are absent here on purpose: their physical
            # item already lives on in the revived original history row.
            reattached_sources = {new_key for new_key, _original in reattachments}
            persist_rows = [
                (key, payload_text)
                for key, payload_text in item_rows
                if key not in reattached_sources
            ]
            for chunk_start in range(0, len(persist_rows), _ITEM_INSERT_CHUNK):
                chunk = persist_rows[chunk_start : chunk_start + _ITEM_INSERT_CHUNK]
                value_rows = ",".join("(?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)" for _ in chunk)
                parameters: list[tuple[object, ...]] = []
                for item_key, item_json in chunk:
                    parameters.append((account_id, item_key, item_json, timestamp, timestamp))
                    parameters.append(anchor_rows.get(item_key, _EMPTY_ANCHORS))
                transaction.write(
                    f"""
                    INSERT INTO inventory_monitor_item_history (
                        steam_id, item_key, item_json,
                        first_seen_at, last_seen_at, last_missing_at,
                        market_hash_name, paint_index, paintwear,
                        pattern_index, stickers_json
                    ) VALUES {value_rows}
                    ON CONFLICT (steam_id, item_key) DO UPDATE SET
                        item_json = excluded.item_json,
                        last_seen_at = excluded.last_seen_at,
                        last_missing_at = NULL,
                        market_hash_name =
                            COALESCE(inventory_monitor_item_history.market_hash_name,
                                     excluded.market_hash_name),
                        paint_index =
                            COALESCE(inventory_monitor_item_history.paint_index,
                                     excluded.paint_index),
                        paintwear =
                            COALESCE(inventory_monitor_item_history.paintwear,
                                     excluded.paintwear),
                        pattern_index =
                            COALESCE(inventory_monitor_item_history.pattern_index,
                                     excluded.pattern_index),
                        stickers_json =
                            COALESCE(inventory_monitor_item_history.stickers_json,
                                     excluded.stickers_json)
                    """,  # noqa: S608 - placeholder scaffolding is a fixed constant
                    tuple(p for group in parameters for p in group),
                )

            updated = transaction.write(
                """
                UPDATE inventory_monitor_accounts
                SET nickname = ?, snapshot_json = ?, snapshot_hash = ?,
                    source_time = ?, last_success_at = ?, next_check_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = NULL, consecutive_failures = 0, updated_at = ?
                WHERE steam_id = ? AND lease_owner = ?
                """,
                (
                    account_name,
                    payload,
                    digest,
                    source_timestamp,
                    timestamp,
                    next_check,
                    timestamp,
                    account_id,
                    claim.lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise InventoryMonitorRepositoryError("inventory account lease was lost")
            return CommitResult(True, event_id, queued)

        return await self.database.transaction(commit)

    async def record_account_failure(
        self,
        claim: InventoryAccountClaim,
        error: str,
        *,
        now: datetime | int | None = None,
    ) -> bool:
        account_id = _steam_id(claim.account.steam_id)
        timestamp = _epoch(now)
        safe_error = str(error).strip()[:MAX_ERROR_LENGTH] or "InventoryCheckFailed"

        def record(transaction: Transaction) -> bool:
            rows = transaction.read(
                """
                SELECT consecutive_failures FROM inventory_monitor_accounts
                WHERE steam_id = ? AND lease_owner = ?
                """,
                (account_id, claim.lease_token),
            )
            if not rows:
                return False
            failures = int(rows[0]["consecutive_failures"]) + 1
            if failures >= ACCOUNT_MAX_CONSECUTIVE_FAILURES:
                # A permanently broken account must not hit the upstream on
                # every poll cycle; park it for a day (retry backoff ceiling).
                next_check = timestamp + ACCOUNT_DISABLED_PARK_SECONDS
            else:
                # One transient error must not punish the owner with a full-day
                # gap: retry quickly, backing off exponentially per failure.
                next_check = timestamp + min(
                    ACCOUNT_FAILURE_RETRY_SECONDS * (2 ** (failures - 1)),
                    ACCOUNT_FAILURE_BACKOFF_CAP_SECONDS,
                )
            result = transaction.write(
                """
                UPDATE inventory_monitor_accounts
                SET lease_owner = NULL, lease_expires_at = NULL,
                    consecutive_failures = ?, last_error = ?,
                    next_check_at = ?, updated_at = ?
                WHERE steam_id = ? AND lease_owner = ?
                """,
                (
                    failures,
                    safe_error,
                    next_check,
                    timestamp,
                    account_id,
                    claim.lease_token,
                ),
            )
            return result.rowcount == 1

        return await self.database.transaction(record)

    async def claim_deliveries(
        self,
        limit: int,
        lease_seconds: int,
        *,
        now: datetime | int | None = None,
    ) -> tuple[InventoryDelivery, ...]:
        if not 1 <= limit <= 1_000 or lease_seconds <= 0:
            raise ValueError("delivery claim settings are invalid")
        timestamp = _epoch(now)
        expires_at = timestamp + lease_seconds

        def claim(transaction: Transaction) -> tuple[InventoryDelivery, ...]:
            candidates = transaction.read(
                """
                SELECT d.*, e.message_text
                FROM inventory_monitor_deliveries AS d
                JOIN inventory_monitor_events AS e ON e.id = d.event_id
                WHERE
                    (d.status = 'pending'
                        AND (d.next_retry_at IS NULL OR d.next_retry_at <= ?))
                    OR (d.status = 'claimed' AND d.lease_expires_at <= ?)
                ORDER BY COALESCE(d.next_retry_at, d.created_at), d.event_id
                LIMIT ?
                """,
                (timestamp, timestamp, limit),
            )
            deliveries: list[InventoryDelivery] = []
            for candidate in candidates:
                attempts = int(candidate["attempts"]) + 1
                identity = (
                    candidate["event_id"],
                    candidate["owner_user_id"],
                    candidate["destination_type"],
                    candidate["destination_id"],
                )
                if attempts > self.max_attempts:
                    transaction.write(
                        """
                        UPDATE inventory_monitor_deliveries
                        SET status = 'failed', attempts = ?, next_retry_at = NULL,
                            lease_owner = NULL, lease_expires_at = NULL,
                            last_error = COALESCE(last_error, 'claim lease expired'),
                            failed_at = ?, updated_at = ?
                        WHERE event_id = ? AND owner_user_id = ?
                            AND destination_type = ? AND destination_id = ?
                            AND (
                                status = 'pending'
                                OR (status = 'claimed' AND lease_expires_at <= ?)
                            )
                        """,
                        (
                            attempts,
                            timestamp,
                            timestamp,
                            *identity,
                            timestamp,
                        ),
                    )
                    continue
                token = uuid.uuid4().hex
                updated = transaction.write(
                    """
                    UPDATE inventory_monitor_deliveries
                    SET status = 'claimed', attempts = ?, lease_owner = ?,
                        lease_expires_at = ?, next_retry_at = NULL,
                        updated_at = ?
                    WHERE event_id = ? AND owner_user_id = ?
                        AND destination_type = ? AND destination_id = ?
                        AND (
                            status = 'pending'
                            OR (status = 'claimed' AND lease_expires_at <= ?)
                        )
                    """,
                    (attempts, token, expires_at, timestamp, *identity, timestamp),
                )
                if updated.rowcount != 1:
                    continue
                deliveries.append(
                    InventoryDelivery(
                        event_id=str(candidate["event_id"]),
                        owner_user_id=int(candidate["owner_user_id"]),
                        destination_type=_destination_type(str(candidate["destination_type"])),
                        destination_id=int(candidate["destination_id"]),
                        message_text=str(candidate["message_text"]),
                        attempts=attempts,
                        lease_token=token,
                    )
                )
            return tuple(deliveries)

        return await self.database.transaction(claim)

    async def mark_delivery(
        self,
        delivery: InventoryDelivery,
        message_id: str | int | None,
        *,
        now: datetime | int | None = None,
    ) -> bool:
        timestamp = _epoch(now)
        identifier = str(message_id) if message_id is not None else None
        if identifier is not None and len(identifier) > MAX_MESSAGE_ID_LENGTH:
            identifier = identifier[:MAX_MESSAGE_ID_LENGTH]
        result = await self.database.write(
            """
            UPDATE inventory_monitor_deliveries
            SET status = 'delivered', lease_owner = NULL,
                lease_expires_at = NULL, delivered_at = ?,
                message_id = ?, last_error = NULL, updated_at = ?
            WHERE event_id = ? AND owner_user_id = ?
                AND destination_type = ? AND destination_id = ?
                AND status = 'claimed' AND lease_owner = ?
            """,
            (
                timestamp,
                identifier,
                timestamp,
                delivery.event_id,
                delivery.owner_user_id,
                delivery.destination_type,
                delivery.destination_id,
                delivery.lease_token,
            ),
        )
        return result.rowcount == 1

    async def record_delivery_failure(
        self,
        delivery: InventoryDelivery,
        error: str,
        *,
        now: datetime | int | None = None,
    ) -> bool:
        timestamp = _epoch(now)
        safe_error = str(error).strip()[:MAX_ERROR_LENGTH] or "QQSendFailed"
        if delivery.attempts >= self.max_attempts:
            status = "failed"
            next_retry_at = None
            failed_at: int | None = timestamp
        else:
            status = "pending"
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, delivery.attempts - 1)),
            )
            next_retry_at = timestamp + delay
            failed_at = None
        result = await self.database.write(
            """
            UPDATE inventory_monitor_deliveries
            SET status = ?, next_retry_at = ?, lease_owner = NULL,
                lease_expires_at = NULL, last_error = ?, failed_at = ?,
                updated_at = ?
            WHERE event_id = ? AND owner_user_id = ?
                AND destination_type = ? AND destination_id = ?
                AND status = 'claimed' AND lease_owner = ?
            """,
            (
                status,
                next_retry_at,
                safe_error,
                failed_at,
                timestamp,
                delivery.event_id,
                delivery.owner_user_id,
                delivery.destination_type,
                delivery.destination_id,
                delivery.lease_token,
            ),
        )
        return result.rowcount == 1

    async def item_history_anchors(
        self, steam_id: str
    ) -> dict[str, tuple[str | None, float | None, bool]]:
        """Return ``(market_hash_name, paintwear, is_missing)`` per history key."""

        account_id = _steam_id(steam_id)
        rows = await self.database.read(
            """
            SELECT item_key, market_hash_name, paintwear, last_missing_at
            FROM inventory_monitor_item_history
            WHERE steam_id = ?
            """,
            (account_id,),
        )
        return {
            str(row["item_key"]): (
                str(row["market_hash_name"]) if row["market_hash_name"] is not None else None,
                float(row["paintwear"]) if row["paintwear"] is not None else None,
                row["last_missing_at"] is not None,
            )
            for row in rows
        }

    async def trajectory_snapshot(
        self,
        steam_id: str,
        *,
        since: int,
        limit: int = 5_000,
    ) -> tuple[str, tuple[InventoryHistoryTrajectoryRow, ...]] | None:
        """Project one account's history for aggregation (owner-filtered upstream)."""

        account_id = _steam_id(steam_id)
        bounded_limit = max(1, min(int(limit), 20_000))
        accounts = await self.database.read(
            "SELECT nickname FROM inventory_monitor_accounts WHERE steam_id = ?",
            (account_id,),
        )
        if not accounts:
            return None
        rows = await self.database.read(
            """
            SELECT item_key, item_json, market_hash_name, paintwear,
                   first_seen_at, last_seen_at, last_missing_at
            FROM inventory_monitor_item_history
            WHERE steam_id = ?
            ORDER BY first_seen_at DESC, item_key
            LIMIT ?
            """,
            (account_id, bounded_limit),
        )
        projected: list[InventoryHistoryTrajectoryRow] = []
        for row in rows:
            fallback_name = "未知饰品"
            try:
                payload = json.loads(str(row["item_json"]))
                if isinstance(payload, Mapping):
                    fallback_name = str(payload.get("name") or fallback_name)[:180]
            except (TypeError, ValueError):
                pass
            wear = row["paintwear"]
            projected.append(
                InventoryHistoryTrajectoryRow(
                    item_key=str(row["item_key"]),
                    name=fallback_name,
                    market_hash_name=(
                        str(row["market_hash_name"])
                        if row["market_hash_name"] is not None
                        else None
                    ),
                    paintwear=float(wear) if wear is not None else None,
                    first_seen_at=int(row["first_seen_at"]),
                    last_seen_at=int(row["last_seen_at"]),
                    last_missing_at=(
                        int(row["last_missing_at"]) if row["last_missing_at"] is not None else None
                    ),
                )
            )
        return str(accounts[0]["nickname"]), tuple(projected)

    async def load_float_cache(
        self,
        hashes: Sequence[str],
    ) -> dict[str, ItemFloatAnchors]:
        if not hashes:
            return {}
        digests = [
            digest.strip().casefold()
            for digest in hashes[:_MAX_FLOAT_CACHE_BATCH]
            if len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest.lower())
        ]
        if not digests:
            return {}
        placeholders = ",".join("?" for _ in digests)
        rows = await self.database.read(
            f"""
            SELECT inspect_url_hash, market_hash_name, paintwear, paint_index,
                   pattern_index, stickers_json
            FROM inventory_monitor_float_cache
            WHERE inspect_url_hash IN ({placeholders})
            """,  # noqa: S608 - placeholders are positional ``?`` markers only
            tuple(digests),
        )
        results: dict[str, ItemFloatAnchors] = {}
        for row in rows:
            stickers_raw = row["stickers_json"]
            try:
                stickers = tuple(json.loads(stickers_raw)) if stickers_raw else ()
            except (TypeError, ValueError):
                stickers = ()
            results[str(row["inspect_url_hash"])] = ItemFloatAnchors(
                paintwear=float(row["paintwear"]),
                market_hash_name=(
                    str(row["market_hash_name"]) if row["market_hash_name"] is not None else None
                ),
                paint_index=int(row["paint_index"]) if row["paint_index"] is not None else None,
                pattern_index=(
                    int(row["pattern_index"]) if row["pattern_index"] is not None else None
                ),
                stickers=stickers,
            )
        return results

    async def save_float_cache(
        self,
        entries: Mapping[str, tuple[ItemFloatAnchors, int]],
    ) -> int:
        if not entries:
            return 0

        def persist(transaction: Transaction) -> int:
            written = 0
            for digest, (anchors, fetched_at) in entries.items():
                normalized = digest.strip().casefold()
                if len(normalized) != 64 or any(
                    character not in "0123456789abcdef" for character in normalized
                ):
                    continue
                transaction.write(
                    """
                    INSERT INTO inventory_monitor_float_cache (
                        inspect_url_hash, market_hash_name, paintwear,
                        paint_index, pattern_index, stickers_json,
                        fetched_at, http_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (inspect_url_hash) DO UPDATE SET
                        market_hash_name = excluded.market_hash_name,
                        paintwear = excluded.paintwear,
                        paint_index = excluded.paint_index,
                        pattern_index = excluded.pattern_index,
                        stickers_json = excluded.stickers_json,
                        fetched_at = excluded.fetched_at,
                        http_status = excluded.http_status
                    """,
                    (
                        normalized,
                        anchors.market_hash_name or "",
                        float(anchors.paintwear),
                        anchors.paint_index,
                        anchors.pattern_index,
                        serialize_stickers(anchors.stickers) if anchors.stickers else None,
                        _epoch(fetched_at),
                        200,
                    ),
                )
                written += 1
            return written

        return await self.database.transaction(persist)

    async def cleanup_events(self, *, before: int, limit: int = 1_000) -> int:
        """Delete old events only after every delivery reached a terminal state."""

        timestamp = _epoch(before)
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await self.database.write(
            """
            DELETE FROM inventory_monitor_events
            WHERE id IN (
                SELECT e.id
                FROM inventory_monitor_events AS e
                WHERE e.created_at < ?
                    AND NOT EXISTS (
                        SELECT 1 FROM inventory_monitor_deliveries AS d
                        WHERE d.event_id = e.id
                            AND d.status IN ('pending', 'claimed')
                    )
                ORDER BY e.created_at, e.id
                LIMIT ?
            )
            """,
            (timestamp, limit),
        )
        return max(0, result.rowcount)

    async def cleanup_item_history(self, *, before: int, limit: int = 1_000) -> int:
        """Forget long-missing fingerprints after the configured inference window."""

        timestamp = _epoch(before)
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        result = await self.database.write(
            """
            DELETE FROM inventory_monitor_item_history
            WHERE (steam_id, item_key) IN (
                SELECT steam_id, item_key
                FROM inventory_monitor_item_history
                WHERE last_missing_at IS NOT NULL
                    AND last_missing_at < ? AND last_seen_at < ?
                ORDER BY last_missing_at, steam_id, item_key
                LIMIT ?
            )
            """,
            (timestamp, timestamp, limit),
        )
        return max(0, result.rowcount)


__all__ = [
    "ACCOUNT_FAILURE_RETRY_SECONDS",
    "CommitResult",
    "DAILY_INTERVAL_SECONDS",
    "DestinationType",
    "InventoryAccountClaim",
    "InventoryDelivery",
    "InventoryHistoryTrajectoryRow",
    "InventoryMonitorAccount",
    "InventoryMonitorRepository",
    "InventoryMonitorSubscription",
    "ItemAnchorValues",
    "MAX_MONITORS_PER_OWNER",
    "MAX_TOTAL_MONITORED_ACCOUNTS",
    "MAX_TOTAL_SUBSCRIPTIONS",
    "SubscribeResult",
]

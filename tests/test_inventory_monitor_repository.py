from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.state.inventory_monitor_repository import (
    ACCOUNT_DISABLED_PARK_SECONDS,
    ACCOUNT_FAILURE_BACKOFF_CAP_SECONDS,
    ACCOUNT_FAILURE_RETRY_SECONDS,
    ACCOUNT_MAX_CONSECUTIVE_FAILURES,
    InventoryMonitorRepository,
)


def _item(name: str) -> str:
    return json.dumps({"name": name}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_subscriptions_share_one_account_and_check_only_once_per_day(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    first = await repository.subscribe(
        101,
        "group",
        9001,
        steam_id="76561198000000000",
        nickname="first",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("one")},
        now=100,
    )
    second = await repository.subscribe(
        202,
        "private",
        202,
        steam_id="76561198000000000",
        nickname="new-name-must-not-reset-baseline",
        snapshot_json='{"version":2}',
        snapshot_hash="b" * 64,
        source_time=99,
        items={"asset:2": _item("two")},
        now=200,
    )

    assert first.created and second.created
    accounts = await repository.database.read("SELECT * FROM inventory_monitor_accounts")
    assert len(accounts) == 1
    assert accounts[0]["nickname"] == "first"
    assert accounts[0]["snapshot_hash"] == "a" * 64
    assert await repository.claim_due_accounts(10, 60, now=86_499) == ()
    claims = await repository.claim_due_accounts(10, 60, now=86_500)
    assert len(claims) == 1
    assert claims[0].account.steam_id == "76561198000000000"


@pytest.mark.asyncio
async def test_commit_creates_durable_deliveries_and_tracks_missing_history(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    for owner, kind, destination in ((101, "group", 9001), (202, "private", 202)):
        await repository.subscribe(
            owner,
            kind,
            destination,
            steam_id="76561198000000000",
            nickname="account",
            snapshot_json='{"version":1}',
            snapshot_hash="a" * 64,
            source_time=90,
            items={"asset:1": _item("one")},
            now=100,
        )
    claim = await repository.claim_account("76561198000000000", 60, now=200)
    assert claim is not None
    committed = await repository.commit_snapshot(
        claim,
        nickname="account",
        snapshot_json='{"version":1,"changed":true}',
        snapshot_hash="b" * 64,
        source_time=190,
        items={"asset:2": _item("two")},
        removed_item_keys=("asset:1",),
        message_text="inventory changed",
        exclude_delivery=(101, "group", 9001),
        now=200,
    )

    assert committed.applied
    assert committed.queued_deliveries == 1
    assert await repository.missing_item_keys("76561198000000000") == frozenset({"asset:1"})
    deliveries = await repository.claim_deliveries(10, 60, now=200)
    assert len(deliveries) == 1
    assert deliveries[0].owner_user_id == 202
    assert deliveries[0].message_text == "inventory changed"
    assert await repository.mark_delivery(deliveries[0], 88, now=201)
    rows = await repository.database.read(
        "SELECT status, message_id FROM inventory_monitor_deliveries"
    )
    assert rows == [{"status": "delivered", "message_id": "88"}]


@pytest.mark.asyncio
async def test_failed_check_never_overwrites_the_last_valid_baseline(tmp_path: Path) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    await repository.subscribe(
        101,
        "private",
        101,
        steam_id="76561198000000000",
        nickname="account",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("one")},
        now=100,
    )
    claim = await repository.claim_account("76561198000000000", 60, now=200)
    assert claim is not None

    assert await repository.record_account_failure(claim, "upstream empty", now=200)
    rows = await repository.database.read(
        """
        SELECT snapshot_hash, source_time, last_error, next_check_at
        FROM inventory_monitor_accounts
        """
    )
    assert rows == [
        {
            "snapshot_hash": "a" * 64,
            "source_time": 90,
            "last_error": "upstream empty",
            "next_check_at": 200 + ACCOUNT_FAILURE_RETRY_SECONDS,
        }
    ]


@pytest.mark.asyncio
async def test_unsubscribe_removes_shared_history_only_after_last_subscription(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    for owner in (101, 202):
        await repository.subscribe(
            owner,
            "private",
            owner,
            steam_id="76561198000000000",
            nickname="account",
            snapshot_json='{"version":1}',
            snapshot_hash="a" * 64,
            source_time=90,
            items={"asset:1": _item("one")},
            now=100,
        )

    assert await repository.unsubscribe(101, "private", 101, "76561198000000000")
    assert len(await repository.database.read("SELECT * FROM inventory_monitor_accounts")) == 1
    assert await repository.unsubscribe(202, "private", 202, "76561198000000000")
    assert await repository.database.read("SELECT * FROM inventory_monitor_accounts") == []
    assert await repository.database.read("SELECT * FROM inventory_monitor_item_history") == []


@pytest.mark.asyncio
async def test_lists_and_permissions_are_isolated_by_chat_destination(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    subscriptions = (
        ("private", 101, "76561198000000001"),
        ("group", 9001, "76561198000000002"),
        ("group", 9002, "76561198000000003"),
    )
    for kind, destination, steam_id in subscriptions:
        await repository.subscribe(
            101,
            kind,
            destination,
            steam_id=steam_id,
            nickname=steam_id[-2:],
            snapshot_json='{"version":1}',
            snapshot_hash=steam_id[-1] * 64,
            source_time=100,
            items={f"asset:{steam_id}": _item(steam_id)},
            now=100,
        )

    private = await repository.list_destination(101, "private", 101)
    first_group = await repository.list_destination(101, "group", 9001)
    second_group = await repository.list_destination(101, "group", 9002)

    assert [item.steam_id for item in private] == ["76561198000000001"]
    assert [item.steam_id for item in first_group] == ["76561198000000002"]
    assert [item.steam_id for item in second_group] == ["76561198000000003"]
    assert await repository.has_subscription(101, "private", 101, "76561198000000001")
    assert not await repository.has_subscription(101, "group", 9001, "76561198000000001")


@pytest.mark.asyncio
async def test_global_account_capacity_still_allows_shared_accounts(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(
        StateDatabase(tmp_path / "state.db"),
        max_total_accounts=1,
        max_total_subscriptions=10,
    )
    await repository.initialize()
    first = await repository.subscribe(
        101,
        "private",
        101,
        steam_id="76561198000000001",
        nickname="first",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("one")},
        now=100,
    )
    shared = await repository.subscribe(
        202,
        "private",
        202,
        steam_id="76561198000000001",
        nickname="shared",
        snapshot_json='{"version":2}',
        snapshot_hash="b" * 64,
        source_time=100,
        items={"asset:2": _item("two")},
        now=110,
    )
    rejected = await repository.subscribe(
        303,
        "private",
        303,
        steam_id="76561198000000002",
        nickname="second",
        snapshot_json='{"version":1}',
        snapshot_hash="c" * 64,
        source_time=100,
        items={"asset:3": _item("three")},
        now=120,
    )

    assert first.created and shared.created
    assert not shared.global_quota_exceeded
    assert not rejected.created and rejected.global_quota_exceeded
    assert len(await repository.database.read("SELECT * FROM inventory_monitor_accounts")) == 1


@pytest.mark.asyncio
async def test_global_subscription_capacity_is_enforced(tmp_path: Path) -> None:
    repository = InventoryMonitorRepository(
        StateDatabase(tmp_path / "state.db"),
        max_total_accounts=10,
        max_total_subscriptions=1,
    )
    await repository.initialize()
    await repository.subscribe(
        101,
        "private",
        101,
        steam_id="76561198000000001",
        nickname="first",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("one")},
        now=100,
    )
    rejected = await repository.subscribe(
        202,
        "private",
        202,
        steam_id="76561198000000001",
        nickname="shared",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("one")},
        now=101,
    )

    assert not rejected.created and rejected.global_quota_exceeded


@pytest.mark.asyncio
async def test_retention_keeps_undelivered_events_and_forgets_old_missing_items(
    tmp_path: Path,
) -> None:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    await repository.subscribe(
        101,
        "private",
        101,
        steam_id="76561198000000001",
        nickname="first",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:missing": _item("missing"), "asset:active": _item("active")},
        now=100,
    )
    for event_id in ("pending-event", "delivered-event"):
        await repository.database.write(
            """
            INSERT INTO inventory_monitor_events (
                id, steam_id, previous_hash, new_hash, message_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                "76561198000000001",
                "a" * 64,
                "b" * 64,
                "changed",
                100,
            ),
        )
    await repository.database.write(
        """
        INSERT INTO inventory_monitor_deliveries (
            event_id, owner_user_id, destination_type, destination_id,
            status, attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
        """,
        ("pending-event", 101, "private", 101, 100, 100),
    )
    await repository.database.write(
        """
        INSERT INTO inventory_monitor_deliveries (
            event_id, owner_user_id, destination_type, destination_id,
            status, attempts, created_at, updated_at, delivered_at
        ) VALUES (?, ?, ?, ?, 'delivered', 1, ?, ?, ?)
        """,
        ("delivered-event", 101, "private", 101, 100, 100, 110),
    )
    await repository.database.write(
        """
        UPDATE inventory_monitor_item_history
        SET last_seen_at = 100, last_missing_at = 100
        WHERE item_key = 'asset:missing'
        """
    )

    assert await repository.cleanup_events(before=200) == 1
    assert await repository.cleanup_item_history(before=200) == 1
    events = await repository.database.read("SELECT id FROM inventory_monitor_events ORDER BY id")
    history = await repository.database.read(
        "SELECT item_key FROM inventory_monitor_item_history ORDER BY item_key"
    )
    assert events == [{"id": "pending-event"}]
    assert history == [{"item_key": "asset:active"}]


@pytest.mark.asyncio
async def test_repeated_account_failures_back_off_then_park(tmp_path: Path) -> None:
    """A19 regression: broken accounts back off exponentially, then park."""

    repository = InventoryMonitorRepository(StateDatabase(tmp_path / "state19.db"))
    await repository.initialize()
    steam_id = "76561198000000000"
    await repository.subscribe(
        101,
        "private",
        101,
        steam_id=steam_id,
        nickname="account",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": _item("one")},
        now=100,
    )

    now = 200
    for attempt in range(1, ACCOUNT_MAX_CONSECUTIVE_FAILURES + 1):
        claim = await repository.claim_account(steam_id, 60, now=now)
        assert claim is not None
        assert await repository.record_account_failure(claim, "still broken", now=now)
        rows = await repository.database.read(
            "SELECT consecutive_failures, next_check_at FROM inventory_monitor_accounts"
        )
        assert rows[0]["consecutive_failures"] == attempt
        if attempt < ACCOUNT_MAX_CONSECUTIVE_FAILURES:
            expected_backoff = min(
                ACCOUNT_FAILURE_RETRY_SECONDS * (2 ** (attempt - 1)),
                ACCOUNT_FAILURE_BACKOFF_CAP_SECONDS,
            )
            assert rows[0]["next_check_at"] == now + expected_backoff
            now = int(rows[0]["next_check_at"])
        else:
            # The account is parked for a full day instead of being retried.
            assert rows[0]["next_check_at"] == now + ACCOUNT_DISABLED_PARK_SECONDS

    # A successful snapshot clears the counter and resumes normal polling.
    claim = await repository.claim_account(steam_id, 60, now=now + ACCOUNT_DISABLED_PARK_SECONDS)
    assert claim is not None
    committed = await repository.commit_snapshot(
        claim,
        nickname="account",
        snapshot_json='{"version":2}',
        snapshot_hash="b" * 64,
        source_time=now + ACCOUNT_DISABLED_PARK_SECONDS - 60,
        items={"asset:1": _item("one")},
        removed_item_keys=(),
        message_text=None,
        now=now + ACCOUNT_DISABLED_PARK_SECONDS,
    )
    assert committed.applied is True
    rows = await repository.database.read(
        "SELECT consecutive_failures, last_error FROM inventory_monitor_accounts"
    )
    assert rows[0]["consecutive_failures"] == 0
    assert rows[0]["last_error"] is None

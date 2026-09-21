from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.database import StateDatabase
from src.state.alert_repository import (
    LEGACY_SOURCE_NAME,
    AlertRepository,
    LegacyAlertMigrationError,
    price_to_cents,
)


async def _repository(tmp_path: Path, name: str = "state.db") -> AlertRepository:
    repository = AlertRepository(StateDatabase(tmp_path / name), legacy_path=None)
    assert await repository.initialize() is None
    return repository


def test_price_conversion_is_decimal_and_half_up() -> None:
    assert price_to_cents(Decimal("1.005")) == 101
    assert price_to_cents("99.994") == 9_999
    assert price_to_cents(0.1) == 10
    for invalid in (True, 0, -1, "NaN", float("inf"), "0.004"):
        with pytest.raises(ValueError):
            price_to_cents(invalid)


@pytest.mark.asyncio
async def test_create_list_admin_page_and_owner_scoped_cancel(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    first = await repository.create(101, "first", "12.345", group_id="9001", now=100)
    other = await repository.create(202, "other", 20, now=101)
    second = await repository.create(101, "second", 30, direction="below", now=102)

    assert first.created and first.alert is not None
    assert first.alert.target_price_cents == 1_235
    assert first.alert.direction == "above"
    assert first.alert.group_id == 9001
    assert other.created and second.created
    assert second.alert is not None and second.alert.direction == "below"
    assert [item.market_name for item in await repository.list_owner(101)] == [
        "first",
        "second",
    ]

    removed = await repository.cancel_at(101, 0)
    assert removed.applied and removed.alert is not None
    assert removed.alert.market_name == "first"
    assert [item.market_name for item in await repository.list_owner(101)] == ["second"]
    assert [item.market_name for item in await repository.list_owner(202)] == ["other"]
    assert not (await repository.cancel_at(101, 99)).applied

    page = await repository.admin_page(offset=0, limit=1)
    assert page.total == 2
    assert len(page.items) == 1
    owner_page = await repository.admin_page(owner_user_id=202, status="pending")
    assert owner_page.total == 1
    assert owner_page.items[0].owner_user_id == 202

    with pytest.raises(ValueError, match="direction"):
        await repository.create(101, "invalid", 10, direction="sideways")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_owner_quota_is_atomic_across_repository_instances(tmp_path: Path) -> None:
    path = tmp_path / "quota.db"
    first = AlertRepository(StateDatabase(path), legacy_path=None)
    second = AlertRepository(StateDatabase(path), legacy_path=None)
    await asyncio.gather(first.initialize(), second.initialize())

    results = await asyncio.gather(
        *(
            (first if index % 2 else second).create(
                101,
                f"item-{index}",
                index + 1,
                now=index,
            )
            for index in range(60)
        )
    )

    assert sum(result.created for result in results) == 20
    assert sum(result.quota_exceeded for result in results) == 40
    assert len(await first.list_owner(101)) == 20


@pytest.mark.asyncio
async def test_owner_quota_counts_only_active_alerts(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    for index in range(20):
        assert (await repository.create(101, f"item-{index}", 10, now=index)).created

    # Delivered and dead-lettered history must not block new active alerts.
    await repository.database.write(
        """
        UPDATE alerts
        SET status = 'delivered', next_retry_at = NULL, delivered_at = 1
        WHERE owner_user_id = 101
        """
    )
    fresh = await repository.create(101, "fresh-after-history", 20, now=100)

    assert fresh.created and fresh.alert is not None
    assert fresh.alert.status == "pending"


@pytest.mark.asyncio
async def test_claim_is_atomic_across_instances_and_tokens_are_unique(tmp_path: Path) -> None:
    path = tmp_path / "claims.db"
    first = AlertRepository(StateDatabase(path), legacy_path=None)
    second = AlertRepository(StateDatabase(path), legacy_path=None)
    await asyncio.gather(first.initialize(), second.initialize())
    for index in range(8):
        assert (await first.create(index + 1, f"item-{index}", 10, now=index)).created

    claimed_a, claimed_b = await asyncio.gather(
        first.claim_due(8, 30, now=100),
        second.claim_due(8, 30, now=100),
    )
    all_claimed = (*claimed_a, *claimed_b)

    assert sorted((len(claimed_a), len(claimed_b))) == [0, 8]
    assert len(all_claimed) == 8
    assert len({alert.id for alert in all_claimed}) == 8
    assert len({alert.lease_token for alert in all_claimed}) == 8
    assert all(alert.status == "claimed" for alert in all_claimed)
    assert await first.claim_due(8, 30, now=100) == ()


@pytest.mark.asyncio
async def test_expired_claim_recovery_invalidates_old_token(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    created = await repository.create(101, "recover", 100, now=1)
    assert created.alert is not None
    first_claim = (await repository.claim_due(1, 10, now=100))[0]
    assert first_claim.lease_token is not None

    assert await repository.claim_due(1, 10, now=109) == ()
    recovered = (await repository.claim_due(1, 10, now=110))[0]
    assert recovered.lease_token is not None
    assert recovered.lease_token != first_claim.lease_token
    assert recovered.attempts == 0
    assert recovered.evaluation_failures == 1
    assert recovered.last_evaluation_error == "previous claim lease expired"

    stale = await repository.mark_delivered(
        recovered.id,
        first_claim.lease_token,
        delivered_at=111,
    )
    assert not stale.applied
    committed = await repository.mark_delivered(
        recovered.id,
        recovered.lease_token,
        observed_price_cents=10_001,
        message_id=88,
        delivered_at=111,
    )
    assert committed.applied and committed.alert is not None
    assert committed.alert.status == "delivered"
    assert committed.alert.message_id == "88"
    assert committed.alert.last_observed_price_cents == 10_001


@pytest.mark.asyncio
async def test_claim_due_prioritizes_never_checked_alerts_over_later_rechecks(
    tmp_path: Path,
) -> None:
    repository = await _repository(tmp_path)
    for owner, created_at in ((101, 1), (202, 2), (303, 3)):
        assert (await repository.create(owner, f"item-{owner}", 100, now=created_at)).created

    first_batch = await repository.claim_due(2, 10, now=100)
    assert [item.owner_user_id for item in first_batch] == [101, 202]
    for item in first_batch:
        assert item.lease_token is not None
        deferred = await repository.defer_below_target(
            item.id,
            item.lease_token,
            1,
            next_check_at=130,
            now=100,
        )
        assert deferred.applied

    next_claim = await repository.claim_due(1, 10, now=130)
    assert [item.owner_user_id for item in next_claim] == [303]


@pytest.mark.asyncio
async def test_expired_claim_is_an_evaluation_failure_not_a_dead_letter(tmp_path: Path) -> None:
    repository = AlertRepository(
        StateDatabase(tmp_path / "expired-dead-letter.db"),
        legacy_path=None,
        max_attempts=1,
    )
    await repository.initialize()
    created = await repository.create(101, "crash-window", 100, now=1)
    assert created.alert is not None
    claimed = (await repository.claim_due(1, 5, now=100))[0]
    assert claimed.lease_token is not None

    recovered = (await repository.claim_due(1, 5, now=105))[0]
    assert recovered.id == claimed.id
    assert recovered.attempts == 0
    assert recovered.evaluation_failures == 1
    assert recovered.failed_at is None
    assert recovered.last_evaluation_error == "previous claim lease expired"
    assert (await repository.admin_page(status="failed")).total == 0
    assert not (
        await repository.mark_delivered(
            claimed.id,
            claimed.lease_token,
            delivered_at=106,
        )
    ).applied


@pytest.mark.asyncio
async def test_concurrent_expired_claim_recovery_records_one_evaluation_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-expiry.db"
    first = AlertRepository(StateDatabase(path), legacy_path=None)
    second = AlertRepository(StateDatabase(path), legacy_path=None)
    await asyncio.gather(first.initialize(), second.initialize())
    await first.create(101, "recover-once", 100, now=1)
    original = (await first.claim_due(1, 5, now=100))[0]

    left, right = await asyncio.gather(
        first.claim_due(1, 5, now=105),
        second.claim_due(1, 5, now=105),
    )
    recovered = (*left, *right)
    assert len(recovered) == 1
    assert recovered[0].attempts == 0
    assert recovered[0].evaluation_failures == 1
    assert recovered[0].lease_token != original.lease_token


@pytest.mark.asyncio
async def test_defer_and_failure_require_the_current_claim_token(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    await repository.create(101, "retry", 100, now=1)
    claimed = (await repository.claim_due(1, 10, now=10))[0]
    assert claimed.lease_token is not None

    wrong = await repository.defer_below_target(
        claimed.id,
        "wrong-token",
        9_999,
        now=11,
    )
    assert not wrong.applied
    deferred = await repository.defer_below_target(
        claimed.id,
        claimed.lease_token,
        9_999,
        next_check_at=50,
        now=11,
    )
    assert deferred.applied and deferred.alert is not None
    assert deferred.alert.status == "pending"
    assert deferred.alert.next_retry_at == 50
    assert await repository.claim_due(1, 10, now=49) == ()

    retry_claim = (await repository.claim_due(1, 10, now=50))[0]
    assert retry_claim.lease_token is not None
    failed = await repository.record_failure(
        retry_claim.id,
        retry_claim.lease_token,
        "x" * 1_500,
        500,
        now=51,
    )
    assert failed.applied and failed.alert is not None
    assert failed.alert.attempts == 1
    assert failed.alert.next_retry_at == 500
    assert failed.alert.last_error == "x" * 1_000


@pytest.mark.asyncio
async def test_retry_budget_dead_letters_alert_and_owner_can_requeue_it(tmp_path: Path) -> None:
    repository = AlertRepository(
        StateDatabase(tmp_path / "alert-dead-letter.db"),
        legacy_path=None,
        max_attempts=1,
    )
    await repository.initialize()
    created = await repository.create(101, "permanent failure", 10, now=1)
    assert created.alert is not None
    claimed = (await repository.claim_due(1, 10, now=10))[0]
    assert claimed.lease_token is not None

    failed = await repository.record_failure(
        claimed.id,
        claimed.lease_token,
        "upstream unavailable",
        20,
        now=11,
    )
    assert failed.applied and failed.alert is not None
    assert failed.alert.status == "failed"
    assert failed.alert.failed_at == 11
    assert failed.alert.next_retry_at is None
    assert await repository.claim_due(1, 10, now=1_000) == ()

    wrong_owner = await repository.retry_failed(claimed.id, owner_user_id=202, now=30)
    assert not wrong_owner.applied
    retried = await repository.retry_failed(claimed.id, owner_user_id=101, now=30)
    assert retried.applied and retried.alert is not None
    assert retried.alert.status == "pending"
    assert retried.alert.attempts == 0


@pytest.mark.asyncio
async def test_alert_dead_letter_cleanup_is_bounded_by_age(tmp_path: Path) -> None:
    repository = AlertRepository(
        StateDatabase(tmp_path / "alert-dead-letter-cleanup.db"),
        legacy_path=None,
        max_attempts=1,
    )
    await repository.initialize()
    for owner, timestamp in ((101, 10), (202, 20)):
        await repository.create(owner, f"item-{owner}", 10, now=timestamp)
        claimed = (await repository.claim_due(1, 5, now=timestamp))[0]
        assert claimed.lease_token is not None
        await repository.record_failure(
            claimed.id,
            claimed.lease_token,
            "failed",
            timestamp + 10,
            now=timestamp + 1,
        )

    assert await repository.cleanup_failed(failed_before=20) == 1
    page = await repository.admin_page(status="failed")
    assert page.total == 1
    assert page.items[0].owner_user_id == 202


def _write_legacy(path: Path, entries: list[object]) -> bytes:
    payload = json.dumps(entries, ensure_ascii=False, allow_nan=True).encode()
    path.write_bytes(payload)
    return payload


@pytest.mark.asyncio
async def test_strict_legacy_migration_is_idempotent_and_source_is_unchanged(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "alerts.json"
    original = _write_legacy(
        legacy,
        [
            {
                "name": "AK-47 | Redline",
                "price": "100.005",
                "qq": "101",
                "gid": "9001",
                "time": "2026-07-11T00:00:00Z",
                "triggered": False,
            },
            {
                "id": "known-id",
                "name": "delivered",
                "price": 20,
                "owner_user_id": 202,
                "status": "delivered",
                "triggered": True,
                "created_at": "2026-07-10T00:00:00+00:00",
            },
        ],
    )
    repository = AlertRepository(StateDatabase(tmp_path / "migrated.db"), legacy_path=legacy)

    migrated = await repository.initialize()
    assert migrated is not None and migrated.state == "imported"
    assert migrated.imported_rows == 2
    assert legacy.read_bytes() == original
    first = (await repository.list_owner(101))[0]
    assert first.id.startswith("legacy-")
    assert first.target_price_cents == 10_001
    assert first.group_id == 9001
    delivered = (await repository.list_owner(202))[0]
    assert delivered.id == "known-id"
    assert delivered.status == "delivered"
    assert delivered.delivered_at is not None

    second_run = await repository.initialize()
    assert second_run is not None and second_run.state == "already_imported"
    assert second_run.imported_rows == 2
    assert len((await repository.admin_page()).items) == 2
    marker = await repository.database.read(
        "SELECT * FROM legacy_imports WHERE source_name = ?",
        (LEGACY_SOURCE_NAME,),
    )
    assert marker[0]["source_sha256"] == migrated.source_sha256

    # The same document produces the same generated legacy ID in another DB.
    other = AlertRepository(StateDatabase(tmp_path / "other.db"), legacy_path=legacy)
    await other.initialize()
    assert (await other.list_owner(101))[0].id == first.id


@pytest.mark.asyncio
async def test_absent_marker_prevents_a_late_legacy_file_from_being_merged(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "alerts.json"
    repository = AlertRepository(StateDatabase(tmp_path / "absent.db"), legacy_path=legacy)
    first = await repository.initialize()
    assert first is not None and first.state == "absent"

    _write_legacy(legacy, [{"name": "late", "price": 10, "qq": 1}])
    second = await repository.initialize()

    assert second is not None and second.state == "absent"
    assert await repository.database.read("SELECT * FROM alerts") == []


@pytest.mark.asyncio
async def test_source_change_before_commit_rolls_back_import_and_marker(tmp_path: Path) -> None:
    legacy = tmp_path / "alerts.json"
    _write_legacy(legacy, [{"name": "original", "price": 10, "qq": 1}])
    repository = AlertRepository(StateDatabase(tmp_path / "changed.db"), legacy_path=legacy)
    real_loader = repository._load_legacy_payload

    def load_then_change(path: Path):
        payload = real_loader(path)
        _write_legacy(path, [{"name": "changed", "price": 20, "qq": 2}])
        return payload

    with patch.object(repository, "_load_legacy_payload", side_effect=load_then_change):
        with pytest.raises(LegacyAlertMigrationError, match="changed"):
            await repository.initialize()

    assert await repository.database.read("SELECT * FROM alerts") == []
    assert await repository.database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_entry",
    [
        {"name": "bad-gid", "price": 10, "qq": 1, "gid": "not-a-number"},
        {"name": "nan", "price": float("nan"), "qq": 1},
        {"name": "too-long", "price": 10, "qq": 2**63},
    ],
)
async def test_invalid_legacy_data_is_backed_up_and_never_partially_imported(
    tmp_path: Path,
    invalid_entry: dict[str, object],
) -> None:
    legacy = tmp_path / "alerts.json"
    original = _write_legacy(
        legacy,
        [
            {"name": "valid-first", "price": 10, "qq": 1},
            invalid_entry,
        ],
    )
    database = StateDatabase(tmp_path / "invalid.db")
    repository = AlertRepository(database, legacy_path=legacy)

    with pytest.raises(LegacyAlertMigrationError) as caught:
        await repository.initialize()

    assert legacy.read_bytes() == original
    assert caught.value.backup_path is not None
    assert caught.value.backup_path.read_bytes() == original
    assert await database.read("SELECT * FROM alerts") == []
    assert await database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
async def test_corrupt_json_is_backed_up_without_modifying_source(tmp_path: Path) -> None:
    legacy = tmp_path / "alerts.json"
    damaged = b'[{"incomplete":'
    legacy.write_bytes(damaged)
    database = StateDatabase(tmp_path / "corrupt.db")
    repository = AlertRepository(database, legacy_path=legacy)

    with pytest.raises(LegacyAlertMigrationError) as caught:
        await repository.initialize()

    assert legacy.read_bytes() == damaged
    assert caught.value.backup_path is not None
    assert caught.value.backup_path.read_bytes() == damaged
    assert await database.read("SELECT * FROM alerts") == []


@pytest.mark.asyncio
async def test_migration_rejects_over_quota_and_ambiguous_nonempty_target(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "alerts.json"
    _write_legacy(
        legacy,
        [{"name": f"item-{index}", "price": 10, "qq": 1} for index in range(21)],
    )
    over_quota = AlertRepository(StateDatabase(tmp_path / "quota-migrate.db"), legacy_path=legacy)
    with pytest.raises(LegacyAlertMigrationError):
        await over_quota.initialize()
    assert await over_quota.database.read("SELECT * FROM alerts") == []

    database = StateDatabase(tmp_path / "ambiguous.db")
    await database.initialize()
    await database.write(
        """
        INSERT INTO alerts (id, owner_user_id, market_name, target_price_cents)
        VALUES ('preexisting', 9, 'existing', 100)
        """
    )
    ambiguous = AlertRepository(database, legacy_path=legacy)
    with pytest.raises(LegacyAlertMigrationError, match="non-empty"):
        await ambiguous.initialize()
    assert len(await database.read("SELECT * FROM alerts")) == 1
    assert await database.read("SELECT * FROM legacy_imports") == []


@pytest.mark.asyncio
async def test_cq_markup_remains_plain_database_text(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    cq_text = "[CQ:image,file=http://127.0.0.1/private]"

    created = await repository.create(101, cq_text, 10)

    assert created.alert is not None
    assert created.alert.market_name == cq_text
    assert (await repository.list_owner(101))[0].market_name == cq_text


@pytest.mark.asyncio
async def test_persistent_evaluation_failures_dead_letter_the_alert(tmp_path: Path) -> None:
    """A19 regression: unbounded evaluation retries must end in the dead-letter state."""

    repository = await _repository(tmp_path, "eval-dead-letter.db")
    repository.max_evaluation_failures = 3  # lower the bar for a fast test

    created = await repository.create(101, "flaky feed", 10, now=1)
    assert created.alert is not None

    claimed = None
    status = None
    for round_index in range(3):
        claimed = (await repository.claim_due(1, 10, now=100 + round_index * 100))[0]
        assert claimed.lease_token is not None
        result = await repository.record_evaluation_failure(
            claimed.id,
            claimed.lease_token,
            "upstream timeout",
            next_retry_at=200 + round_index * 100,
            now=101 + round_index * 100,
        )
        assert result.applied and result.alert is not None
        status = result.alert.status
    assert status == "failed"
    assert claimed is not None

    # Dead-lettered alerts stop being claimable but stay recoverable.
    assert await repository.claim_due(1, 10, now=10_000) == ()
    page = await repository.admin_page(status="failed")
    assert page.total == 1

    retried = await repository.retry_failed(claimed.id, owner_user_id=101, now=20_000)
    assert retried.applied and retried.alert is not None
    assert retried.alert.status == "pending"

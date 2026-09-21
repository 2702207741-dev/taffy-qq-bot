from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.state.broadcast_repository import BroadcastRepository


@pytest.mark.asyncio
async def test_multiple_instances_create_one_unique_claim_per_group(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = BroadcastRepository(StateDatabase(path))
    second = BroadcastRepository(StateDatabase(path))
    await asyncio.gather(first.initialize(), second.initialize())

    left, right = await asyncio.gather(
        first.claim(
            "2026-07-11T08:00+08:00",
            [1001, 2002, 1001],
            lease_token="worker-left:attempt-1",
            lease_seconds=30,
            now=100,
        ),
        second.claim(
            "2026-07-11T08:00+08:00",
            [1001, 2002],
            lease_token="worker-right:attempt-1",
            lease_seconds=30,
            now=100,
        ),
    )

    assert not (
        {delivery.group_id for delivery in left} & {delivery.group_id for delivery in right}
    )
    assert {delivery.group_id for delivery in left + right} == {1001, 2002}
    rows = await first.list(schedule_key="2026-07-11T08:00+08:00")
    assert len(rows) == 2
    assert {row.status for row in rows} == {"claimed"}


@pytest.mark.asyncio
async def test_restart_deduplicates_success_and_retries_only_failed_groups(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    repository = BroadcastRepository(StateDatabase(path), retry_base_seconds=10)
    await repository.initialize()
    schedule_key = "2026-07-11T12:00+08:00"

    claimed = await repository.claim(
        schedule_key,
        [1001, 2002],
        lease_token="attempt-one",
        lease_seconds=30,
        now=100,
    )
    assert {row.group_id for row in claimed} == {1001, 2002}
    assert await repository.mark_delivered(
        schedule_key,
        1001,
        lease_token="attempt-one",
        message_id=987654,
        delivered_at=101,
    )
    assert await repository.record_failure(
        schedule_key,
        2002,
        lease_token="attempt-one",
        error="temporary network failure",
        failed_at=101,
        retry_delay_seconds=10,
    )

    restarted = BroadcastRepository(StateDatabase(path), retry_base_seconds=10)
    await restarted.initialize()
    assert (
        await restarted.claim(
            schedule_key,
            [1001, 2002],
            lease_token="too-early",
            lease_seconds=30,
            now=110,
        )
        == []
    )
    retry = await restarted.claim(
        schedule_key,
        [1001, 2002],
        lease_token="attempt-two",
        lease_seconds=30,
        now=111,
    )
    assert [row.group_id for row in retry] == [2002]

    # Neither a completed group's token nor an older failed token can mutate
    # the newly claimed retry.
    assert not await restarted.mark_delivered(
        schedule_key, 2002, lease_token="attempt-one", delivered_at=112
    )
    assert await restarted.mark_delivered(
        schedule_key,
        2002,
        lease_token="attempt-two",
        message_id="message-2",
        delivered_at=112,
    )
    assert (
        await restarted.claim(
            schedule_key,
            [1001, 2002],
            lease_token="after-restart",
            lease_seconds=30,
            now=999,
        )
        == []
    )

    rows = await restarted.list(schedule_key=schedule_key)
    assert [row.status for row in rows] == ["delivered", "delivered"]
    assert rows[0].message_id == "987654"
    assert rows[1].attempts == 1


@pytest.mark.asyncio
async def test_expired_claim_is_recovered_and_old_token_loses_ownership(tmp_path: Path) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    key = "2026-07-11T15:00+08:00"

    await repository.claim(key, [1001], lease_token="dead-worker", lease_seconds=5, now=100)
    assert (
        await repository.claim(key, [1001], lease_token="early-worker", lease_seconds=5, now=104)
        == []
    )
    recovered = await repository.claim(
        key, [1001], lease_token="replacement", lease_seconds=5, now=105
    )
    assert len(recovered) == 1
    assert recovered[0].lease_owner == "replacement"
    assert recovered[0].last_error == "previous claim lease expired"
    assert recovered[0].attempts == 1

    assert not await repository.mark_delivered(
        key, 1001, lease_token="dead-worker", delivered_at=106
    )
    assert not await repository.record_failure(
        key, 1001, lease_token="dead-worker", error="late callback", failed_at=106
    )
    assert await repository.record_failure(
        key,
        1001,
        lease_token="replacement",
        error="send failed",
        failed_at=106,
        retry_delay_seconds=7,
    )
    row = (await repository.list(schedule_key=key))[0]
    assert row.status == "pending"
    assert row.next_retry_at == 113
    assert row.attempts == 2


@pytest.mark.asyncio
async def test_expired_claim_due_consumes_retry_budget_and_dead_letters(
    tmp_path: Path,
) -> None:
    repository = BroadcastRepository(
        StateDatabase(tmp_path / "expired-dead-letter.db"),
        max_attempts=1,
    )
    await repository.initialize()
    key = "expired-slot"
    claimed = await repository.claim(
        key,
        [1001],
        lease_token="dead-worker",
        lease_seconds=5,
        now=100,
    )
    assert len(claimed) == 1

    assert (
        await repository.claim_due(
            lease_token="replacement",
            lease_seconds=5,
            now=105,
        )
        == []
    )
    failed = await repository.list(schedule_key=key, status="failed")
    assert len(failed) == 1
    assert failed[0].attempts == 1
    assert failed[0].failed_at == 105
    assert failed[0].last_error == "previous claim lease expired"
    assert not await repository.mark_delivered(
        key,
        1001,
        lease_token="dead-worker",
        delivered_at=106,
    )


@pytest.mark.asyncio
async def test_concurrent_expired_claim_recovery_consumes_one_attempt(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-expiry.db"
    first = BroadcastRepository(StateDatabase(path))
    second = BroadcastRepository(StateDatabase(path))
    await asyncio.gather(first.initialize(), second.initialize())
    key = "concurrent-expired-slot"
    original = await first.claim(
        key,
        [1001],
        lease_token="original",
        lease_seconds=5,
        now=100,
    )
    assert len(original) == 1

    left, right = await asyncio.gather(
        first.claim(
            key,
            [1001],
            lease_token="left",
            lease_seconds=5,
            now=105,
        ),
        second.claim(
            key,
            [1001],
            lease_token="right",
            lease_seconds=5,
            now=105,
        ),
    )
    recovered = left + right
    assert len(recovered) == 1
    assert recovered[0].attempts == 1
    assert recovered[0].lease_owner in {"left", "right"}


@pytest.mark.asyncio
async def test_claim_due_bounds_expired_recovery_to_batch_limit(tmp_path: Path) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "bounded-expiry.db"))
    await repository.initialize()
    for index in range(3):
        claimed = await repository.claim(
            f"slot-{index}",
            [1000 + index],
            lease_token=f"worker-{index}",
            lease_seconds=5,
            now=100,
        )
        assert len(claimed) == 1

    recovered = await repository.claim_due(
        lease_token="replacement",
        lease_seconds=5,
        limit=1,
        now=105,
    )

    assert len(recovered) == 1
    all_rows = await repository.list(limit=10)
    assert sum(row.attempts for row in all_rows) == 1
    assert sum(row.status == "claimed" for row in all_rows) == 3


@pytest.mark.asyncio
async def test_cleanup_removes_only_old_delivered_history(tmp_path: Path) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "state.db"))
    await repository.initialize()
    await repository.claim("old", [1001, 2002], lease_token="claim", lease_seconds=10, now=10)
    await repository.mark_delivered("old", 1001, lease_token="claim", delivered_at=20)
    await repository.record_failure(
        "old",
        2002,
        lease_token="claim",
        error="retry me",
        failed_at=20,
        retry_delay_seconds=30,
    )

    assert await repository.cleanup_history(delivered_before=21) == 1
    remaining = await repository.list(schedule_key="old")
    assert len(remaining) == 1
    assert remaining[0].group_id == 2002
    assert remaining[0].status == "pending"


@pytest.mark.asyncio
async def test_retry_budget_moves_delivery_to_dead_letter_until_manual_retry(
    tmp_path: Path,
) -> None:
    repository = BroadcastRepository(
        StateDatabase(tmp_path / "dead-letter.db"),
        max_attempts=1,
    )
    await repository.initialize()
    claimed = await repository.claim(
        "slot",
        [1001],
        lease_token="worker",
        lease_seconds=30,
        now=100,
    )
    assert len(claimed) == 1
    assert await repository.record_failure(
        "slot",
        1001,
        lease_token="worker",
        error="permanent failure",
        failed_at=101,
    )

    failed = (await repository.list(status="failed"))[0]
    assert failed.attempts == 1
    assert failed.failed_at == 101
    assert failed.next_retry_at is None
    assert (
        await repository.claim_due(
            lease_token="another-worker",
            lease_seconds=30,
            now=10_000,
        )
        == []
    )

    assert await repository.retry_failed("slot", 1001, now=200)
    retried = await repository.claim_due(
        lease_token="retry-worker",
        lease_seconds=30,
        now=200,
    )
    assert len(retried) == 1
    assert retried[0].attempts == 0
    assert await repository.record_failure(
        "slot",
        1001,
        lease_token="retry-worker",
        error="still permanent",
        failed_at=201,
    )
    assert await repository.discard_failed("slot", 1001)
    assert await repository.list(schedule_key="slot") == []


@pytest.mark.asyncio
async def test_payload_failure_releases_whole_claim_in_one_repository_operation(
    tmp_path: Path,
) -> None:
    repository = BroadcastRepository(StateDatabase(tmp_path / "claim-failure.db"))
    await repository.initialize()
    claimed = await repository.claim(
        "slot",
        [1001, 1002, 1003],
        lease_token="worker",
        lease_seconds=30,
        now=100,
    )
    assert len(claimed) == 3

    applied = await repository.record_claim_failures(
        "slot",
        lease_token="worker",
        error="MarketPayloadError",
        failed_at=101,
    )

    assert applied == 3
    rows = await repository.list(schedule_key="slot")
    assert {row.status for row in rows} == {"pending"}
    assert {row.attempts for row in rows} == {1}
    assert {row.last_error for row in rows} == {"MarketPayloadError"}


@pytest.mark.asyncio
async def test_cleanup_failed_respects_retention_cutoff(tmp_path: Path) -> None:
    repository = BroadcastRepository(
        StateDatabase(tmp_path / "dead-letter-cleanup.db"),
        max_attempts=1,
    )
    await repository.initialize()
    for key, timestamp in (("old", 10), ("recent", 20)):
        await repository.claim(
            key,
            [1001],
            lease_token=key,
            lease_seconds=5,
            now=timestamp,
        )
        assert await repository.record_failure(
            key,
            1001,
            lease_token=key,
            error="failed",
            failed_at=timestamp + 1,
        )

    assert await repository.cleanup_failed(failed_before=20) == 1
    remaining = await repository.list(status="failed")
    assert [item.schedule_key for item in remaining] == ["recent"]


@pytest.mark.asyncio
async def test_due_retry_reuses_the_first_frozen_payload(tmp_path: Path) -> None:
    repository = BroadcastRepository(
        StateDatabase(tmp_path / "payload.db"),
        retry_base_seconds=10,
    )
    await repository.initialize()
    key = "market-index:20260711T0400Z"
    claimed = await repository.claim(
        key,
        [1001, 2002],
        lease_token="first-token",
        lease_seconds=30,
        now=100,
    )
    assert len(claimed) == 2
    assert (
        await repository.set_payload(
            key,
            lease_token="first-token",
            payload_text="frozen market snapshot",
        )
        == "frozen market snapshot"
    )
    assert await repository.mark_delivered(
        key,
        1001,
        lease_token="first-token",
        delivered_at=101,
    )
    assert await repository.record_failure(
        key,
        2002,
        lease_token="first-token",
        error="temporary",
        failed_at=101,
        retry_delay_seconds=10,
    )

    retry = await repository.claim_due(
        lease_token="retry-token",
        lease_seconds=30,
        now=111,
    )

    assert [item.group_id for item in retry] == [2002]
    assert retry[0].payload_text == "frozen market snapshot"
    assert await repository.get_payload(key) == "frozen market snapshot"
    assert (
        await repository.set_payload(
            key,
            lease_token="retry-token",
            payload_text="must not replace the first snapshot",
        )
        == "frozen market snapshot"
    )

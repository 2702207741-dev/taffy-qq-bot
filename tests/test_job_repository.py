from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.state.job_repository import JobRepository


@pytest.mark.asyncio
async def test_concurrent_workers_only_one_claims_a_slot(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    first = JobRepository(StateDatabase(path))
    second = JobRepository(StateDatabase(path))
    await first.initialize()

    claims = await asyncio.gather(
        first.claim("warm-cache", "warm-cache:100", lease_seconds=60, now=100),
        second.claim("warm-cache", "warm-cache:100", lease_seconds=60, now=100),
    )

    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.asyncio
async def test_expired_claim_is_recovered_and_old_token_is_fenced(tmp_path: Path) -> None:
    repository = JobRepository(StateDatabase(tmp_path / "recover.db"))
    await repository.initialize()
    old = await repository.claim("warm-cache", "slot", lease_seconds=5, now=100)
    assert old is not None

    assert await repository.claim("warm-cache", "slot", lease_seconds=5, now=104) is None
    recovered = await repository.claim("warm-cache", "slot", lease_seconds=5, now=105)
    assert recovered is not None
    assert recovered.lease_token != old.lease_token
    assert recovered.attempts == 2
    assert not await repository.mark_completed(old, completed_at=106)
    assert await repository.mark_completed(recovered, completed_at=106)
    assert await repository.claim("warm-cache", "slot", lease_seconds=5, now=200) is None


@pytest.mark.asyncio
async def test_failure_defers_retry_and_invalidates_claim_token(tmp_path: Path) -> None:
    repository = JobRepository(StateDatabase(tmp_path / "failure.db"))
    await repository.initialize()
    claim = await repository.claim("warm-cache", "slot", lease_seconds=30, now=100)
    assert claim is not None

    assert await repository.record_failure(
        claim,
        error="x" * 2_000,
        retry_at=120,
        now=101,
    )
    assert not await repository.mark_completed(claim, completed_at=102)
    assert await repository.claim("warm-cache", "slot", lease_seconds=30, now=119) is None
    retried = await repository.claim("warm-cache", "slot", lease_seconds=30, now=120)
    assert retried is not None
    assert retried.attempts == 2


@pytest.mark.asyncio
async def test_cleanup_only_removes_old_completed_runs(tmp_path: Path) -> None:
    repository = JobRepository(StateDatabase(tmp_path / "cleanup.db"))
    await repository.initialize()
    old = await repository.claim("warm-cache", "old", lease_seconds=5, now=10)
    recent = await repository.claim("warm-cache", "recent", lease_seconds=5, now=20)
    assert old is not None and recent is not None
    assert await repository.mark_completed(old, completed_at=11)
    assert await repository.mark_completed(recent, completed_at=21)

    assert await repository.cleanup_completed(before=20) == 1
    rows = await repository.database.read(
        "SELECT run_key FROM background_job_runs ORDER BY run_key"
    )
    assert rows == [{"run_key": "recent"}]


@pytest.mark.asyncio
async def test_cleanup_abandoned_keeps_live_claims(tmp_path: Path) -> None:
    repository = JobRepository(StateDatabase(tmp_path / "abandoned.db"))
    await repository.initialize()
    old = await repository.claim("warm-cache", "old", lease_seconds=5, now=10)
    live = await repository.claim("warm-cache", "live", lease_seconds=50, now=10)
    assert old is not None and live is not None

    assert await repository.cleanup_abandoned(before=20) == 1
    rows = await repository.database.read(
        "SELECT run_key FROM background_job_runs ORDER BY run_key"
    )
    assert rows == [{"run_key": "live"}]

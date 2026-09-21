"""Concurrency regressions for the paid-call daily budgets.

The vision budget was already made atomic; these two sibling counters were not,
so every concurrent caller compared the same `used` value and all of them
passed, spending past a cap that is supposed to bound provider cost.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.services import embedding, steamdt_inspect
from src.state import runtime


@pytest.mark.asyncio
async def test_concurrent_embedding_budget_is_a_hard_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = StateDatabase(tmp_path / "embedding.db")
    await database.initialize()
    monkeypatch.setattr(runtime, "database", database)
    monkeypatch.setattr(embedding, "_DAILY_BUDGET", 3)
    monkeypatch.setattr(embedding, "_today_key", lambda: "test-day")

    results = await asyncio.gather(*(embedding._consume_daily_budget() for _ in range(20)))

    assert sum(results) == 3
    rows = await database.read("SELECT value FROM app_meta WHERE key = ?", ("embedding:test-day",))
    assert int(rows[0]["value"]) == 3


@pytest.mark.asyncio
async def test_concurrent_inspect_global_budget_is_a_hard_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = StateDatabase(tmp_path / "inspect.db")
    await database.initialize()
    monkeypatch.setattr(runtime, "database", database)
    monkeypatch.setattr(steamdt_inspect, "_DAILY_BUDGET", 3)
    monkeypatch.setattr(steamdt_inspect, "_PER_USER_DAILY_CAP", 99)
    monkeypatch.setattr(steamdt_inspect, "_today_key", lambda: "test-day")

    # Distinct speakers, so only the global ceiling can stop them.
    results = await asyncio.gather(
        *(steamdt_inspect.consume_inspect_quota(user) for user in range(20))
    )

    assert sum(results) == 3
    rows = await database.read("SELECT value FROM app_meta WHERE key = ?", ("inspect:test-day",))
    assert int(rows[0]["value"]) == 3


@pytest.mark.asyncio
async def test_concurrent_inspect_per_user_cap_holds_under_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = StateDatabase(tmp_path / "inspect_user.db")
    await database.initialize()
    monkeypatch.setattr(runtime, "database", database)
    monkeypatch.setattr(steamdt_inspect, "_DAILY_BUDGET", 99)
    monkeypatch.setattr(steamdt_inspect, "_PER_USER_DAILY_CAP", 2)
    monkeypatch.setattr(steamdt_inspect, "_today_key", lambda: "test-day")

    results = await asyncio.gather(
        *(steamdt_inspect.consume_inspect_quota(1001) for _ in range(20))
    )

    assert sum(results) == 2
    rows = await database.read(
        "SELECT value FROM app_meta WHERE key = ?", ("inspect:user:1001:test-day",)
    )
    assert int(rows[0]["value"]) == 2


@pytest.mark.asyncio
async def test_zero_budgets_never_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = StateDatabase(tmp_path / "zero.db")
    await database.initialize()
    monkeypatch.setattr(runtime, "database", database)
    monkeypatch.setattr(embedding, "_DAILY_BUDGET", 0)
    monkeypatch.setattr(embedding, "_today_key", lambda: "test-day")
    monkeypatch.setattr(steamdt_inspect, "_DAILY_BUDGET", 0)
    monkeypatch.setattr(steamdt_inspect, "_PER_USER_DAILY_CAP", 0)
    monkeypatch.setattr(steamdt_inspect, "_today_key", lambda: "test-day")

    assert await embedding._consume_daily_budget() is False
    assert await steamdt_inspect.consume_inspect_quota(1001) is False

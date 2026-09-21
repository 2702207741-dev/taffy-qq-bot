from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.services import vision
from src.state import runtime


@pytest.mark.asyncio
async def test_concurrent_vision_budget_is_hard_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = StateDatabase(tmp_path / "vision.db")
    await database.initialize()
    monkeypatch.setattr(runtime, "database", database)
    monkeypatch.setattr(vision, "_DAILY_BUDGET", 3)
    monkeypatch.setattr(vision, "_today_key", lambda: "test-day")
    results = await asyncio.gather(*(vision._consume_daily_budget() for _ in range(20)))
    assert sum(results) == 3
    rows = await database.read("SELECT value FROM app_meta WHERE key = ?", ("vision:test-day",))
    assert int(rows[0]["value"]) == 3


@pytest.mark.asyncio
async def test_zero_vision_budget_never_reserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = StateDatabase(tmp_path / "vision.db")
    await database.initialize()
    monkeypatch.setattr(runtime, "database", database)
    monkeypatch.setattr(vision, "_DAILY_BUDGET", 0)
    assert await vision._consume_daily_budget() is False

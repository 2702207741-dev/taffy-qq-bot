from __future__ import annotations

from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.state.usage_log_repository import UsageLogRepository


@pytest.mark.asyncio
async def test_usage_log_record_and_query_round_trip(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    repository = UsageLogRepository(database)
    record_id = await repository.record(
        user_id=101,
        group_id=555,
        feature="watchlist",
        command="我的自选",
        input_text="我的自选",
        output_text="1. AK-47 | Redline",
    )
    assert record_id > 0
    records = await repository.list(user_id=101)
    assert len(records) == 1
    record = records[0]
    assert record.user_id == 101
    assert record.group_id == 555
    assert record.feature == "watchlist"
    assert record.input_text == "我的自选"
    assert record.output_text == "1. AK-47 | Redline"
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_usage_log_supports_null_output_and_filters(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    repository = UsageLogRepository(database)
    await repository.record(user_id=101, feature="market", command="大盘", input_text="大盘")
    await repository.record(
        user_id=202,
        feature="alert",
        command="预警 涨到 红线 10",
        input_text="预警 涨到 红线 10",
        output_text="已创建预警",
    )
    market_rows = await repository.list(feature="market")
    assert len(market_rows) == 1
    assert market_rows[0].output_text is None
    assert await repository.count(user_id=202) == 1
    assert await repository.count(feature="alert") == 1


@pytest.mark.asyncio
async def test_usage_log_cleanup_removes_only_expired_rows(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    repository = UsageLogRepository(database)
    first = await repository.record(
        user_id=101,
        feature="chat",
        command="你好",
        input_text="你好",
    )
    second = await repository.record(
        user_id=101,
        feature="chat",
        command="再见",
        input_text="再见",
    )
    await database.write(
        "UPDATE usage_log SET created_at = created_at - 100 WHERE id = ?",
        (first,),
    )
    row = (await repository.list())[0]
    assert row.id == second
    removed = await repository.cleanup(cutoff_created_at=row.created_at)
    assert removed == 1
    remaining = await repository.list()
    assert [item.id for item in remaining] == [second]


@pytest.mark.asyncio
async def test_usage_log_truncates_oversized_fields(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    repository = UsageLogRepository(database)
    await repository.record(
        user_id=101,
        feature="chat",
        command="x" * 500,
        input_text="y" * 5_000,
        output_text="z" * 10_000,
    )
    records = await repository.list()
    assert len(records[0].command) == 200
    assert len(records[0].input_text) == 4_000
    assert len(records[0].output_text or "") == 8_000


@pytest.mark.asyncio
async def test_usage_log_rejects_invalid_inputs(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    repository = UsageLogRepository(database)
    with pytest.raises(ValueError):
        await repository.record(user_id=0, feature="chat", command="x", input_text="x")
    with pytest.raises(ValueError):
        await repository.record(user_id=101, feature="", command="x", input_text="x")

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from nonebot.exception import FinishedException

from src.plugins import daily, feature_gate
from src.services import market_report
from src.services.index_history import IndexHistoryStore
from src.services.market_history import MarketHistoryStore


@pytest.mark.asyncio
async def test_daily_report_renders_local_sections(tmp_path: Path) -> None:
    index_store = await _index_store_with_closes(tmp_path, days=10)
    history_store = await _history_store_with_movers(tmp_path)
    with (
        patch.object(market_report, "index_history_store", index_store),
        patch.object(market_report, "market_history", history_store),
        patch.object(market_report, "_delivered_alerts", AsyncMock(return_value=((), 0))),
    ):
        report = await market_report.build_report("daily")
    assert report is not None
    assert "塔菲日报" in report
    assert "大盘：¥" in report
    assert "较前一收盘日" in report
    assert "异动" in report
    assert "预警触发：本时段无记录" in report
    assert "仅供参考" in report


@pytest.mark.asyncio
async def test_weekly_report_uses_seven_day_baseline(tmp_path: Path) -> None:
    index_store = await _index_store_with_closes(tmp_path, days=12)
    history_store = await _history_store_with_movers(tmp_path)
    with (
        patch.object(market_report, "index_history_store", index_store),
        patch.object(market_report, "market_history", history_store),
        patch.object(market_report, "_delivered_alerts", AsyncMock(return_value=((), 0))),
    ):
        report = await market_report.build_report("weekly")
    assert report is not None
    assert "塔菲周报" in report
    assert "近7个交易日" in report


@pytest.mark.asyncio
async def test_report_returns_none_without_local_data(tmp_path: Path) -> None:
    empty_index = IndexHistoryStore(path=tmp_path / "empty-index.db")
    empty_history = MarketHistoryStore(path=tmp_path / "empty-history.db")
    with (
        patch.object(market_report, "index_history_store", empty_index),
        patch.object(market_report, "market_history", empty_history),
    ):
        assert await market_report.build_report("daily") is None


@pytest.mark.asyncio
async def test_report_command_falls_back_to_disabled_notice_without_data(tmp_path: Path) -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "日报")
    empty_history = MarketHistoryStore(path=tmp_path / "fallback-history.db")
    with (
        patch.object(daily._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(market_report.index_history_store, "load_closes", AsyncMock(return_value=())),
        patch.object(market_report, "market_history", empty_history),
        patch.object(daily.daily_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await daily.handle_report(event)  # type: ignore[arg-type]
    assert str(finish.await_args.args[0]) == daily._DISABLED_MESSAGE


@pytest.mark.asyncio
async def test_report_command_is_rate_limited() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=102, get_plaintext=lambda: "日报")
    with (
        patch.object(daily._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(daily.daily_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await daily.handle_report(event)  # type: ignore[arg-type]
    assert "太频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_report_command_blocked_when_market_feature_denied() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=103, get_plaintext=lambda: "周报")
    with (
        patch.object(
            feature_gate,
            "authority",
            SimpleNamespace(allowed=AsyncMock(return_value=False)),
        ),
        patch.object(daily.daily_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await daily.handle_report(event)  # type: ignore[arg-type]
    assert "未对你开放" in str(finish.await_args.args[0])


async def _index_store_with_closes(root: Path, *, days: int) -> IndexHistoryStore:
    store = IndexHistoryStore(path=root / "index.db")
    today = date.today()
    closes = {(today - timedelta(days=offset)): 900.0 + offset for offset in range(days)}
    await store.upsert_closes(closes)
    return store


async def _history_store_with_movers(root: Path, *, items: int = 520) -> MarketHistoryStore:
    store = MarketHistoryStore(path=root / "history.db")
    await store.initialize()
    now = int(time.time())
    bucket = now // 3600 * 3600
    connection = sqlite3.connect(root / "history.db")
    try:
        for offset, multiplier in ((0, 1.0), (1, 1.2), (24, 1.0)):
            connection.executemany(
                "INSERT OR REPLACE INTO quote_snapshots "
                "(bucket_at, market_name, sell_price, bid_price, sell_count, bid_count, data_time) "
                "VALUES (?, ?, ?, NULL, 10, 5, ?)",
                [
                    (bucket - offset * 3600, f"Item {index:04d}", 100.0 * multiplier, bucket)
                    for index in range(items)
                ],
            )
        connection.commit()
    finally:
        connection.close()
    return store

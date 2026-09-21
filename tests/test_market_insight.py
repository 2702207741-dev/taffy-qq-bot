from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import feature_gate
from src.plugins.chat import insight
from src.services import market_insight
from src.services.market_history import MarketHistoryStore


async def _history_with_item(root: Path, *, change_multiplier: float = 1.2) -> MarketHistoryStore:
    store = MarketHistoryStore(path=root / "history.db")
    await store.initialize()
    now = int(time.time())
    bucket = now // 3600 * 3600
    connection = sqlite3.connect(root / "history.db")
    try:
        for offset, price in ((0, 100.0 * change_multiplier), (24, 100.0), (25, 99.0)):
            connection.execute(
                "INSERT OR REPLACE INTO quote_snapshots "
                "(bucket_at, market_name, sell_price, bid_price, sell_count, bid_count, data_time) "
                "VALUES (?, 'Test Item', ?, NULL, 10, 5, ?)",
                (bucket - offset * 3600, price, bucket),
            )
        connection.commit()
    finally:
        connection.close()
    return store


@pytest.mark.asyncio
async def test_item_change_reads_local_twenty_four_hour_delta(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path)
    result = await store.item_change("Test Item")
    assert result is not None
    _price, change = result
    assert change == pytest.approx(20.0, abs=0.01)


@pytest.mark.asyncio
async def test_item_change_returns_none_for_unknown_item(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path)
    assert await store.item_change("Missing Item") is None


@pytest.mark.asyncio
async def test_explain_movement_reports_no_data(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path)
    with patch.object(market_insight.market_history, "item_change", store.item_change):
        explanation = await market_insight.explain_movement("Missing Item")
    assert explanation.kind == "no_data"


@pytest.mark.asyncio
async def test_explain_movement_below_threshold_says_no_movement(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path, change_multiplier=1.02)
    with (
        patch.object(market_insight.market_history, "item_change", store.item_change),
        patch.object(market_insight, "collect_fact_sources", AsyncMock()),
    ):
        explanation = await market_insight.explain_movement("Test Item")
    assert explanation.kind == "no_movement"
    assert explanation.change_percent == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_explain_movement_without_sources_reports_no_public_reason(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path)
    unverified = SimpleNamespace(is_verified=False, sources=())
    with (
        patch.object(market_insight.market_history, "item_change", store.item_change),
        patch.object(market_insight, "collect_fact_sources", AsyncMock(return_value=unverified)),
    ):
        explanation = await market_insight.explain_movement("Test Item")
    assert explanation.kind == "no_sources"


@pytest.mark.asyncio
async def test_explain_movement_renders_verified_sources(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path)
    source = SimpleNamespace(title="Valve update", domain="example.com")
    bundle = SimpleNamespace(
        is_verified=True,
        sources=(source,),
        render_context=lambda: "正文",
    )
    validation = SimpleNamespace(
        valid_sentences=1,
        rejected_citations=0,
        rejected_grounding=0,
        rejected_unverified=0,
        content="Valve 发布了更新[来源1]。",
    )
    with (
        patch.object(market_insight.market_history, "item_change", store.item_change),
        patch.object(market_insight, "collect_fact_sources", AsyncMock(return_value=bundle)),
        patch.object(market_insight, "try_anthropic", AsyncMock(return_value="正文[来源1]。")),
        patch.object(
            market_insight,
            "filter_verified_fact_sentences",
            lambda content, bundle: validation,
        ),
    ):
        explanation = await market_insight.explain_movement("Test Item")
    assert explanation.kind == "explained"
    assert explanation.source_lines == ("[来源1] Valve update（example.com）",)


def test_numbers_only_from_input_accepts_known_and_rejects_invented() -> None:
    allowed = {912.21, -0.22, 53.2}
    assert market_insight._numbers_only_from_input("大盘 912.21，上涨占比 53.2%", allowed)
    assert not market_insight._numbers_only_from_input("大盘 912.21，涨幅 12%", allowed)
    assert not market_insight._numbers_only_from_input("今天 08-16 的行情", allowed)


@pytest.mark.asyncio
async def test_interpret_market_falls_back_to_template_on_bad_numbers() -> None:
    facts = {
        "latest_close": 912.21,
        "close_change_percent": -0.22,
        "rising_ratio": 53.2,
        "median_change": 0.12,
        "compared": 600,
        "sell_depth": 12000,
        "bid_depth": 3000,
    }
    with (
        patch.object(market_insight, "_brief_facts", AsyncMock(return_value=facts)),
        patch.object(market_insight.index_history_store, "load_closes", AsyncMock(return_value=())),
        patch.object(
            market_insight,
            "try_anthropic",
            AsyncMock(return_value="大盘很强，涨幅高达 99%。"),
        ),
    ):
        brief = await market_insight.interpret_market()
    assert brief.kind == "template"
    assert "912.21" in brief.content
    assert "不构成交易建议" in brief.content


@pytest.mark.asyncio
async def test_template_brief_never_calls_a_green_day_cold() -> None:
    facts = {
        "latest_close": 950.0,
        "close_change_percent": 3.4,
        "rising_ratio": 82.5,
        "median_change": 2.9,
        "compared": 600,
        "sell_depth": 12000,
        "bid_depth": 3000,
    }
    content = market_insight._template_brief(facts)
    assert "偏冷" not in content
    assert "偏暖" in content


@pytest.mark.asyncio
async def test_interpret_market_accepts_validated_numbers() -> None:
    facts = {"latest_close": 912.21, "close_change_percent": -0.22}
    with (
        patch.object(market_insight, "_brief_facts", AsyncMock(return_value=facts)),
        patch.object(market_insight.index_history_store, "load_closes", AsyncMock(return_value=())),
        patch.object(
            market_insight,
            "try_anthropic",
            AsyncMock(return_value="大盘收于 912.21，微跌 0.22%，结构偏稳。"),
        ),
    ):
        brief = await market_insight.interpret_market()
    assert brief.kind == "llm"


@pytest.mark.asyncio
async def test_interpret_market_no_data() -> None:
    with patch.object(market_insight, "_brief_facts", AsyncMock(return_value=None)):
        brief = await market_insight.interpret_market()
    assert brief.kind == "no_data"


@pytest.mark.asyncio
async def test_interpret_market_appends_history_block() -> None:
    from datetime import date, timedelta

    facts = {"latest_close": 912.21, "close_change_percent": -0.22}
    start = date(2026, 1, 1)
    closes = tuple(
        (start + timedelta(days=index), 800.0 * (1.01 if (index // 4) % 2 == 0 else 0.99))
        for index in range(40)
    )
    with (
        patch.object(market_insight, "_brief_facts", AsyncMock(return_value=facts)),
        patch.object(
            market_insight.index_history_store, "load_closes", AsyncMock(return_value=closes)
        ),
        patch.object(market_insight, "try_anthropic", AsyncMock(side_effect=RuntimeError("down"))),
    ):
        brief = await market_insight.interpret_market()
    assert brief.kind == "template"
    assert "历史维度" in brief.content and "历史分位" in brief.content
    assert "预测倾向" in brief.content


@pytest.mark.asyncio
async def test_movement_command_replies_for_no_movement(tmp_path: Path) -> None:
    store = await _history_with_item(tmp_path, change_multiplier=1.01)
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=301, get_plaintext=lambda: "异动原因 Test Item")
    with (
        patch.object(insight._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch("src.plugins.chat.resolve_name", AsyncMock(return_value="Test Item")),
        patch.object(market_insight.market_history, "item_change", store.item_change),
        patch.object(insight.movement_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await insight.handle_movement(object(), event)  # type: ignore[arg-type]
    message = str(finish.await_args.args[0])
    assert "无异动" in message
    assert "+1.00%" in message


@pytest.mark.asyncio
async def test_movement_command_rate_limited() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=302, get_plaintext=lambda: "为什么涨 红线")
    with (
        patch.object(insight._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(insight.movement_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await insight.handle_movement(object(), event)  # type: ignore[arg-type]
    assert "太频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_brief_command_blocked_when_chat_denied() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=303, get_plaintext=lambda: "盘面解读")
    with (
        patch.object(
            feature_gate,
            "authority",
            SimpleNamespace(allowed=AsyncMock(return_value=False)),
        ),
        patch.object(insight.brief_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await insight.handle_brief(object(), event)  # type: ignore[arg-type]
    assert "未对你开放" in str(finish.await_args.args[0])

from __future__ import annotations

import io
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException
from PIL import Image

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import feature_gate
from src.plugins.chat import cards as market_card
from src.plugins.chat import media
from src.services import card_renderer
from src.services.index_history import IndexHistoryStore
from src.services.market_history import MarketHistoryStore


def test_item_card_renders_valid_png() -> None:
    snapshot = {
        "latest_price": 123.45,
        "week_average": 120.0,
        "week_change": 3.45,
        "samples": 7,
        "data_time": time.time(),
    }
    payload = card_renderer.render_item_card("AK-47 | Redline (FT)", snapshot)
    image = Image.open(io.BytesIO(payload))
    assert image.format == "PNG"
    assert image.size == (1200, 800)


def test_broad_card_renders_valid_png() -> None:
    today = date.today()
    closes = tuple((today - timedelta(days=offset), 900.0 + offset) for offset in range(30, -1, -1))
    payload = card_renderer.render_broad_card(closes)
    image = Image.open(io.BytesIO(payload))
    assert image.format == "PNG"


@pytest.mark.asyncio
async def test_card_command_sends_item_card(tmp_path: Path) -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=201, get_plaintext=lambda: "卡片 AK红线")
    history = MarketHistoryStore(path=tmp_path / "history.db")
    await history.initialize()
    snapshot = {
        "latest_price": 123.45,
        "week_average": 120.0,
        "week_change": 3.45,
        "samples": 7,
        "data_time": time.time(),
    }
    with (
        patch.object(market_card._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch("src.plugins.chat.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch.object(market_card.market_history, "item_snapshot", AsyncMock(return_value=snapshot)),
        patch.object(market_card.card_cmd, "finish", AsyncMock(side_effect=FinishedException())),
    ):
        await market_card.handle_card(bot, event)  # type: ignore[arg-type]
    bot.send.assert_awaited_once()
    segment = bot.send.await_args.args[1]
    assert "file:///" in str(segment)


@pytest.mark.asyncio
async def test_card_command_reports_missing_local_snapshot() -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=202, get_plaintext=lambda: "卡片 稀有饰品")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(market_card._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch("src.plugins.chat.resolve_name", AsyncMock(return_value="Rare Item")),
        patch.object(market_card.market_history, "item_snapshot", AsyncMock(return_value=None)),
        patch.object(market_card.card_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await market_card.handle_card(bot, event)  # type: ignore[arg-type]
    assert "样本池" in str(finish.await_args.args[0])
    bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_command_rate_limited() -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=203, get_plaintext=lambda: "卡片大盘")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(market_card._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(market_card.card_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await market_card.handle_card(bot, event)  # type: ignore[arg-type]
    assert "太频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_card_command_blocked_when_market_denied() -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=204, get_plaintext=lambda: "卡片大盘")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(
            feature_gate,
            "authority",
            SimpleNamespace(allowed=AsyncMock(return_value=False)),
        ),
        patch.object(market_card.card_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await market_card.handle_card(bot, event)  # type: ignore[arg-type]
    assert "未对你开放" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_broad_card_requires_two_closes(tmp_path: Path) -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=205, get_plaintext=lambda: "卡片大盘")
    store = IndexHistoryStore(path=tmp_path / "index.db")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(market_card._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(market_card.index_history_store, "load_closes", AsyncMock(return_value=())),
        patch.object(market_card.card_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await market_card.handle_card(bot, event)  # type: ignore[arg-type]
    assert "积累" in str(finish.await_args.args[0])
    del store


@pytest.mark.asyncio
async def test_generated_image_over_size_limit_is_refused() -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=206)
    await media.send_generated_image(bot, event, b"x" * (9 * 1024 * 1024))  # type: ignore[arg-type]
    text = str(bot.send.await_args.args[1])
    assert "太大" in text

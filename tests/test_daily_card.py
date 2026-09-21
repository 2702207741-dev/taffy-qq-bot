from __future__ import annotations

import io
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

from src.plugins import daily
from src.plugins.chat import official_price
from src.services import card_renderer, market_report, steam_official_price
from src.services.market_report import ReportData, build_report_data, render_report_text


@pytest.mark.asyncio
async def test_daily_card_renders_ranking_png(tmp_path: Path) -> None:
    today = date.today()
    closes = tuple((today - timedelta(days=offset), 900.0 + offset) for offset in range(30, -1, -1))
    movers = [
        ("Rise Item", 12.3, 100.0, 0.0),
        ("Fall Item", -5.2, 50.0, 0.0),
    ]
    report = ReportData(
        kind="daily",
        weekly=False,
        today=today,
        closes=closes,
        movers=movers,
        risers=[("Rise Item", 12.3, 100.0, 0.0)],
        fallers=[("Fall Item", -5.2, 50.0, 0.0)],
        triggers=(),
        alert_total=0,
    )
    payload = card_renderer.render_daily_card(report)
    image = Image.open(io.BytesIO(payload))
    assert image.format == "PNG"
    assert image.size == (1200, 800)


@pytest.mark.asyncio
async def test_build_report_data_returns_structured_dict(tmp_path: Path) -> None:
    with (
        patch.object(
            market_report.index_history_store,
            "load_closes",
            AsyncMock(
                return_value=((date.today(), 900.0), (date.today() - timedelta(days=1), 880.0))
            ),
        ),
        patch.object(
            market_report.market_history,
            "top_movers",
            AsyncMock(return_value=[("A", 3.0, 1.0, 0.0), ("B", -2.0, 1.0, 0.0)]),
        ),
        patch.object(market_report, "_delivered_alerts", AsyncMock(return_value=((), 0))),
    ):
        data = await build_report_data("daily")
    assert data is not None
    assert data["weekly"] is False
    assert data["risers"][0][0] == "A"
    assert data["fallers"][0][0] == "B"
    assert data["alert_total"] == 0


@pytest.mark.asyncio
async def test_daily_command_sends_card_when_renderable(tmp_path: Path) -> None:
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(user_id=401, get_plaintext=lambda: "日报")
    today = date.today()
    closes = ((today, 900.0), (today - timedelta(days=1), 880.0))
    report = {
        "kind": "daily",
        "weekly": False,
        "today": today,
        "closes": closes,
        "movers": None,
        "risers": [],
        "fallers": [],
        "triggers": (),
        "alert_total": 0,
    }
    with (
        patch.object(daily._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(daily, "build_report_data", AsyncMock(return_value=report)),
        patch("src.plugins.chat.media.send_generated_image", AsyncMock()),
    ):
        await daily.handle_report(event, bot)  # type: ignore[arg-type]
    bot.send.assert_awaited()
    segment = str(bot.send.await_args.args[1])
    assert "file:///" in segment


def test_parse_price_handles_cny_and_thousands() -> None:
    assert official_price._parse_price("¥12.34") == 12.34
    assert official_price._parse_price("12,34") == 12.34
    assert official_price._parse_price("1,234.5") == 1234.5
    assert official_price._parse_price("") is None


def test_render_official_price_cross_check() -> None:
    official = steam_official_price.OfficialPrice(
        market_hash_name="AK-47 | Redline",
        lowest_price="¥12.34",
        median_price="¥13.56",
        volume="1,234",
    )
    text = official_price.render_official_price("AK-47 | Redline", official, 12.5)
    assert "最低价: ¥12.34" in text
    assert "24h 成交量: 1,234 件" in text
    assert "+8.5%" in text or "+8.4%" in text
    assert "仅供参考" in text


def test_render_official_price_without_steamdt() -> None:
    official = steam_official_price.OfficialPrice(
        market_hash_name="AK-47 | Redline",
        lowest_price="¥12.34",
        median_price="¥13.56",
        volume="",
    )
    text = official_price.render_official_price("AK-47 | Redline", official, None)
    assert "对照" not in text


@pytest.mark.asyncio
async def test_get_official_price_caches_and_hits_breaker() -> None:
    price = steam_official_price.OfficialPrice(
        market_hash_name="AK-47 | Redline",
        lowest_price="¥12.34",
        median_price="¥13.56",
        volume="10",
    )
    with patch.object(
        steam_official_price,
        "request_json",
        AsyncMock(
            return_value={
                "success": True,
                "lowest_price": "¥12.34",
                "median_price": "¥13.56",
                "volume": "10",
            }
        ),
    ):
        first = await steam_official_price.get_official_price("AK-47 | Redline")
    assert first == price
    with patch.object(
        steam_official_price,
        "request_json",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # cached path: no fetch happens, breaker untouched
        second = await steam_official_price.get_official_price("AK-47 | Redline")
    assert second == price
    steam_official_price._price_cache.clear()
    with patch.object(
        steam_official_price,
        "request_json",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        for _ in range(6):
            assert await steam_official_price.get_official_price("AK-47 | Redline") is None
        breaker = steam_official_price.get_circuit_breaker(steam_official_price._BREAKER_NAME)
        snapshot = await breaker.snapshot()
        assert snapshot.state == "open"
        # open circuit returns None without fetching
        assert await steam_official_price.get_official_price("AK-47 | Redline") is None
    await breaker.succeed()
    steam_official_price._price_cache.clear()


@pytest.mark.asyncio
async def test_official_price_command_renders_and_rate_limits() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=402, get_plaintext=lambda: "官方价 红线")
    official = steam_official_price.OfficialPrice(
        market_hash_name="AK-47 | Redline",
        lowest_price="¥12.34",
        median_price="¥13.56",
        volume="10",
    )
    with (
        patch.object(official_price._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch("src.plugins.chat.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch("src.plugins.chat_utils.st_get", AsyncMock(return_value={})),
        patch.object(
            official_price,
            "validate_quote_response",
            lambda response: [],
        ),
        patch.object(
            official_price,
            "get_official_price",
            AsyncMock(return_value=official),
        ),
        patch.object(official_price.official_price_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await official_price.handle_official_price(object(), event)  # type: ignore[arg-type]
    message = str(finish.await_args.args[0])
    assert "Steam 官方市场价" in message

    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=403, get_plaintext=lambda: "官方价 红线")
    with (
        patch.object(official_price._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(official_price.official_price_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await official_price.handle_official_price(object(), event)  # type: ignore[arg-type]
    assert "太频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_official_price_command_degrades_when_service_unavailable() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=404, get_plaintext=lambda: "官方价 红线")
    with (
        patch.object(official_price._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch("src.plugins.chat.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch.object(official_price, "get_official_price", AsyncMock(return_value=None)),
        patch.object(official_price.official_price_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await official_price.handle_official_price(object(), event)  # type: ignore[arg-type]
    assert "不可用" in str(finish.await_args.args[0])


async def test_official_price_falls_back_to_steamdt_when_official_unavailable() -> None:
    import time as _time

    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=405, get_plaintext=lambda: "官方价 红线")
    fresh = int(_time.time()) - 10
    with (
        patch.object(official_price._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch("src.plugins.chat.resolve_name", AsyncMock(return_value="AK-47 | Redline")),
        patch(
            "src.plugins.chat_utils.st_get",
            AsyncMock(
                return_value={
                    "success": True,
                    "timestamp": fresh,
                    "data": [
                        {
                            "platform": "STEAM",
                            "sellPrice": 12.34,
                            "updateTime": fresh,
                        }
                    ],
                }
            ),
        ),
        patch.object(official_price, "get_official_price", AsyncMock(return_value=None)),
        patch.object(official_price.official_price_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await official_price.handle_official_price(object(), event)  # type: ignore[arg-type]
    text = str(finish.await_args.args[0])
    assert "经 SteamDT 数据" in text
    assert "¥12.34" in text


@pytest.mark.asyncio
async def test_report_data_carries_hoard_line(tmp_path: Path) -> None:
    """窗口B：日报结构化数据携带挂刀行（快照缺失时为 None）。"""

    with (
        patch.object(
            market_report.index_history_store,
            "load_closes",
            AsyncMock(
                return_value=((date.today(), 900.0), (date.today() - timedelta(days=1), 880.0))
            ),
        ),
        patch.object(
            market_report.market_history,
            "top_movers",
            AsyncMock(return_value=[("A", 3.0, 1.0, 0.0)]),
        ),
        patch.object(market_report, "_delivered_alerts", AsyncMock(return_value=((), 0))),
        patch.object(
            market_report,
            "_hoard_line",
            AsyncMock(return_value="挂刀：今日中位 77.9%｜分位 72.9~80.7"),
        ),
    ):
        data = await build_report_data("daily")
    assert data is not None
    assert data["hoard_line"] == "挂刀：今日中位 77.9%｜分位 72.9~80.7"
    text = render_report_text(data)
    assert "挂刀：今日中位 77.9%" in text


@pytest.mark.asyncio
async def test_report_hoard_placeholder_when_snapshot_absent(tmp_path: Path) -> None:
    with (
        patch.object(
            market_report.index_history_store,
            "load_closes",
            AsyncMock(
                return_value=((date.today(), 900.0), (date.today() - timedelta(days=1), 880.0))
            ),
        ),
        patch.object(
            market_report.market_history,
            "top_movers",
            AsyncMock(return_value=[("A", 3.0, 1.0, 0.0)]),
        ),
        patch.object(market_report, "_delivered_alerts", AsyncMock(return_value=((), 0))),
        patch.object(market_report, "_hoard_line", AsyncMock(return_value=None)),
    ):
        data = await build_report_data("daily")
    assert data is not None
    text = render_report_text(data)
    assert "挂刀：快照积累中" in text

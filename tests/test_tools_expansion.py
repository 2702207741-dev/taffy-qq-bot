"""V0.5 night batch 2: tool-loop expansion — alerts / inventory / case-EV / ranking.

Every new tool keeps the fail-closed contract: read-only or confirmed
writes, explicit "unavailable" on failure, numbers only from verified
renderers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import tools as tools_module
from src.services.tools import ToolContext, execute_tool

CTX = ToolContext(user_id=1001, group_id=2001)


class _AlertRecord:
    def __init__(
        self,
        market_name: str,
        target_price_cents: int,
        direction: str = "above",
        status: str = "pending",
    ) -> None:
        self.market_name = market_name
        self.target_price_cents = target_price_cents
        self.direction = direction
        self.status = status


# ---------------------------------------------------------------------------
# list_my_alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_renders_owner_records() -> None:
    records = (
        _AlertRecord("AK-47 | Redline", 12345),
        _AlertRecord("AWP | Asiimov", 90000, direction="below"),
    )
    fake_alerts = SimpleNamespace(list_owner=AsyncMock(return_value=records))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, action = await execute_tool(CTX, "list_my_alerts", {})
    assert action is None
    assert "AK-47 | Redline" in text
    assert "涨到 123.45" in text
    assert "跌到 900.0" in text


@pytest.mark.asyncio
async def test_list_alerts_empty_fail_closed() -> None:
    fake_alerts = SimpleNamespace(list_owner=AsyncMock(return_value=()))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, _ = await execute_tool(CTX, "list_my_alerts", {})
    assert "还没有设置任何预警" in text


@pytest.mark.asyncio
async def test_list_alerts_crash_fail_closed() -> None:
    fake_alerts = SimpleNamespace(list_owner=AsyncMock(side_effect=RuntimeError("db down")))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, _ = await execute_tool(CTX, "list_my_alerts", {})
    assert "不可用" in text


# ---------------------------------------------------------------------------
# create_alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_alert_ok() -> None:
    result = SimpleNamespace(created=True, quota_exceeded=False, alert=None)
    fake_alerts = SimpleNamespace(create=AsyncMock(return_value=result))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, _ = await execute_tool(
            CTX, "create_alert", {"item": "AK-47 | Redline", "price": "123.45"}
        )
    assert "已为「AK-47 | Redline」设置预警" in text
    assert "涨到 123.45" in text


@pytest.mark.asyncio
async def test_create_alert_below_direction() -> None:
    result = SimpleNamespace(created=True, quota_exceeded=False, alert=None)
    fake_alerts = SimpleNamespace(create=AsyncMock(return_value=result))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, _ = await execute_tool(
            CTX,
            "create_alert",
            {"item": "AWP | Asiimov", "price": "100", "direction": "below"},
        )
    assert "跌到 100" in text
    assert "AWP | Asiimov" in text


@pytest.mark.asyncio
async def test_create_alert_missing_args() -> None:
    text, _ = await execute_tool(CTX, "create_alert", {"item": ""})
    assert "缺少饰品名或价格" in text


@pytest.mark.asyncio
async def test_create_alert_bad_price() -> None:
    text, _ = await execute_tool(CTX, "create_alert", {"item": "X", "price": "abc"})
    assert "价格必须是大于 0 的数字" in text


@pytest.mark.asyncio
async def test_create_alert_quota_exceeded() -> None:
    result = SimpleNamespace(created=False, quota_exceeded=True, alert=None)
    fake_alerts = SimpleNamespace(create=AsyncMock(return_value=result))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, _ = await execute_tool(CTX, "create_alert", {"item": "X", "price": "10"})
    assert "已达上限" in text


@pytest.mark.asyncio
async def test_create_alert_duplicate() -> None:
    result = SimpleNamespace(created=False, quota_exceeded=False, alert=None)
    fake_alerts = SimpleNamespace(create=AsyncMock(return_value=result))
    with patch("src.state.runtime.alerts", fake_alerts):
        text, _ = await execute_tool(CTX, "create_alert", {"item": "X", "price": "10"})
    assert "已经设置过了" in text


# ---------------------------------------------------------------------------
# lookup_inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inventory_lookup_ok() -> None:
    fake_report = "🧾 库存报告\n总估值 $123.45\n覆盖率 80%"
    with patch(
        "src.services.steam_inventory.get_inventory_report", AsyncMock(return_value=fake_report)
    ):
        text, _ = await execute_tool(CTX, "lookup_inventory", {"identifier": "7656119"})
    assert "库存报告" in text


@pytest.mark.asyncio
async def test_inventory_lookup_missing_arg() -> None:
    text, _ = await execute_tool(CTX, "lookup_inventory", {})
    assert "缺少 SteamID" in text


@pytest.mark.asyncio
async def test_inventory_lookup_unavailable_prefix() -> None:
    with patch(
        "src.services.steam_inventory.get_inventory_report",
        AsyncMock(return_value="⚠️ 库存不可用"),
    ):
        text, _ = await execute_tool(CTX, "lookup_inventory", {"identifier": "7656119"})
    assert "不可用" in text


@pytest.mark.asyncio
async def test_inventory_lookup_crash() -> None:
    with patch(
        "src.services.steam_inventory.get_inventory_report",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        text, _ = await execute_tool(CTX, "lookup_inventory", {"identifier": "7656119"})
    assert "不可用" in text


# ---------------------------------------------------------------------------
# case_ev
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case_ev_ok() -> None:
    with patch(
        "src.services.case_ev.build_case_ev_reply",
        AsyncMock(return_value="📦 【开箱EV｜AK-47 | 蛇噬收藏箱】\n回报率 -35%"),
    ):
        text, _ = await execute_tool(CTX, "case_ev", {"case": "蛇噬收藏箱"})
    assert "开箱EV" in text


@pytest.mark.asyncio
async def test_case_ev_missing_case() -> None:
    text, _ = await execute_tool(CTX, "case_ev", {})
    assert "缺少箱子名称" in text


@pytest.mark.asyncio
async def test_case_ev_crash() -> None:
    with patch(
        "src.services.case_ev.build_case_ev_reply",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        text, _ = await execute_tool(CTX, "case_ev", {"case": "蛇噬收藏箱"})
    assert "不可用" in text


# ---------------------------------------------------------------------------
# market_ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ranking_ok() -> None:
    with (
        patch(
            "src.services.market_history.MarketHistoryStore",
        ) as store_cls,
        patch.object(
            store_cls.return_value,
            "render_ranking",
            AsyncMock(return_value="📊 【异动榜】\n1. AK-47 | Redline +5.2%"),
        ),
    ):
        text, _ = await execute_tool(CTX, "market_ranking", {"board": "异动"})
    assert "异动榜" in text


@pytest.mark.asyncio
async def test_ranking_bad_board() -> None:
    text, _ = await execute_tool(CTX, "market_ranking", {"board": "热度"})
    assert "榜单类型必须是" in text


@pytest.mark.asyncio
async def test_ranking_limit_clamped() -> None:
    with (
        patch("src.services.market_history.MarketHistoryStore") as store_cls,
        patch.object(
            store_cls.return_value,
            "render_ranking",
            AsyncMock(return_value="📊"),
        ),
    ):
        await execute_tool(CTX, "market_ranking", {"board": "涨跌", "limit": 99})
        call = store_cls.return_value.render_ranking.await_args
    assert call is not None
    assert call.kwargs["limit"] == 20  # clamped to _MAX_RANKING_LIMIT


@pytest.mark.asyncio
async def test_ranking_crash() -> None:
    with (
        patch("src.services.market_history.MarketHistoryStore") as store_cls,
        patch.object(
            store_cls.return_value,
            "render_ranking",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        text, _ = await execute_tool(CTX, "market_ranking", {"board": "异动"})
    assert "不可用" in text


# ---------------------------------------------------------------------------
# registry level
# ---------------------------------------------------------------------------


def test_schema_count_now_ten() -> None:
    schemas = tools_module.anthropic_tool_schemas()
    names = {schema["name"] for schema in schemas}
    assert "list_my_alerts" in names
    assert "create_alert" in names
    assert "lookup_inventory" in names
    assert "case_ev" in names
    assert "market_ranking" in names
    assert "kline_data" in names
    assert len(schemas) == 10


def test_new_tool_parameters_are_json_schema() -> None:
    schemas = tools_module.openai_tool_schemas()
    create = next(schema for schema in schemas if schema["function"]["name"] == "create_alert")
    params = create["function"]["parameters"]
    assert params["required"] == ["item", "price"]
    assert params["properties"]["direction"]["enum"] == ["above", "below"]
    kline = next(schema for schema in schemas if schema["function"]["name"] == "kline_data")
    assert kline["function"]["parameters"]["required"] == ["item"]


# ---------------------------------------------------------------------------
# kline_data tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kline_tool_ok_returns_tool_action() -> None:
    fake_png = b"\x89PNG fake-chart-bytes"
    with patch("src.services.steamdt_kline.SteamDTKlineScreenshotService") as service_cls:
        service_cls.return_value.get_item_png = AsyncMock(return_value=fake_png)
        text, action = await execute_tool(CTX, "kline_data", {"item": "AK-47 | Redline"})
    assert "K线图" in text
    assert action is not None and action.kind == "send_kline_image"
    assert action.payload == fake_png


@pytest.mark.asyncio
async def test_kline_tool_missing_item() -> None:
    text, action = await execute_tool(CTX, "kline_data", {})
    assert "缺少饰品名" in text
    assert action is None


@pytest.mark.asyncio
async def test_kline_tool_upstream_error_fail_closed() -> None:
    from src.services.steamdt_kline import KlineScreenshotError

    with patch("src.services.steamdt_kline.SteamDTKlineScreenshotService") as service_cls:
        service_cls.return_value.get_item_png = AsyncMock(
            side_effect=KlineScreenshotError("upstream down")
        )
        text, action = await execute_tool(CTX, "kline_data", {"item": "AK-47 | Redline"})
    assert "生成不了" in text
    assert action is None


@pytest.mark.asyncio
async def test_kline_tool_crash_fail_closed() -> None:
    with patch("src.services.steamdt_kline.SteamDTKlineScreenshotService") as service_cls:
        service_cls.return_value.get_item_png = AsyncMock(side_effect=RuntimeError("boom"))
        text, action = await execute_tool(CTX, "kline_data", {"item": "AK-47 | Redline"})
    assert "不可用" in text
    assert action is None


@pytest.mark.asyncio
async def test_kline_tool_empty_png_fail_closed() -> None:
    with patch("src.services.steamdt_kline.SteamDTKlineScreenshotService") as service_cls:
        service_cls.return_value.get_item_png = AsyncMock(return_value=b"")
        text, action = await execute_tool(CTX, "kline_data", {"item": "AK-47 | Redline"})
    assert "生成不了" in text
    assert action is None

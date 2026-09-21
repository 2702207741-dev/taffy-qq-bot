"""V0.4 P1: CSQAQ-backed 比价 command, 查询 trailing lines and movement facts."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.chat.commands import ChatCommandService, _csqaq_context_lines
from src.plugins.chat.compare import (
    csqaq_rent_line,
    csqaq_supply_line,
    format_price_comparison,
)
from src.services import market_insight as market_insight_module
from src.services.csqaq import CsqaqGoodDetail, CsqaqPlatformQuote, CsqaqRentInfo, parse_good_detail

STEAMDT_TS_MS = 1_756_442_400_000  # 2026-08-29 12:00:00 +08

STEAMDT_OK = {
    "success": True,
    "data": [
        {
            "platform": "BUFF",
            "sellPrice": 8400.0,
            "sellCount": 445,
            "biddingPrice": 8280,
            "biddingCount": 29,
            "updateTime": STEAMDT_TS_MS,
        },
        {
            "platform": "YOUPIN",
            "sellPrice": 8370.0,
            "sellCount": 469,
            "biddingPrice": 8260,
            "biddingCount": 63,
            "updateTime": STEAMDT_TS_MS,
        },
    ],
}

CSQAQ_DETAIL = CsqaqGoodDetail(
    good_id=7310,
    market_hash_name="★ Butterfly Knife",
    name_cn="蝴蝶刀（★）",
    supply_count=11_299,
    data_age_seconds=658.0,
    quotes=(
        CsqaqPlatformQuote("buff", 8400.0, 445, 8280.0, 29),
        CsqaqPlatformQuote("yyyp", 8340.0, 469, 8260.0, 63),
        CsqaqPlatformQuote("igxe", 8454.0, 10, 8250.0, 7),
        CsqaqPlatformQuote("steam", None, 0, 11789.0, 3809),
    ),
    rent=CsqaqRentInfo(4.5, 4.2, 10.32, 13.25, 52),
    price_change_7d=1.82,
    price_change_30d=-5.62,
)


def _detail_from_payload(payload: dict) -> CsqaqGoodDetail:
    detail = parse_good_detail(135, payload)
    assert detail is not None
    return detail


class FormatComparisonTests(unittest.TestCase):
    def test_full_render_contains_both_sides_spread_supply_rent(self) -> None:
        reply = format_price_comparison(
            "★ Butterfly Knife",
            STEAMDT_OK,
            {"success": True, "detail": CSQAQ_DETAIL},
            now=1_756_442_400.0 + 60,
        )

        self.assertIn("★ Butterfly Knife 比价", reply)
        self.assertIn("SteamDT 实时（", reply)
        self.assertIn("卖 8370.00 ×469 ｜ 收 8260.00 ×63", reply)
        self.assertIn("CSQAQ 快照（", reply)
        self.assertIn("IGXE", reply)
        self.assertIn("求购 11789.00 ×3809", reply)
        self.assertIn("同平台在售价差（CSQAQ − SteamDT）:", reply)
        self.assertIn("BUFF +0.00（+0.00%）", reply)
        self.assertIn("悠悠 -30.00（-0.36%）", reply)
        self.assertIn("存世量 11,299 件", reply)
        self.assertIn("日租 ¥4.5", reply)
        self.assertIn("短租年化 10.32%", reply)
        self.assertIn("两源采集时间可能不同步", reply)

    def test_unconfigured_source_short_circuits(self) -> None:
        reply = format_price_comparison(
            "x", STEAMDT_OK, {"success": False, "errorMsg": "未配置", "unconfigured": True}
        )
        self.assertEqual(reply, "⚠️ 比价数据源未配置（CSQAQ_API_KEY），本次无法比价。")

    def test_steamdt_side_failure_is_reported_not_hidden(self) -> None:
        reply = format_price_comparison(
            "x", {"success": False, "errorMsg": "boom"}, {"success": True, "detail": CSQAQ_DETAIL}
        )
        self.assertIn("SteamDT 侧：", reply)
        self.assertIn("CSQAQ 快照", reply)

    def test_csqaq_side_failure_is_reported_not_hidden(self) -> None:
        reply = format_price_comparison(
            "x",
            STEAMDT_OK,
            {"success": False, "errorMsg": "CSQAQ 侧无法确认该饰品，本次不展示其数据"},
            now=1_756_442_400.0 + 60,
        )
        self.assertIn("SteamDT 实时", reply)
        self.assertIn("CSQAQ 侧：CSQAQ 侧无法确认该饰品", reply)

    def test_both_sides_failed_refuses(self) -> None:
        reply = format_price_comparison(
            "x",
            {"success": False, "errorMsg": "⚠️ 上游未返回有效报价。"},
            {"success": False, "errorMsg": "数据暂时不可用"},
        )
        self.assertIn("比价数据这次拿不到", reply)
        self.assertIn("请稍后重试", reply)

    def test_stale_steamdt_quotes_fail_closed(self) -> None:
        stale = {
            "success": True,
            "data": [{**STEAMDT_OK["data"][0], "updateTime": STEAMDT_TS_MS - 86_400_000}],
        }
        reply = format_price_comparison(
            "x", stale, {"success": True, "detail": CSQAQ_DETAIL}, now=1_756_442_400.0 + 60
        )
        self.assertIn("SteamDT 侧：", reply)


class TrailingLineTests(unittest.TestCase):
    def test_supply_and_rent_lines_render_with_age(self) -> None:
        self.assertEqual(
            csqaq_supply_line(CSQAQ_DETAIL), "存世量：11,299 件（CSQAQ，数据 11 分钟前）"
        )
        self.assertEqual(
            csqaq_rent_line(CSQAQ_DETAIL),
            "租金参考：日租 ¥4.5 ｜ 短租年化 10.32% ｜ 长租年化 13.25%（在租 52）",
        )

    def test_missing_fields_render_nothing(self) -> None:
        bare = CsqaqGoodDetail(
            good_id=1,
            market_hash_name=None,
            name_cn=None,
            supply_count=None,
            data_age_seconds=None,
            quotes=(),
            rent=None,
            price_change_7d=None,
            price_change_30d=None,
        )
        self.assertIsNone(csqaq_supply_line(bare))
        self.assertIsNone(csqaq_rent_line(bare))

    def test_context_lines_silent_on_failure_and_empty_detail(self) -> None:
        self.assertEqual(_csqaq_context_lines({"success": False}), "")
        detail = CsqaqGoodDetail(
            good_id=1,
            market_hash_name=None,
            name_cn=None,
            supply_count=None,
            data_age_seconds=None,
            quotes=(),
            rent=None,
            price_change_7d=None,
            price_change_30d=None,
        )
        self.assertEqual(_csqaq_context_lines({"success": True, "detail": detail}), "")
        rendered = _csqaq_context_lines({"success": True, "detail": CSQAQ_DETAIL})
        self.assertIn("存世量：11,299 件", rendered)
        self.assertIn("租金参考：", rendered)


class CompareCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_compare_command_renders_both_sides(self) -> None:
        provider = AsyncMock(return_value={"success": True, "detail": CSQAQ_DETAIL})
        resolver = AsyncMock(return_value="★ Butterfly Knife")
        service = ChatCommandService(
            market_get=AsyncMock(return_value=STEAMDT_OK),
            market_post=AsyncMock(),
            name_resolver=resolver,
            csqaq_side_provider=provider,
        )

        result = await service.execute("比价 蝴蝶刀")

        assert result is not None
        self.assertIn("★ Butterfly Knife 比价", result)
        provider.assert_awaited_once_with("蝴蝶刀", "★ Butterfly Knife")

    async def test_compare_rejects_special_styles_without_csqaq_calls(self) -> None:
        provider = AsyncMock()
        resolver = AsyncMock(return_value="AK-47 | Redline (Field-Tested)")
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
            name_resolver=resolver,
            csqaq_side_provider=provider,
        )

        result = await service.execute("比价 红线 久经 P1")

        assert result is not None
        self.assertIn("比价暂不支持特殊款式", result)
        provider.assert_not_awaited()

    async def test_price_query_appends_csqaq_lines_when_available(self) -> None:
        provider = AsyncMock(return_value={"success": True, "detail": CSQAQ_DETAIL})
        payload = {
            "success": True,
            "data": {
                "avgPrice": 8000,
                "dataList": [{"platform": "BUFF", "avgPrice": 8000, "dataTime": STEAMDT_TS_MS}],
                "updateTime": STEAMDT_TS_MS,
            },
        }
        service = ChatCommandService(
            market_get=AsyncMock(side_effect=[STEAMDT_OK, payload]),
            market_post=AsyncMock(),
            name_resolver=AsyncMock(return_value="★ Butterfly Knife"),
            csqaq_side_provider=provider,
        )

        result = await service.execute("查询 蝴蝶刀")

        assert result is not None
        self.assertIn("存世量：11,299 件", result)
        self.assertIn("租金参考：", result)

    async def test_price_query_stays_silent_when_csqaq_fails(self) -> None:
        provider = AsyncMock(return_value={"success": False, "errorMsg": "x"})
        payload = {
            "success": True,
            "data": {
                "avgPrice": 8000,
                "dataList": [{"platform": "BUFF", "avgPrice": 8000, "dataTime": STEAMDT_TS_MS}],
                "updateTime": STEAMDT_TS_MS,
            },
        }
        service = ChatCommandService(
            market_get=AsyncMock(side_effect=[STEAMDT_OK, payload]),
            market_post=AsyncMock(),
            name_resolver=AsyncMock(return_value="★ Butterfly Knife"),
            csqaq_side_provider=provider,
        )

        result = await service.execute("查询 蝴蝶刀")

        assert result is not None
        self.assertNotIn("存世量", result)
        self.assertNotIn("租金参考", result)

    async def test_without_provider_compare_reports_unconfigured(self) -> None:
        service = ChatCommandService(
            market_get=AsyncMock(return_value=STEAMDT_OK),
            market_post=AsyncMock(),
            name_resolver=AsyncMock(return_value="★ Butterfly Knife"),
        )

        result = await service.execute("比价 蝴蝶刀")

        assert result is not None
        self.assertIn("比价数据源未配置", result)


class MovementFactTests(unittest.IsolatedAsyncioTestCase):
    async def test_csqaq_lines_require_every_field(self) -> None:
        from src.services.csqaq import CsqaqGoodDetail

        class FakeService:
            def __init__(self, response: dict) -> None:
                self.response = response
                self.calls: list[tuple[str, str]] = []

            async def verified_detail(self, query: str, *, market_hash_name: str) -> dict:
                self.calls.append((query, market_hash_name))
                return self.response

        bare = CsqaqGoodDetail(
            good_id=7,
            market_hash_name="x",
            name_cn="x",
            supply_count=None,
            data_age_seconds=None,
            quotes=(),
            rent=None,
            price_change_7d=None,
            price_change_30d=None,
            steam_turnover_number=353,
            turnover_period_at=None,
        )
        service = FakeService({"success": True, "detail": bare})

        with patch.object(market_insight_module, "csqaq_service", service):
            self.assertEqual(await market_insight_module._csqaq_movement_lines("x", None), ())
            lines = await market_insight_module._csqaq_movement_lines(
                "AK-47 | Redline (Field-Tested)", "红线 久经"
            )
        self.assertEqual(lines, ())
        self.assertEqual(service.calls, [("红线 久经", "AK-47 | Redline (Field-Tested)")])

    async def test_csqaq_lines_render_turnover_fact_with_link(self) -> None:
        payload = {
            "goods_info": {
                "id": 135,
                "name": "AK-47 | 红线 (久经沙场)",
                "market_hash_name": "AK-47 | Redline (Field-Tested)",
                "turnover_number": 353,
                "turnover_avg_price": 807,
                "period_at": "2026-08-29T00:00:00",
                "updated_at": "2026-08-29T22:23:32",
            }
        }
        detail = _detail_from_payload(payload)

        class _FakeService:
            async def verified_detail(self, query: str, *, market_hash_name: str) -> dict:
                return {"success": True, "detail": detail}

        service = _FakeService()

        with patch.object(market_insight_module, "csqaq_service", service):
            lines = await market_insight_module._csqaq_movement_lines(
                "AK-47 | Redline (Field-Tested)", "红线 久经"
            )

        assert lines is not None
        self.assertEqual(len(lines), 2)
        self.assertIn("Steam 近期成交 353 笔", lines[0])
        self.assertIn("成交均价 $807.00", lines[0])
        self.assertIn("截至 2026-08-29", lines[0])
        self.assertIn("https://csqaq.com/goods/135", lines[1])

    async def test_no_sources_reply_still_carries_csqaq_fact(self) -> None:
        movement = market_insight_module.MovementExplanation(
            kind="no_sources",
            change_percent=6.0,
            extra_lines=("CSQAQ 统计：Steam 近期成交 353 笔（截至 2026-08-29）",),
        )
        with patch.object(
            market_insight_module, "explain_movement", AsyncMock(return_value=movement)
        ):
            from src.plugins.chat import insight as insight_module

            with patch.object(insight_module, "explain_movement", AsyncMock(return_value=movement)):
                reply = await insight_module._movement_reply(
                    "AK-47 | Redline (Field-Tested)", user_query="红线 久经"
                )
        self.assertIn("未发现公开原因", reply)
        self.assertIn("CSQAQ 统计", reply)

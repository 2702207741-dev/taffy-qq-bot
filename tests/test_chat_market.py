from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest

import scrape_steamdt

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins import chat
from src.plugins.chat import market as market_module
from src.plugins.chat.commands import (
    BROAD_KLINE_SCREENSHOT,
    BroadKlineScreenshotRequest,
    ChatCommandService,
    ItemKlineScreenshotRequest,
    parse_ranking_command,
)
from src.plugins.chat.market import (
    format_average_price,
    format_broad_market,
    format_item_analysis,
    format_kline,
    format_price,
    resolution_failure_message,
    resolve_catalog_name,
    resolve_name,
)
from src.services import config as service_config
from src.services import market as market_service_module
from src.services.market_catalog import CatalogEntry
from src.services.market_temperature import MarketTemperature, TemperatureUnavailable
from src.services.steamdt_kline import KlineScreenshotError


class MarketNameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        marker = patch.object(
            chat.market_catalog_store,
            "mark_requested",
            AsyncMock(return_value=True),
        )
        marker.start()
        self.addCleanup(marker.stop)

    async def test_full_market_name_requires_explicit_wear(self) -> None:
        self.assertIsNone(await resolve_name("AK-47 | Redline"))
        self.assertEqual(
            await resolve_name("AK-47 | Redline (Field-Tested)"),
            "AK-47 | Redline (Field-Tested)",
        )

    async def test_longest_alias_and_explicit_wear_win(self) -> None:
        self.assertEqual(
            await resolve_name("红线awp 崭新"),
            "AWP | Redline (Factory New)",
        )
        self.assertIsNone(await resolve_name("m4a4印花集 崭新"))

    async def test_unknown_alias_returns_none(self) -> None:
        self.assertIsNone(await resolve_name("完全不存在的皮肤"))

    def test_failure_does_not_claim_weapon_is_missing_when_it_was_provided(self) -> None:
        message = resolution_failure_message("m4a4印花集 崭新")

        self.assertIn("未找到", message)
        self.assertIn("不会改成同系列的其他武器", message)
        self.assertNotIn("请补充具体武器", message)

    def test_full_catalog_resolves_one_unique_chinese_or_english_name(self) -> None:
        entries = (
            CatalogEntry(
                "AK-47 | Leet Museo (Field-Tested)",
                "AK-47 | 抽象派 1337 (久经沙场)",
            ),
            CatalogEntry(
                "AK-47 | Leet Museo (Factory New)",
                "AK-47 | 抽象派 1337 (崭新出厂)",
            ),
        )

        self.assertEqual(
            resolve_catalog_name("抽象派 久经", entries),
            "AK-47 | Leet Museo (Field-Tested)",
        )
        self.assertEqual(
            resolve_catalog_name("leet museo ft", entries),
            "AK-47 | Leet Museo (Field-Tested)",
        )

    def test_full_catalog_requires_wear_and_refuses_ambiguous_matches(self) -> None:
        entries = (
            CatalogEntry("AK-47 | Redline (Field-Tested)", "AK-47 | 红线 (久经沙场)"),
            CatalogEntry("AWP | Redline (Field-Tested)", "AWP | 红线 (久经沙场)"),
        )

        self.assertIsNone(resolve_catalog_name("红线", entries))
        self.assertIsNone(resolve_catalog_name("红线 久经", entries))
        self.assertEqual(
            resolve_catalog_name("AK红线 久经", entries),
            "AK-47 | Redline (Field-Tested)",
        )

    def test_full_catalog_distinguishes_shared_finish_by_weapon_alias(self) -> None:
        entries = (
            CatalogEntry(
                "M4A1-S | Printstream (Factory New)",
                "M4A1消音版 | 印花集 (崭新出厂)",
            ),
            CatalogEntry(
                "AWP | Printstream (Factory New)",
                "AWP | 印花集 (崭新出厂)",
            ),
            CatalogEntry(
                "USP-S | Printstream (Factory New)",
                "USP消音版 | 印花集 (崭新出厂)",
            ),
            CatalogEntry(
                "Desert Eagle | Printstream (Factory New)",
                "沙漠之鹰 | 印花集 (崭新出厂)",
            ),
        )

        self.assertIsNone(resolve_catalog_name("印花集 崭新", entries))
        for query in ("a1印花集 崭新", "m4a1印花集 崭新", "m4a1s印花集 崭新"):
            with self.subTest(query=query):
                self.assertEqual(
                    resolve_catalog_name(query, entries),
                    "M4A1-S | Printstream (Factory New)",
                )
        self.assertIsNone(resolve_catalog_name("a4印花集 崭新", entries))
        expected_by_query = {
            "awp印花集 崭新": "AWP | Printstream (Factory New)",
            "usp印花集 崭新": "USP-S | Printstream (Factory New)",
            "沙鹰印花集 崭新": "Desert Eagle | Printstream (Factory New)",
            "deagle printstream fn": "Desert Eagle | Printstream (Factory New)",
        }
        for query, expected in expected_by_query.items():
            with self.subTest(query=query):
                self.assertEqual(resolve_catalog_name(query, entries), expected)
        self.assertIsNone(
            resolve_catalog_name(
                "m4a4印花集 崭新",
                (
                    CatalogEntry(
                        "AWP | Printstream (Factory New)",
                        "AWP | 印花集 (崭新出厂)",
                    ),
                ),
            )
        )

    def test_full_catalog_accepts_weapon_before_or_after_shared_finish(self) -> None:
        entries = (
            CatalogEntry(
                "M4A1-S | Fade (Factory New)",
                "M4A1消音版 | 渐变之色 (崭新出厂)",
            ),
            CatalogEntry(
                "AWP | Fade (Factory New)",
                "AWP | 渐变之色 (崭新出厂)",
            ),
        )

        self.assertIsNone(resolve_catalog_name("渐变之色 崭新", entries))
        for query in (
            "a1渐变之色 崭新",
            "渐变之色a1 崭新",
            "M4A1-S 渐变之色 崭新",
            "渐变之色 M4A1-S 崭新",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    resolve_catalog_name(query, entries),
                    "M4A1-S | Fade (Factory New)",
                )
        self.assertEqual(
            resolve_catalog_name("渐变之色awp 崭新", entries),
            "AWP | Fade (Factory New)",
        )
        self.assertIsNone(resolve_catalog_name("渐变之色a1 崭新", entries[1:]))

    def test_full_catalog_keeps_stattrak_separate(self) -> None:
        entries = (
            CatalogEntry(
                "AK-47 | Leet Museo (Field-Tested)",
                "AK-47 | 抽象派 1337 (久经沙场)",
            ),
            CatalogEntry(
                "StatTrak™ AK-47 | Leet Museo (Field-Tested)",
                "StatTrak™ AK-47 | 抽象派 1337 (久经沙场)",
            ),
        )

        self.assertEqual(
            resolve_catalog_name("抽象派 久经", entries),
            "AK-47 | Leet Museo (Field-Tested)",
        )
        self.assertEqual(
            resolve_catalog_name("暗金 抽象派 久经", entries),
            "StatTrak™ AK-47 | Leet Museo (Field-Tested)",
        )

    async def test_public_resolver_falls_back_to_durable_catalog(self) -> None:
        entries = (
            CatalogEntry(
                "M4A4 | Catalog Finish (Field-Tested)",
                "M4A4 | 全量目录测试 (久经沙场)",
            ),
        )
        with (
            patch.object(
                chat,
                "get_market_catalog_entries",
                AsyncMock(return_value=entries),
            ) as catalog_provider,
            patch.object(
                chat,
                "search_market_catalog",
                AsyncMock(return_value=()),
            ) as live_search,
        ):
            result = await chat.resolve_name("全量目录测试 久经")

        self.assertEqual(result, "M4A4 | Catalog Finish (Field-Tested)")
        catalog_provider.assert_awaited_once_with()
        live_search.assert_not_awaited()

    async def test_public_resolver_uses_live_search_before_full_sync(self) -> None:
        entries = (
            CatalogEntry(
                "M4A4 | In Living Color (Factory New)",
                "M4A4 | 活色生香 (崭新出厂)",
            ),
            CatalogEntry(
                "StatTrak™ M4A4 | In Living Color (Factory New)",
                "M4A4（StatTrak™） | 活色生香 (崭新出厂)",
            ),
        )
        with (
            patch.object(
                chat,
                "get_market_catalog_entries",
                AsyncMock(return_value=()),
            ),
            patch.object(
                chat,
                "search_market_catalog",
                AsyncMock(return_value=entries),
            ),
        ):
            result = await chat.resolve_name("活色生香 崭新")

        self.assertEqual(result, "M4A4 | In Living Color (Factory New)")

    async def test_public_resolver_expands_weapon_alias_for_live_search(self) -> None:
        entries = (
            CatalogEntry(
                "M4A1-S | Printstream (Factory New)",
                "M4A1消音版 | 印花集 (崭新出厂)",
            ),
        )
        with (
            patch.object(
                chat,
                "get_market_catalog_entries",
                AsyncMock(return_value=()),
            ),
            patch.object(
                chat,
                "search_market_catalog",
                AsyncMock(return_value=entries),
            ) as live_search,
        ):
            result = await chat.resolve_name("a1印花集 崭新")

        self.assertEqual(result, "M4A1-S | Printstream (Factory New)")
        live_search.assert_awaited_once_with("M4A1-S 印花集 Factory New")

    async def test_public_resolver_moves_trailing_weapon_alias_for_live_search(self) -> None:
        entries = (
            CatalogEntry(
                "M4A1-S | Fade (Factory New)",
                "M4A1消音版 | 渐变之色 (崭新出厂)",
            ),
        )
        with (
            patch.object(
                chat,
                "get_market_catalog_entries",
                AsyncMock(return_value=()),
            ),
            patch.object(
                chat,
                "search_market_catalog",
                AsyncMock(return_value=entries),
            ) as live_search,
        ):
            result = await chat.resolve_name("渐变之色a1 崭新")

        self.assertEqual(result, "M4A1-S | Fade (Factory New)")
        live_search.assert_awaited_once_with("M4A1-S 渐变之色 Factory New")

    async def test_public_resolver_does_not_guess_between_live_candidates(self) -> None:
        entries = (
            CatalogEntry("AK-47 | Redline (Field-Tested)", "AK-47 | 红线 (久经沙场)"),
            CatalogEntry("AWP | Redline (Field-Tested)", "AWP | 红线 (久经沙场)"),
        )
        with (
            patch.object(
                chat,
                "get_market_catalog_entries",
                AsyncMock(return_value=()),
            ),
            patch.object(
                chat,
                "search_market_catalog",
                AsyncMock(return_value=entries),
            ),
        ):
            result = await chat.resolve_name("红线 久经")

        self.assertIsNone(result)

    async def test_package_keeps_public_compatibility_facades(self) -> None:
        self.assertTrue(callable(chat.resolve_name))
        self.assertTrue(callable(resolve_name))
        self.assertTrue(callable(chat.cmd))
        self.assertTrue(callable(chat.skey))
        self.assertIsNotNone(chat.gk)
        self.assertIsNotNone(chat.gc)
        self.assertIsNotNone(chat.pc)
        self.assertIsNotNone(chat.cc)


class MarketFormattingTests(unittest.TestCase):
    def test_scraper_and_reader_share_project_relative_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(
                    os.environ,
                    {"TAFFY_STEAMDT_CACHE": "runtime-cache/steamdt.json"},
                    clear=False,
                ),
                patch.object(service_config, "PROJECT_ROOT", root),
                patch.object(service_config, "_RUNTIME_SETTINGS", {}),
            ):
                expected = (root / "runtime-cache" / "steamdt.json").resolve()
                self.assertEqual(scrape_steamdt._cache_file_path(), expected)
                self.assertEqual(market_module._volume_cache_path(), expected)

    def test_price_format_skips_invalid_rows(self) -> None:
        now = 1_700_000_100
        result = format_price(
            {
                "success": True,
                "updateTime": 1_700_000_000,
                "data": [
                    {
                        "platform": "BUFF",
                        "sellPrice": 100.5,
                        "sellCount": 3,
                        "biddingPrice": 99,
                        "biddingCount": 2,
                    },
                    {"platform": "C5", "sellPrice": 0},
                    "invalid",
                ],
            },
            now=now,
        )

        self.assertIn("BUFF", result)
        self.assertIn("100.50", result)
        self.assertNotIn("C5", result)

    def test_price_format_ignores_stale_zero_rows_without_poisoning_fresh_quotes(self) -> None:
        now = 1_700_000_100
        result = format_price(
            {
                "success": True,
                "data": [
                    {
                        "platform": "OLD_ZERO",
                        "sellPrice": 0,
                        "updateTime": now - 365 * 86_400,
                    },
                    {
                        "platform": "BUFF",
                        "sellPrice": 100.5,
                        "biddingPrice": 99,
                        "updateTime": str(now - 30),
                    },
                ],
            },
            now=now,
        )

        self.assertIn("BUFF", result)
        self.assertIn("100.50", result)
        self.assertNotIn("OLD_ZERO", result)
        self.assertNotIn("已过期", result)

    def test_price_format_hides_zero_or_crossed_bid_fields(self) -> None:
        now = 1_700_000_100
        result = format_price(
            {
                "success": True,
                "updateTime": now - 10,
                "data": [
                    {
                        "platform": "BUFF",
                        "sellPrice": 100.0,
                        "sellCount": 20,
                        "biddingPrice": 0,
                        "biddingCount": 0,
                    },
                    {
                        "platform": "C5",
                        "sellPrice": 100.0,
                        "sellCount": 20,
                        "biddingPrice": 220.0,
                        "biddingCount": 15,
                    },
                ],
            },
            now=now,
        )

        self.assertEqual(result.count("暂无有效报价"), 2)
        self.assertNotIn("220.00", result)

    def test_price_format_does_not_echo_unvalidated_count_values(self) -> None:
        now = 1_700_000_100
        result = format_price(
            {
                "success": True,
                "updateTime": now - 10,
                "data": [
                    {
                        "platform": "BUFF",
                        "sellPrice": 100.0,
                        "sellCount": "not-a-count",
                        "biddingPrice": 99.0,
                        "biddingCount": "not-a-count",
                    }
                ],
            },
            now=now,
        )

        self.assertNotIn("not-a-count", result)
        self.assertIn("暂无有效报价", result)

    def test_price_format_rejects_stale_positive_row_even_with_fresh_container(self) -> None:
        now = 1_700_000_100
        result = format_price(
            {
                "success": True,
                "updateTime": now,
                "data": [
                    {
                        "platform": "BUFF",
                        "sellPrice": 100.5,
                        "updateTime": now - 86_400,
                    }
                ],
            },
            now=now,
        )

        self.assertIn("各平台实时报价均已过期", result)
        self.assertNotIn("BUFF报价", result)

    def test_price_format_aggregates_stale_platforms_without_internal_codes(self) -> None:
        now = 1_700_003_000
        result = format_price(
            {
                "success": True,
                "data": [
                    {
                        "platform": "C5",
                        "sellPrice": 0,
                        "updateTime": now - 10,
                    },
                    {
                        "platform": "YOUPIN",
                        "sellPrice": 9700,
                        "updateTime": now - 2800,
                    },
                    {
                        "platform": "BUFF",
                        "sellPrice": 9000,
                        "updateTime": now - 2200,
                    },
                    {
                        "platform": "STEAM",
                        "sellPrice": 10006.61,
                        "updateTime": now - 1500,
                    },
                ],
            },
            now=now,
        )

        self.assertEqual(
            result,
            "⚠️ 各平台实时报价均已过期（最新数据时间: "
            f"{datetime.fromtimestamp(now - 1500, tz=timezone(timedelta(hours=8))):%Y-%m-%d %H:%M:%S}"
            "），本次不展示即时价格。",
        )
        self.assertNotIn("YOUPIN", result)
        self.assertNotIn("C5", result)

    def test_seven_day_average_uses_fetch_time_and_correct_window_label(self) -> None:
        now = 1_700_000_100
        result = format_average_price(
            {
                "success": True,
                "data": {
                    "avgPrice": 105.5,
                    "dataList": [{"platform": "BUFF", "avgPrice": 104.25}],
                },
            },
            fetched_at=now - 5,
            now=now,
        )

        self.assertIn("7日历史均价", result)
        self.assertIn("全平台均价: ¥105.50", result)
        self.assertIn("BUFF均价: ¥104.25", result)
        self.assertNotIn("30日", result)

    def test_seven_day_average_shows_extra_platforms_in_stable_order(self) -> None:
        now = 1_700_000_100
        result = format_average_price(
            {
                "success": True,
                "data": {
                    "avgPrice": 105.5,
                    "dataList": [
                        {"platform": "SKINPORT", "avgPrice": 88.0},
                        {"platform": "BUFF", "avgPrice": 104.25},
                        {"platform": "CSMONEY", "avgPrice": 90.5},
                        {"platform": "BUFF", "avgPrice": 1.0},
                        {"platform": "WAXPEER", "avgPrice": -5},
                        {"platform": "IGXE", "avgPrice": "n/a"},
                        {"platform": "NEWCODE", "avgPrice": 77.0},
                        "junk",
                    ],
                },
            },
            fetched_at=now - 5,
            now=now,
        )

        self.assertIn("BUFF均价: ¥104.25", result)
        self.assertIn("CSMONEY均价: ¥90.50", result)
        self.assertIn("SKINPORT均价: ¥88.00", result)
        self.assertIn("NEWCODE均价: ¥77.00", result)
        self.assertNotIn("¥1.00", result)
        self.assertLess(result.index("BUFF均价"), result.index("CSMONEY均价"))
        self.assertLess(result.index("CSMONEY均价"), result.index("SKINPORT均价"))
        self.assertLess(result.index("SKINPORT均价"), result.index("NEWCODE均价"))

    def test_kline_accepts_numeric_string_timestamps(self) -> None:
        now = 1_700_086_500
        result = format_kline(
            "AK-47 | Redline (Field-Tested)",
            {
                "success": True,
                "data": [
                    ["1700000000", 100, 105, 110, 95],
                    ["1700086400", 105, 108, 112, 101],
                ],
            },
            now=now,
        )

        self.assertIn("最近2个日K", result)
        self.assertIn("展示区间涨跌: +2.9%", result)

    def test_item_analysis_uses_timestamped_kline_instead_of_unverified_average(self) -> None:
        now = 1_700_086_500
        result = format_item_analysis(
            "AK-47 | Redline (Field-Tested)",
            {
                "success": True,
                "updateTime": now - 10,
                "data": [{"platform": "BUFF", "sellPrice": 108}],
            },
            {
                "success": True,
                "data": [
                    ["1700000000", 100, 105, 110, 95],
                    ["1700086400", 105, 108, 112, 101],
                ],
            },
            now=now,
        )

        self.assertIn("收盘均价: ¥106.50", result)
        self.assertIn("1日动量: +2.86%", result)
        self.assertIn("历史数据统计，不代表价格预测", result)

    def test_broad_format_survives_corrupt_volume_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "steamdt_data.json"
            cache.write_text("{broken", encoding="utf-8")
            now = cache.stat().st_mtime
            result = format_broad_market(
                {
                    "success": True,
                    "data": {
                        "broadMarketIndex": 1000,
                        "diffYesterday": 10,
                        "diffYesterdayRatio": 1,
                        "historyMarketIndexList": [],
                        "updateTime": now,
                    },
                },
                volume_cache_path=cache,
                now=now,
            )

        self.assertIn("大盘点位: 1000.00", result)
        self.assertIn("成交数据无效", result)
        self.assertIn("成交未通过展示条件，本次不引用", result)
        self.assertNotIn("指数与已展示成交均通过", result)
        self.assertIn("📈 【市场指数】", result)
        self.assertIn("数据时间:", result)
        self.assertIn("温度暂不可用: 历史样本不足", result)
        self.assertNotIn("来源", result)
        self.assertNotIn("第三方", result)
        self.assertNotIn("SteamDT", result)

    def test_broad_format_displays_explainable_temperature_without_source_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "volume.json"
            cache.write_text("{}", encoding="utf-8")
            now = cache.stat().st_mtime
            result = format_broad_market(
                {
                    "success": True,
                    "data": {
                        "broadMarketIndex": 917.14,
                        "diffYesterday": -5.31,
                        "diffYesterdayRatio": -0.58,
                        "historyMarketIndexList": [[now, 917.14]],
                        "updateTime": now,
                    },
                },
                temperature=MarketTemperature(
                    score=-14.3,
                    change_1d=-0.5756,
                    change_3d=-0.7843,
                    change_7d=-0.9878,
                    deviation_7d=-0.6114,
                    current_state="低位承压",
                    detail="1/3/7日动量同步偏弱",
                    temperature_state="偏冷",
                ),
                volume_cache_path=cache,
                now=now,
            )

        self.assertIn("🌡️ 【盘面温度】", result)
        self.assertIn("当前状态: 低位承压", result)
        self.assertIn("动量数据: 1日 -0.58% / 3日 -0.78% / 7日 -0.99%", result)
        self.assertIn("温度分: -14.3/100", result)
        self.assertIn("温度为本机器人量化动量指标，不代表价格预测", result)
        for forbidden in ("来源", "第三方", "SteamDT"):
            self.assertNotIn(forbidden, result)

    def test_broad_volume_keeps_yesterday_values_separate_from_change_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "volume.json"
            cache.write_text(
                '{"success":true,"volume":"10万","turnover":"20万",'
                '"volume_yest":"99万","turnover_yest":"88万",'
                '"volume_change":"↑ 99%","turnover_change":"↑ 88%"}',
                encoding="utf-8",
            )
            now = cache.stat().st_mtime
            result = format_broad_market(
                {
                    "success": True,
                    "data": {
                        "broadMarketIndex": 1000,
                        "diffYesterday": 0,
                        "diffYesterdayRatio": 0,
                        "historyMarketIndexList": [],
                        "updateTime": now,
                    },
                },
                volume_cache_path=cache,
                now=now,
            )

        self.assertIn("今日累计成交量: 10万（昨日: 99万）", result)
        self.assertIn("今日累计成交额: 20万（昨日: 88万）", result)
        self.assertIn("环比指标: 成交额↑ 88% / 成交量↑ 99%", result)
        self.assertIn("指数与已展示成交均通过字段及时效校验", result)
        self.assertNotIn("昨日同段变化", result)

    def test_broad_volume_prefers_same_response_summary_statistics(self) -> None:
        now = time.time()
        result = format_broad_market(
            {
                "success": True,
                "data": {
                    "broadMarketIndex": 912.36,
                    "diffYesterday": -1.9,
                    "diffYesterdayRatio": -0.2078,
                    "historyMarketIndexList": [],
                    "todayStatistics": {
                        "tradeNum": "688030",
                        "turnover": 22_049_530,
                        "tradeVolumeRatio": -15,
                        "tradeAmountRatio": 3.64,
                    },
                    "yesterdayStatistics": {
                        "tradeNum": "1499240",
                        "turnover": 49_464_710,
                    },
                    "updateTime": now,
                },
            },
            volume_cache_path=Path("this-file-must-not-be-read.json"),
            now=now,
        )

        self.assertIn("今日累计成交量: 68.803万（昨日: 149.924万）", result)
        self.assertIn("今日累计成交额: 2204.953万（昨日: 4946.471万）", result)
        self.assertIn("环比指标: 成交额↑ 3.64% / 成交量↓ 15%", result)
        self.assertIn("指数与已展示成交均通过字段及时效校验", result)
        for forbidden in ("来源", "第三方", "SteamDT"):
            self.assertNotIn(forbidden, result)


class BroadSummaryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_summary_uses_direct_json_without_legacy_gateway(self) -> None:
        now = time.time()
        response = {
            "success": True,
            "data": {
                "broadMarketIndex": 912.36,
                "diffYesterday": -1.9,
                "diffYesterdayRatio": -0.2078,
                "historyMarketIndexList": [],
                "todayStatistics": {"tradeNum": "1", "turnover": 1},
                "yesterdayStatistics": {"tradeNum": "1", "turnover": 1},
                "updateTime": now,
            },
        }
        request = AsyncMock(return_value=response)
        legacy = AsyncMock()

        with (
            patch.object(market_service_module, "request_json", request),
            patch.object(market_service_module.market_service, "get", legacy),
        ):
            result = await market_service_module.fetch_broad_summary()

        self.assertEqual(result, response)
        legacy.assert_not_awaited()
        request.assert_awaited_once()
        _, url = request.await_args.args
        self.assertEqual(url, "https://www.steamdt.com/api/index/statistics/v1/summary")
        self.assertEqual(request.await_args.kwargs["attempts"], 1)
        self.assertEqual(request.await_args.kwargs["total_timeout"], 4.0)


class CommandServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_broad_market_uses_fast_provider_without_legacy_fetch(self) -> None:
        now = time.time()
        broad_provider = AsyncMock(
            return_value={
                "success": True,
                "data": {
                    "broadMarketIndex": 1000,
                    "diffYesterday": 0,
                    "diffYesterdayRatio": 0,
                    "historyMarketIndexList": [],
                    "updateTime": now,
                },
            }
        )
        legacy_market_get = AsyncMock()
        service = ChatCommandService(
            market_get=legacy_market_get,
            market_post=AsyncMock(),
            broad_provider=broad_provider,
        )

        result = await service.execute("大盘")

        self.assertIn("市场指数", result or "")
        broad_provider.assert_awaited_once_with()
        legacy_market_get.assert_not_awaited()

    async def test_unrepresentative_market_columns_are_paused_without_fetching(self) -> None:
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
        )

        for command in (
            "热门榜",
            "热度榜",
            "流动性榜",
            "异动",
            "预测",
            "涨跌榜",
            "综合指数",
            "装备指数",
        ):
            with self.subTest(command=command):
                result = await service.execute(command)
                self.assertIn("正在重建", result or "")
                self.assertIn("不能代表整个CS2市场", result or "")
        menu = await service.execute("菜单")

        self.assertNotIn("热度榜", menu or "")
        # Ranking boards are real again since the sitemap catalog expansion,
        # so the menu legitimately advertises them with category filtering.
        self.assertIn("榜单", menu or "")

    async def test_exact_kline_requests_current_broad_chart_without_api_calls(self) -> None:
        market_get = AsyncMock()
        market_post = AsyncMock()
        resolver = AsyncMock()
        service = ChatCommandService(
            market_get=market_get,
            market_post=market_post,
            name_resolver=resolver,
        )

        for command in ("K线", "k线"):
            with self.subTest(command=command):
                result = await service.execute(command)
                self.assertIs(result, BROAD_KLINE_SCREENSHOT)
                self.assertIsInstance(result, BroadKlineScreenshotRequest)

        market_get.assert_not_awaited()
        market_post.assert_not_awaited()
        resolver.assert_not_awaited()

    async def test_named_kline_requests_resolved_item_chart_without_api_call(self) -> None:
        market_post = AsyncMock()
        resolver = AsyncMock(return_value="AK-47 | Redline (Field-Tested)")
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=market_post,
            name_resolver=resolver,
        )

        result = await service.execute("K线 红线")

        self.assertIsInstance(result, ItemKlineScreenshotRequest)
        self.assertEqual(result.market_name, "AK-47 | Redline (Field-Tested)")
        resolver.assert_awaited_once_with("红线")
        market_post.assert_not_awaited()

    async def test_named_kline_accepts_platform_suffix(self) -> None:
        resolver = AsyncMock(return_value="AK-47 | Redline (Field-Tested)")
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
            name_resolver=resolver,
        )

        result = await service.execute("K线 红线 BUFF")

        self.assertIsInstance(result, ItemKlineScreenshotRequest)
        self.assertEqual(result.platform, "BUFF")
        resolver.assert_awaited_once_with("红线")

        plain = await service.execute("K线 红线")
        self.assertIsInstance(plain, ItemKlineScreenshotRequest)
        self.assertIsNone(plain.platform)

    async def test_broad_kline_backtest_parses_days(self) -> None:
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
        )

        result = await service.execute("K线 30天前")

        self.assertIsInstance(result, BroadKlineScreenshotRequest)
        self.assertEqual(result.max_days_ago, 30)
        self.assertFalse(result.high_definition)

        hd = await service.execute("K线高清 7天前")

        self.assertIsInstance(hd, BroadKlineScreenshotRequest)
        self.assertEqual(hd.max_days_ago, 7)
        self.assertTrue(hd.high_definition)

        bad = await service.execute("K线 0天前")

        self.assertIsInstance(bad, str)
        self.assertIn("1~90", bad)

    async def test_named_kline_resolves_abstract_art_alias_with_wear(self) -> None:
        market_get = AsyncMock()
        market_post = AsyncMock()
        service = ChatCommandService(
            market_get=market_get,
            market_post=market_post,
        )

        result = await service.execute("k线 抽象派 久经")

        self.assertEqual(
            result,
            ItemKlineScreenshotRequest("AK-47 | Leet Museo (Field-Tested)"),
        )
        market_get.assert_not_awaited()
        market_post.assert_not_awaited()

    async def test_market_query_routes_through_injected_clients(self) -> None:
        market_get = AsyncMock(
            return_value={
                "success": True,
                "updateTime": time.time(),
                "data": [
                    {
                        "platform": "BUFF",
                        "sellPrice": 88,
                        "sellCount": 1,
                        "biddingPrice": 87,
                        "biddingCount": 2,
                    }
                ],
            }
        )
        market_post = AsyncMock()
        resolver = AsyncMock(return_value="AK-47 | Redline (Field-Tested)")
        service = ChatCommandService(
            market_get=market_get,
            market_post=market_post,
            name_resolver=resolver,
        )

        result = await service.execute("查询 红线")

        self.assertIn("AK-47 | Redline", result or "")
        self.assertIn("88.00", result or "")
        resolver.assert_awaited_once_with("红线")
        market_get.assert_any_await(
            "/open/cs2/v1/price/single",
            {"marketHashName": "AK-47 | Redline (Field-Tested)"},
        )
        market_get.assert_any_await(
            "/open/cs2/v1/price/avg",
            {"marketHashName": "AK-47 | Redline (Field-Tested)"},
            300,
        )
        self.assertEqual(market_get.await_count, 2)
        market_post.assert_not_awaited()

    async def test_analysis_uses_price_seven_day_average_and_timestamped_kline(self) -> None:
        now = time.time()
        market_get = AsyncMock(
            return_value={
                "success": True,
                "updateTime": now,
                "data": [{"platform": "BUFF", "sellPrice": 88}],
            }
        )
        market_post = AsyncMock(
            return_value={
                "success": True,
                "data": [
                    [str(now - 86_400), 80, 82, 83, 79],
                    [str(now), 82, 88, 89, 81],
                ],
            }
        )
        service = ChatCommandService(
            market_get=market_get,
            market_post=market_post,
            name_resolver=AsyncMock(return_value="AK-47 | Redline (Field-Tested)"),
        )

        result = await service.execute("分析 红线 久经")

        self.assertIn("收盘均价: ¥85.00", result or "")
        market_get.assert_any_await(
            "/open/cs2/v1/price/single",
            {"marketHashName": "AK-47 | Redline (Field-Tested)"},
        )
        market_get.assert_any_await(
            "/open/cs2/v1/price/avg",
            {"marketHashName": "AK-47 | Redline (Field-Tested)"},
            300,
        )
        self.assertEqual(market_get.await_count, 2)
        market_post.assert_awaited_once_with(
            "/open/cs2/item/v1/kline",
            {"marketHashName": "AK-47 | Redline (Field-Tested)", "type": 2},
        )

    async def test_unresolved_item_never_falls_through_to_chat_model(self) -> None:
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
            name_resolver=AsyncMock(return_value=None),
        )

        result = await service.execute("查询 不存在的东西")

        self.assertIn("不会交给聊天模型猜", result or "")

    async def test_special_style_kline_strips_selector_and_carries_verified_code(self) -> None:
        resolver = AsyncMock(return_value="★ Karambit | Doppler (Factory New)")
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
            name_resolver=resolver,
        )

        result = await service.execute("K线 爪子刀 多普勒 红宝石 崭新")

        self.assertEqual(
            result,
            ItemKlineScreenshotRequest(
                "★ Karambit | Doppler (Factory New)",
                special_style="ruby",
            ),
        )
        resolver.assert_awaited_once_with("爪子刀 多普勒 崭新")

    async def test_special_style_price_and_analysis_refuse_generic_price_mix(self) -> None:
        market_get = AsyncMock()
        market_post = AsyncMock()
        service = ChatCommandService(
            market_get=market_get,
            market_post=market_post,
            name_resolver=AsyncMock(return_value="★ Karambit | Doppler (Factory New)"),
        )

        price = await service.execute("查询 爪子刀 多普勒 红宝石 崭新")
        analysis = await service.execute("分析 爪子刀 多普勒 P2 崭新")

        self.assertIn("不拿普通款报价冒充", price or "")
        self.assertIn("只支持K线", analysis or "")
        market_get.assert_not_awaited()
        market_post.assert_not_awaited()

    async def test_unrelated_text_falls_through_to_llm_layer(self) -> None:
        service = ChatCommandService(market_get=AsyncMock(), market_post=AsyncMock())

        self.assertIsNone(await service.execute("今天过得怎么样"))

    async def test_telemetry_failure_never_hides_a_completed_command(self) -> None:
        service = ChatCommandService(market_get=AsyncMock(), market_post=AsyncMock())

        with patch(
            "src.plugins.chat.commands.telemetry.record",
            AsyncMock(side_effect=RuntimeError("forced telemetry failure")),
        ):
            result = await service.execute("menu")

        self.assertIn("菜单", result or "")

    async def test_telemetry_failure_never_replaces_the_command_error(self) -> None:
        service = ChatCommandService(
            market_get=AsyncMock(side_effect=ValueError("market failure")),
            market_post=AsyncMock(),
        )

        with (
            patch(
                "src.plugins.chat.commands.telemetry.record",
                AsyncMock(side_effect=RuntimeError("forced telemetry failure")),
            ),
            self.assertRaisesRegex(ValueError, "market failure"),
        ):
            await service.execute("大盘")

    async def test_concurrent_broad_commands_share_one_upstream_request(self) -> None:
        market_get = AsyncMock(
            return_value={
                "success": True,
                "data": {
                    "broadMarketIndex": 1000,
                    "diffYesterday": 0,
                    "diffYesterdayRatio": 0,
                    "historyMarketIndexList": [],
                    "updateTime": time.time(),
                },
            }
        )
        service = ChatCommandService(market_get=market_get, market_post=AsyncMock())

        results = await asyncio.gather(*(service.execute("大盘") for _ in range(20)))

        self.assertTrue(all("大盘点位: 1000.00" in (result or "") for result in results))
        market_get.assert_awaited_once_with("/open/cs2/broad/v1/index")

    async def test_broad_command_uses_injected_temperature_provider(self) -> None:
        broad = {
            "success": True,
            "data": {
                "broadMarketIndex": 1000,
                "diffYesterday": -1,
                "diffYesterdayRatio": -0.1,
                "historyMarketIndexList": [],
                "updateTime": time.time(),
            },
        }
        provider = AsyncMock(return_value=TemperatureUnavailable())
        service = ChatCommandService(
            market_get=AsyncMock(return_value=broad),
            market_post=AsyncMock(),
            temperature_provider=provider,
        )

        result = await service.execute("大盘")

        provider.assert_awaited_once_with(broad)
        self.assertIn("温度暂不可用: 历史样本不足", result or "")

    async def test_temperature_failure_does_not_hide_valid_broad_market(self) -> None:
        broad = {
            "success": True,
            "data": {
                "broadMarketIndex": 1000,
                "diffYesterday": -1,
                "diffYesterdayRatio": -0.1,
                "historyMarketIndexList": [],
                "updateTime": time.time(),
            },
        }
        service = ChatCommandService(
            market_get=AsyncMock(return_value=broad),
            market_post=AsyncMock(),
            temperature_provider=AsyncMock(side_effect=RuntimeError("offline")),
        )

        result = await service.execute("大盘")

        self.assertIn("大盘点位: 1000.00", result or "")
        self.assertIn("温度暂不可用: 历史样本不足", result or "")

    async def test_movement_reason_is_not_swallowed_by_ranking_fallback(self) -> None:
        ranking_provider = AsyncMock(return_value="ranking-output")
        column_provider = AsyncMock(return_value="column-output")
        service = ChatCommandService(
            market_get=AsyncMock(),
            market_post=AsyncMock(),
            ranking_provider=ranking_provider,
            market_column_provider=column_provider,
        )

        # 异动原因 must not hit the ranking fallback ("榜单参数格式不对").
        result = await service.execute("异动原因 AK-47 | Redline (Field-Tested)")
        self.assertIsNone(result)
        ranking_provider.assert_not_awaited()
        column_provider.assert_not_awaited()

        # Genuine ranking shapes still dispatch to the provider.
        for command in ("异动榜 20", "异动 刀具 20", "涨跌榜 印花 20", "流动性榜 手套"):
            with self.subTest(command=command):
                result = await service.execute(command)
                self.assertEqual(result, "ranking-output")

        # Bare board name still dispatches to the column provider.
        result = await service.execute("异动")
        self.assertEqual(result, "column-output")


class KlineHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_dispatches_exact_kline_to_image_path_only(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = SimpleNamespace(user_id=101, get_plaintext=lambda: "k线")

        with (
            patch.object(chat, "skey", return_value="test-session"),
            patch.object(chat._CHAT_RATE_LIMITER, "allow", AsyncMock(return_value=True)),
            patch.object(chat, "cmd", AsyncMock(return_value=BROAD_KLINE_SCREENSHOT)),
            patch.object(chat, "_send_broad_kline_reply", AsyncMock()) as send_kline,
            patch.object(chat, "_send_text_reply", AsyncMock()) as send_text,
            patch.object(chat, "deepseek_chat", AsyncMock()) as llm,
        ):
            await chat._handle_chat_message(bot, event)

        send_kline.assert_awaited_once_with(bot, event, high_definition=False, max_days_ago=None)
        send_text.assert_not_awaited()
        llm.assert_not_awaited()

    async def test_handler_dispatches_named_kline_to_item_image_path(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = SimpleNamespace(user_id=101, get_plaintext=lambda: "k线 怪兽在B 崭新")
        request = ItemKlineScreenshotRequest("AK-47 | B the Monster (Factory New)")

        with (
            patch.object(chat, "skey", return_value="test-session"),
            patch.object(chat._CHAT_RATE_LIMITER, "allow", AsyncMock(return_value=True)),
            patch.object(chat, "cmd", AsyncMock(return_value=request)),
            patch.object(chat, "_send_item_kline_reply", AsyncMock()) as send_kline,
            patch.object(chat, "_send_text_reply", AsyncMock()) as send_text,
            patch.object(chat, "deepseek_chat", AsyncMock()) as llm,
        ):
            await chat._handle_chat_message(bot, event)

        send_kline.assert_awaited_once_with(
            bot,
            event,
            request.market_name,
            high_definition=False,
            special_style=None,
            platform=None,
        )
        send_text.assert_not_awaited()
        llm.assert_not_awaited()

    async def test_image_reply_sends_onebot_image_segment(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = object()
        png = b"\x89PNG\r\n\x1a\n" + b"image"

        with patch.object(
            chat.steamdt_kline_screenshots,
            "get_broad_png",
            AsyncMock(return_value=png),
        ):
            await chat._send_broad_kline_reply(bot, event)

        sent = bot.send.await_args.args[1]
        self.assertEqual(sent.type, "image")
        self.assertTrue(str(sent.data["file"]).startswith("base64://"))

    async def test_capture_failure_gets_specific_text_fallback(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = object()

        with patch.object(
            chat.steamdt_kline_screenshots,
            "get_broad_png",
            AsyncMock(side_effect=KlineScreenshotError("private detail")),
        ):
            await chat._send_broad_kline_reply(bot, event)

        sent = bot.send.await_args.args[1]
        self.assertIn("当前K线图获取失败", str(sent))
        self.assertNotIn("private detail", str(sent))

    async def test_item_image_reply_sends_onebot_image_segment(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = object()
        png = b"\x89PNG\r\n\x1a\n" + b"item-image"
        market_name = "AK-47 | B the Monster (Factory New)"

        with patch.object(
            chat.steamdt_kline_screenshots,
            "get_item_png",
            AsyncMock(return_value=png),
        ) as get_item:
            await chat._send_item_kline_reply(bot, event, market_name)

        get_item.assert_awaited_once_with(market_name, special_style=None, platform=None)
        sent = bot.send.await_args.args[1]
        self.assertEqual(sent.type, "image")
        self.assertTrue(str(sent.data["file"]).startswith("base64://"))

    async def test_item_image_failure_is_specific_and_hides_private_detail(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = object()

        with patch.object(
            chat.steamdt_kline_screenshots,
            "get_item_png",
            AsyncMock(side_effect=KlineScreenshotError("private detail")),
        ):
            await chat._send_item_kline_reply(bot, event, "item")

        sent = bot.send.await_args.args[1]
        self.assertIn("当前饰品K线图获取失败", str(sent))
        self.assertNotIn("private detail", str(sent))


class RankingParseTests(unittest.TestCase):
    def test_numeric_token_is_a_limit_not_a_category(self) -> None:
        request = parse_ranking_command("涨跌榜 20")
        assert request is not None
        self.assertEqual(request.board, "涨跌榜")
        self.assertEqual(request.category, "")
        self.assertEqual(request.limit, 20)
        self.assertTrue(request.parameterized)

    def test_numeric_token_with_board_aliases(self) -> None:
        request = parse_ranking_command("异动榜 15")
        assert request is not None
        self.assertEqual(request.board, "异动")
        self.assertEqual(request.category, "")
        self.assertEqual(request.limit, 15)

    def test_category_and_limit_both_parse(self) -> None:
        request = parse_ranking_command("涨跌榜 印花 20")
        assert request is not None
        self.assertEqual(request.category, "印花")
        self.assertEqual(request.limit, 20)

    def test_category_only_keeps_default_limit(self) -> None:
        request = parse_ranking_command("涨跌榜 印花")
        assert request is not None
        self.assertEqual(request.category, "印花")
        self.assertEqual(request.limit, 10)

    def test_plain_board_keeps_default_limit(self) -> None:
        request = parse_ranking_command("涨跌榜")
        assert request is not None
        self.assertEqual(request.category, "")
        self.assertEqual(request.limit, 10)

    def test_unparseable_input_returns_none(self) -> None:
        self.assertIsNone(parse_ranking_command("涨跌榜 印花 20 30"))
        self.assertIsNone(parse_ranking_command("随便说点什么"))
        self.assertIsNone(parse_ranking_command(""))


if __name__ == "__main__":
    unittest.main()


def test_ranking_parse_accepts_total_alias_and_reversed_order() -> None:
    from src.plugins.chat.commands import parse_ranking_command

    r = parse_ranking_command("涨跌榜 总榜")
    assert r is not None and r.category == "" and r.limit == 10
    r = parse_ranking_command("流动性榜 总榜 20")
    assert r is not None and r.category == "" and r.limit == 20
    r = parse_ranking_command("涨跌榜 20 印花")
    assert r is not None and r.category == "印花" and r.limit == 20
    r = parse_ranking_command("涨跌榜 全部")
    assert r is not None and r.category == ""
    assert parse_ranking_command("涨跌榜 20 印花 15") is None


@pytest.mark.asyncio
async def test_menu_lists_new_features() -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=())

    from src.plugins.chat.__init__ import _command_service

    result = await _command_service.execute("菜单")
    text = str(result)
    for keyword in ("预测", "盘面解读", "CS2爆料", "官方价", "总榜"):
        assert keyword in text

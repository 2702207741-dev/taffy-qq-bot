from __future__ import annotations

import asyncio
import math
import unittest
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.services import market_temperature as temperature_module
from src.services.market_temperature import (
    MarketTemperature,
    MarketTemperatureService,
    TemperatureUnavailable,
    calculate_market_temperature,
    classify_temperature,
    parse_daily_closes,
)

TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
CURRENT = datetime(2026, 7, 13, 18, 55, 10, tzinfo=TZ)


def _closes_for(current_date: date, values: list[float]) -> dict[date, float]:
    dates = [current_date - timedelta(days=days) for days in range(7, 0, -1)]
    return dict(zip(dates, values, strict=True))


def _payload_for(
    current: datetime,
    values: list[float],
    *,
    current_value: float = 9999.0,
) -> dict[str, Any]:
    closes = _closes_for(current.date(), values)
    rows: list[list[float | str]] = [
        [str(datetime.combine(day, datetime.min.time(), TZ).timestamp()), close]
        for day, close in closes.items()
    ]
    # The live endpoint includes today's unfinished daily point. The service
    # must keep using the current broad index rather than this value.
    rows.append([current.timestamp(), current_value])
    return {"success": True, "data": rows}


def _broad(current: datetime = CURRENT, index: float = 917.14) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "broadMarketIndex": index,
            "updateTime": current.timestamp(),
        },
    }


class MutableClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class LiquidityFactorTests(unittest.TestCase):
    """窗口B：流动性突变第 5 因子（挂单深度代理，cs2.sh 未接前）。"""

    def test_missing_liquidity_keeps_four_factor_weights(self) -> None:
        result = calculate_market_temperature(
            current_index=917.14,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(
                CURRENT.date(),
                [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45],
            ),
            now=CURRENT.timestamp(),
            liquidity_ratio=None,
        )
        assert isinstance(result, MarketTemperature)
        # 与无 liquidity_ratio 参数的既有输出完全一致
        baseline = calculate_market_temperature(
            current_index=917.14,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(
                CURRENT.date(),
                [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45],
            ),
            now=CURRENT.timestamp(),
        )
        assert isinstance(baseline, MarketTemperature)
        self.assertEqual(result.score, baseline.score)
        self.assertIsNone(result.liquidity_ratio)
        self.assertIsNone(result.liquidity_note)

    def test_liquidity_surge_moves_score_and_notes_proxy(self) -> None:
        result = calculate_market_temperature(
            current_index=917.14,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(
                CURRENT.date(),
                [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45],
            ),
            now=CURRENT.timestamp(),
            liquidity_ratio=2.5,
        )
        assert isinstance(result, MarketTemperature)
        self.assertIsNotNone(result.liquidity_note)
        assert result.liquidity_note is not None
        self.assertIn("流动性突变", result.liquidity_note)
        self.assertIn("挂单深度代理", result.liquidity_note)
        # 2.5× → 因子 (2.5-1)/0.5=3 → clip 1.0，权重 0.20 全额计入 → 分数显著上升
        self.assertGreater(result.score, -14.3)

    def test_liquidity_collapse_moves_score_down(self) -> None:
        result = calculate_market_temperature(
            current_index=917.14,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(
                CURRENT.date(),
                [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45],
            ),
            now=CURRENT.timestamp(),
            liquidity_ratio=0.4,
        )
        assert isinstance(result, MarketTemperature)
        # 0.4× → (0.4-1)/0.5 = -1.2 → clip -1.0 → 权重 0.20 全额计入负向
        self.assertLess(result.score, -14.3)


class TemperatureFormulaTests(unittest.TestCase):
    def test_current_sample_matches_fixed_four_factor_formula(self) -> None:
        result = calculate_market_temperature(
            current_index=917.14,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(
                CURRENT.date(),
                [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45],
            ),
            now=CURRENT.timestamp(),
        )

        self.assertIsInstance(result, MarketTemperature)
        assert isinstance(result, MarketTemperature)
        self.assertEqual(result.score, -14.3)
        self.assertAlmostEqual(result.change_1d, -0.58, places=2)
        self.assertAlmostEqual(result.change_3d, -0.78, places=2)
        self.assertAlmostEqual(result.change_7d, -0.99, places=2)
        self.assertEqual(result.current_state, "低位承压")
        self.assertEqual(result.detail, "1/3/7日动量同步偏弱")
        self.assertEqual(result.temperature_state, "偏冷")

    def test_2026_06_19_replay_matches_calibrated_sample(self) -> None:
        current = datetime(2026, 6, 19, 17, 5, tzinfo=TZ)
        result = calculate_market_temperature(
            current_index=909.45,
            current_timestamp=current.timestamp(),
            daily_closes=_closes_for(
                current.date(),
                [943.25, 932.65, 931.29, 934.54, 946.62, 944.88, 929.48],
            ),
            now=current.timestamp(),
        )

        self.assertIsInstance(result, MarketTemperature)
        assert isinstance(result, MarketTemperature)
        self.assertEqual(result.score, -57.9)
        self.assertEqual((result.current_state, result.temperature_state), ("弱势下行", "明显偏冷"))

    def test_factor_clipping_and_negative_zero_normalisation(self) -> None:
        high = calculate_market_temperature(
            current_index=200,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(CURRENT.date(), [100] * 7),
            now=CURRENT.timestamp(),
        )
        low = calculate_market_temperature(
            current_index=1,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(CURRENT.date(), [100] * 7),
            now=CURRENT.timestamp(),
        )
        neutral = calculate_market_temperature(
            current_index=100,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(CURRENT.date(), [100] * 7),
            now=CURRENT.timestamp(),
        )

        self.assertEqual(getattr(high, "score", None), 100.0)
        self.assertEqual(getattr(low, "score", None), -100.0)
        self.assertEqual(getattr(neutral, "score", None), 0.0)
        self.assertEqual(math.copysign(1, getattr(neutral, "score", -1)), 1)

    def test_score_uses_conventional_half_up_rounding(self) -> None:
        self.assertEqual(temperature_module._round_score(14.25), 14.3)
        self.assertEqual(temperature_module._round_score(-14.25), -14.3)
        self.assertEqual(str(temperature_module._round_score(-0.01)), "0.0")

    def test_state_boundaries_are_exact(self) -> None:
        cases = (
            (-70.0, ("极端下行", "极冷")),
            (-69.9, ("弱势下行", "明显偏冷")),
            (-35.0, ("弱势下行", "明显偏冷")),
            (-34.9, ("低位承压", "偏冷")),
            (-10.0, ("低位承压", "偏冷")),
            (-9.9, ("多空平衡", "中性")),
            (9.9, ("多空平衡", "中性")),
            (10.0, ("温和回暖", "偏热")),
            (35.0, ("强势上行", "明显偏热")),
            (70.0, ("高位过热", "极热")),
        )
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(classify_temperature(score), expected)

    def test_short_reversal_details_are_explainable(self) -> None:
        rebound = calculate_market_temperature(
            current_index=101,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(CURRENT.date(), [110, 108, 106, 104, 103, 99, 100]),
            now=CURRENT.timestamp(),
        )
        pullback = calculate_market_temperature(
            current_index=99,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(CURRENT.date(), [90, 92, 94, 96, 97, 101, 100]),
            now=CURRENT.timestamp(),
        )

        self.assertEqual(getattr(rebound, "detail", ""), "短线反弹，7日趋势仍弱")
        self.assertEqual(getattr(pullback, "detail", ""), "短线回撤，7日趋势仍强")

    def test_incomplete_or_stale_inputs_fail_closed(self) -> None:
        complete = _closes_for(CURRENT.date(), [100] * 7)
        del complete[CURRENT.date() - timedelta(days=4)]
        stale = calculate_market_temperature(
            current_index=100,
            current_timestamp=CURRENT.timestamp() - 901,
            daily_closes=_closes_for(CURRENT.date(), [100] * 7),
            now=CURRENT.timestamp(),
        )
        incomplete = calculate_market_temperature(
            current_index=100,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=complete,
            now=CURRENT.timestamp(),
        )

        self.assertIsInstance(stale, TemperatureUnavailable)
        self.assertIsInstance(incomplete, TemperatureUnavailable)


class DailyHistoryParsingTests(unittest.TestCase):
    def test_same_day_unfinished_point_is_not_used_in_temperature(self) -> None:
        values = [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45]
        closes = parse_daily_closes(_payload_for(CURRENT, values))
        result = calculate_market_temperature(
            current_index=917.14,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=closes,
            now=CURRENT.timestamp(),
        )

        self.assertEqual(getattr(result, "score", None), -14.3)
        self.assertEqual(closes[CURRENT.date()], 9999.0)

    def test_duplicate_disordered_nonfinite_and_malformed_rows_are_rejected(self) -> None:
        timestamp = CURRENT.replace(hour=0, minute=0, second=0).timestamp()
        invalid_payloads: tuple[Mapping[str, Any], ...] = (
            {"success": True, "data": [[timestamp, 100], [timestamp + 60, 101]]},
            {"success": True, "data": [[timestamp, 100], [timestamp - 86400, 101]]},
            {"success": True, "data": [[timestamp, float("nan")]]},
            {"success": True, "data": [[timestamp, 100, "extra"]]},
            {"success": True, "data": [[timestamp, 0]]},
            {"success": False, "data": []},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_daily_closes(payload)


class MarketTemperatureCacheTests(unittest.IsolatedAsyncioTestCase):
    values = [926.29, 920.25, 918.01, 929.67, 924.39, 927.56, 922.45]

    async def test_concurrent_requests_share_one_refresh_and_fresh_cache(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fetcher() -> Mapping[str, Any]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return _payload_for(CURRENT, self.values)

        clock = MutableClock()
        service = MarketTemperatureService(
            fetcher=fetcher,
            clock=clock,
            wall_clock=lambda: CURRENT.timestamp(),
        )
        tasks = [asyncio.create_task(service.evaluate(_broad())) for _ in range(12)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)
        cached = await service.evaluate(_broad())

        self.assertEqual(calls, 1)
        self.assertTrue(all(isinstance(result, MarketTemperature) for result in results))
        self.assertIsInstance(cached, MarketTemperature)

    async def test_refresh_failure_uses_only_cache_younger_than_six_hours(self) -> None:
        clock = MutableClock()
        calls = 0

        async def fetcher() -> Mapping[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _payload_for(CURRENT, self.values)
            raise RuntimeError("offline")

        service = MarketTemperatureService(
            fetcher=fetcher,
            clock=clock,
            wall_clock=lambda: CURRENT.timestamp(),
        )
        first = await service.evaluate(_broad())
        clock.value = 30 * 60 + 1
        stale_fallback = await service.evaluate(_broad())
        clock.value = 6 * 60 * 60 + 1
        expired = await service.evaluate(_broad())

        self.assertIsInstance(first, MarketTemperature)
        self.assertIsInstance(stale_fallback, MarketTemperature)
        self.assertIsInstance(expired, TemperatureUnavailable)
        self.assertEqual(calls, 3)

    async def test_cached_history_must_still_cover_every_previous_date(self) -> None:
        payload = _payload_for(CURRENT, self.values)
        payload["data"].pop(3)

        async def fetcher() -> Mapping[str, Any]:
            return payload

        service = MarketTemperatureService(
            fetcher=fetcher,
            wall_clock=lambda: CURRENT.timestamp(),
        )

        self.assertIsInstance(await service.evaluate(_broad()), TemperatureUnavailable)

    async def test_daily_chart_builds_fresh_broad_fallback_during_primary_limit(self) -> None:
        async def fetcher() -> Mapping[str, Any]:
            return _payload_for(CURRENT, self.values, current_value=917.14)

        service = MarketTemperatureService(
            fetcher=fetcher,
            wall_clock=lambda: CURRENT.timestamp(),
        )

        result = await service.broad_market_fallback()

        self.assertTrue(result["success"])
        market = result["data"]
        self.assertEqual(market["broadMarketIndex"], 917.14)
        self.assertAlmostEqual(market["diffYesterday"], -5.31, places=2)
        self.assertAlmostEqual(market["diffYesterdayRatio"], -0.58, places=2)
        self.assertEqual(market["historyMarketIndexList"], [])
        self.assertEqual(market["updateTime"], CURRENT.timestamp())

    async def test_broad_fallback_rejects_daily_chart_older_than_fifteen_minutes(self) -> None:
        async def fetcher() -> Mapping[str, Any]:
            return _payload_for(CURRENT, self.values, current_value=917.14)

        service = MarketTemperatureService(
            fetcher=fetcher,
            wall_clock=lambda: CURRENT.timestamp() + 15 * 60 + 1,
        )

        self.assertFalse((await service.broad_market_fallback())["success"])

    async def test_broad_fallback_refreshes_current_point_before_history_cache_expires(
        self,
    ) -> None:
        wall_clock = MutableClock(CURRENT.timestamp())
        calls = 0

        async def fetcher() -> Mapping[str, Any]:
            nonlocal calls
            calls += 1
            current = datetime.fromtimestamp(wall_clock.value, TZ)
            return _payload_for(current, self.values, current_value=917.14 - calls / 100)

        service = MarketTemperatureService(
            fetcher=fetcher,
            clock=MutableClock(),
            wall_clock=wall_clock,
        )
        first = await service.broad_market_fallback()
        wall_clock.value += 15 * 60 + 1
        refreshed = await service.broad_market_fallback()

        self.assertTrue(first["success"])
        self.assertTrue(refreshed["success"])
        self.assertEqual(calls, 2)
        self.assertEqual(refreshed["data"]["updateTime"], wall_clock.value)


class FiveFactorDetailCoverageTests(unittest.TestCase):
    """兜底描述必须覆盖全部 5 个因子下标（M-01）。

    ``LiquidityFactorTests`` 的三个用例都让 1/3/7 日动量同向，在
    ``_temperature_detail`` 的早退分支就返回了，从未走到「按贡献绝对值取主导
    因子」的兜底路径 —— 这正是 5 因子接入后 ``descriptions`` 仍只有 4 项却没被
    发现的原因。此处显式枚举每一个可能的主导因子下标。
    """

    #: 1/3/7 日动量方向不一致（change_1d == 0）→ 四个早退分支全部落空
    INCONSISTENT_CHANGES = (0.0, -1.0, 1.0)

    def test_description_table_covers_every_weighted_factor(self) -> None:
        """结构不变量：描述表的槽位数必须覆盖每一条权重路径。

        枚举上界刻意**从权重元组导出**而非硬编码字面量：否则将来接入第 6 个
        因子（M-38 计划的 cs2.sh 真实成交量）却忘记补描述时，硬编码的
        ``range(5)`` 仍会让用例通过，而线上在 ``_FACTOR_DESCRIPTION_PAIRS[5]``
        复现同一个 IndexError。
        """

        description_slots = len(temperature_module._FACTOR_DESCRIPTION_PAIRS)
        for weights in (
            temperature_module._FOUR_FACTOR_WEIGHTS,
            temperature_module._FIVE_FACTOR_WEIGHTS,
        ):
            with self.subTest(slots=len(weights)):
                self.assertLessEqual(len(weights), description_slots)

    def test_fallback_detail_covers_every_factor_index(self) -> None:
        slot_count = len(temperature_module._FIVE_FACTOR_WEIGHTS)
        for dominant in range(slot_count):
            contributions = tuple(0.9 if index == dominant else 0.0 for index in range(slot_count))
            with self.subTest(dominant_factor=dominant):
                detail = temperature_module._temperature_detail(
                    *self.INCONSISTENT_CHANGES, contributions
                )
                self.assertIsInstance(detail, str)
                self.assertTrue(detail)

    def test_liquidity_dominant_score_still_returns_a_detail(self) -> None:
        result = calculate_market_temperature(
            current_index=100.0,
            current_timestamp=CURRENT.timestamp(),
            daily_closes=_closes_for(CURRENT.date(), [100.0] * 7),
            now=CURRENT.timestamp(),
            liquidity_ratio=3.0,
        )

        self.assertIsInstance(result, MarketTemperature)
        assert isinstance(result, MarketTemperature)
        self.assertTrue(result.detail)
        # 四项动量贡献为 0，仅流动性因子计入 1.0 × 0.20 → 20.0
        self.assertEqual(result.score, 20.0)

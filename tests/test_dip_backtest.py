from __future__ import annotations

import unittest
from datetime import date, timedelta

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.chat.commands import ChatCommandService
from src.services.dip_backtest import (
    MIN_EPISODES,
    build_dip_report,
    count_episodes,
    crosses_regime_break,
    detect_signal_days,
    format_dip_report,
    percentile_value,
    scenario_outcomes,
    segment_stats,
)


def _series(values: list[float], start: date = date(2026, 1, 1)) -> list[tuple[date, float]]:
    return [(start + timedelta(days=index), value) for index, value in enumerate(values)]


class DipBacktestTests(unittest.TestCase):
    def test_percentile_value_uses_nearest_rank(self) -> None:
        values = sorted(range(100, 200))
        self.assertEqual(percentile_value(values, 0.15), 114)
        self.assertEqual(percentile_value(values, 0.05), 104)
        self.assertEqual(percentile_value(values, 1.0), 199)

    def test_signal_detection_uses_only_prior_closes(self) -> None:
        flat = [100.0] * 70
        drop_early = _series(flat + [50.0] + [100.0] * 29)
        drop_late = _series(flat + [50.0] + [100.0] * 10 + [50.0] * 19)

        signals_early = detect_signal_days(drop_early, band=0.15)
        signals_late = detect_signal_days(drop_late, band=0.15)
        # Both series are identical through day 75; signal membership for those
        # days must be identical no matter how the future continues.
        early_prefix = [index for index in signals_early if index <= 75]
        late_prefix = [index for index in signals_late if index <= 75]
        self.assertEqual(early_prefix, late_prefix)
        self.assertEqual(early_prefix, [70])

    def test_episode_merging_separates_distant_clusters(self) -> None:
        closes = _series([100.0] * 200)
        self.assertEqual(count_episodes([10, 11, 12], closes), 1)
        self.assertEqual(count_episodes([10, 35], closes), 2)
        self.assertEqual(count_episodes([10, 12, 35, 60, 61], closes), 3)

    def test_scenario_outcomes_skip_days_without_forward_data(self) -> None:
        closes = _series([100.0, 80.0, 100.0, 120.0, 100.0])
        outcomes = scenario_outcomes(closes, [1], horizons=(7,))
        self.assertEqual(outcomes[0].scenarios, 0)

    def test_report_insufficient_samples(self) -> None:
        report = build_dip_report(_series([100.0] * 10))
        self.assertEqual(report.verdict, "数据不足")
        self.assertIn("至少需要 30", report.detail)

    def test_report_no_signal_is_observation(self) -> None:
        rising = _series([float(100 + index) for index in range(120)])
        report = build_dip_report(rising)
        self.assertEqual(report.verdict, "观察")
        self.assertIsNone(report.triggered_band)

    def test_report_deep_dip_passes_gates_with_independent_episodes(self) -> None:
        # Five separate dips, each printing a new local low (80 -> 72).  A dip
        # only signals while its close is strictly below the prior band, so the
        # staircase keeps each episode observable without merging the clusters.
        values = [100.0] * 220
        for episode, low in enumerate((80.0, 78.0, 76.0, 74.0, 72.0)):
            start = 70 + episode * 30
            values[start : start + 3] = [low] * 3
        # The final close is a fresh low, strictly below its prior window band.
        values[-1] = 60.0

        report = build_dip_report(_series(values))
        self.assertEqual(report.verdict, "深跌区")
        self.assertEqual(report.triggered_band, 0.05)
        primary = next(outcome for outcome in report.outcomes if outcome.horizon_days == 14)
        self.assertGreaterEqual(primary.episodes, MIN_EPISODES)
        self.assertEqual(primary.win_rate, 1.0)
        self.assertGreater(report.current_percentile, 0.0)
        self.assertLess(report.current_percentile, 0.5)

    def test_report_insufficient_episodes_is_evidence_gap(self) -> None:
        values = [100.0] * 120
        for episode, low in enumerate((80.0, 78.0)):
            start = 60 + episode * 30
            values[start : start + 3] = [low] * 3
        values[-1] = 60.0

        report = build_dip_report(_series(values))
        self.assertEqual(report.verdict, "证据不足")
        self.assertIn("继续积累样本", report.detail)

    def test_format_includes_disclaimer_and_verdict(self) -> None:
        report = build_dip_report(_series([100.0] * 10))
        text = format_dip_report(report)
        self.assertIn("不构成交易建议", text)
        self.assertIn("数据不足", text)


class RegimeBreakTests(unittest.TestCase):
    """窗口B：2025-07-15 / 2025-10-22 两个 regime 断点的覆盖与分段统计。"""

    def test_crosses_regime_break_detects_known_breaks(self) -> None:
        before = _series([100.0] * 30, start=date(2025, 5, 1))
        self.assertFalse(crosses_regime_break(before))
        spanning = _series([100.0] * 100, start=date(2025, 6, 1))
        self.assertTrue(crosses_regime_break(spanning))
        # 精确命中断点当天也算跨越
        exact = [(date(2025, 7, 15) + timedelta(days=i), 100.0) for i in range(60)]
        self.assertTrue(crosses_regime_break(exact))

    def test_segment_stats_split_around_break(self) -> None:
        closes = _series([100.0] * 200, start=date(2025, 5, 1))
        signals = [i for i in range(40, 160) if i % 7 == 0]
        pre, post = segment_stats(closes, signals, horizon=14)
        self.assertIsNotNone(pre)
        self.assertIsNotNone(post)
        assert pre is not None and post is not None
        self.assertEqual(pre["scenarios"] + post["scenarios"], len(signals))
        self.assertGreaterEqual(float(pre["win_rate"]), 0.0)
        self.assertGreaterEqual(float(post["win_rate"]), 0.0)

    def test_report_notes_regime_crossing_in_output(self) -> None:
        # 样本跨越 2025-10-22 汰换合同断点
        values = [100.0] * 200
        values[-1] = 60.0  # 触发低位档位
        report = build_dip_report(_series(values, start=date(2025, 9, 1)))
        self.assertTrue(report.crosses_regime)
        text = format_dip_report(report)
        self.assertIn("结构性事件", text)
        self.assertIn("仅供参考", text)

    def test_no_regime_crossing_no_note(self) -> None:
        values = [100.0] * 200
        values[-1] = 60.0
        report = build_dip_report(_series(values, start=date(2026, 1, 1)))
        self.assertFalse(report.crosses_regime)
        text = format_dip_report(report)
        self.assertNotIn("结构性事件", text)


class DipCommandRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_command_service_routes_dip(self) -> None:
        async def dip_card() -> str:
            return "DIP-CARD"

        service = ChatCommandService(
            market_get=None,  # type: ignore[arg-type]
            market_post=None,  # type: ignore[arg-type]
            dip_provider=dip_card,
        )
        result = await service.execute("抄底")
        self.assertEqual(result, "DIP-CARD")


if __name__ == "__main__":
    unittest.main()

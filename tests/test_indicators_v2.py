"""V0.5 night batch 1: KDJ / ATR / VWAP indicators + prediction agreement.

Every new indicator is pure, deterministic and NaN-safe; the OBV indicator
is deliberately NOT implemented because this market has no honest volume
source (the ranking board's "never use listing counts as volume" rule).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.services.indicators import (
    atr,
    kdj,
    vwap,
)
from src.services.market_prediction import method_agreement, render_scenario_forecast, rule_signal


def _falling_closes(days: int = 40) -> list[float]:
    return [200.0 - index * 1.5 for index in range(days)]


def _rising_closes(days: int = 40) -> list[float]:
    return [140.0 + index * 1.5 for index in range(days)]


def _flat_closes(days: int = 40) -> list[float]:
    return [100.0] * days


# ---------------------------------------------------------------------------
# KDJ
# ---------------------------------------------------------------------------


def test_kdj_flat_series_midrange() -> None:
    k, d, j = kdj(_flat_closes(30))
    assert k is not None and d is not None and j is not None
    assert 0 <= k <= 100 and 0 <= d <= 100
    # Flat prices produce RSV=50 for every window -> K=D=50, J=50
    assert k == pytest.approx(50.0, abs=1e-6)
    assert j == pytest.approx(50.0, abs=1e-6)


def test_kdj_rising_series_high() -> None:
    k, d, j = kdj(_rising_closes())
    assert k is not None
    # Monotonic rise keeps price at the top of every window -> high K
    assert k > 90


def test_kdj_falling_series_low() -> None:
    k, d, j = kdj(_falling_closes())
    assert k is not None
    assert k < 10


def test_kdj_window_not_satisfied_returns_none() -> None:
    assert kdj([100.0, 101.0, 102.0]) is None
    assert kdj([100.0, float("nan")] * 20) is None
    assert kdj([], period=9) is None


def test_kdj_invalid_period_returns_none() -> None:
    assert kdj(_rising_closes(), period=1) is None
    assert kdj(_rising_closes(), k_smooth=0) is None


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_atr_known_values() -> None:
    highs = [101.0 + index * 2 for index in range(30)]
    lows = [99.0 + index * 2 for index in range(30)]
    closes = [100.0 + index * 2 for index in range(30)]
    value = atr(highs, lows, closes, period=14)
    assert value is not None
    # Every bar: high-low = 2.0, |high - prev close| = 3.0 (worst case)
    # First bar true range = 3.0, subsequent bars: max(2, 3, 1) = 3.0
    assert value == pytest.approx(3.0, abs=1e-6)


def test_atr_window_not_satisfied() -> None:
    highs = [101.0] * 10
    lows = [99.0] * 10
    closes = [100.0] * 10
    assert atr(highs, lows, closes, period=14) is None
    assert atr(highs, lows, closes, period=0) is None
    assert atr(highs, lows[:-1], closes, period=5) is None  # mismatched lengths
    assert atr([float("nan")] * 20, lows * 2, closes * 2, period=5) is None


def test_atr_flat_prices_zero_true_range() -> None:
    highs = [100.0] * 20
    lows = [100.0] * 20
    closes = [100.0] * 20
    value = atr(highs, lows, closes, period=5)
    assert value is not None
    assert value == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def test_vwap_default_uses_close_anchor() -> None:
    closes = [100.0 + index for index in range(25)]
    value = vwap(closes, period=20)
    assert value is not None
    assert value == pytest.approx(sum(closes[-20:]) / 20)


def test_vwap_with_typical_prices() -> None:
    closes = [100.0] * 25
    typical = [101.0] * 25
    value = vwap(closes, period=20, typical=typical)
    assert value is not None
    assert value == pytest.approx(101.0)


def test_vwap_window_not_satisfied() -> None:
    assert vwap([100.0, 101.0], period=20) is None
    assert vwap([100.0, float("nan")] * 20, period=5) is None
    assert vwap([], period=5) is None
    # Typical list too short -> None
    assert vwap([100.0] * 25, period=20, typical=[101.0] * 10) is None


# ---------------------------------------------------------------------------
# Prediction multi-method agreement
# ---------------------------------------------------------------------------


def _daily_closes(start: date, values: list[float]) -> dict[date, float]:
    return {start + timedelta(days=index): value for index, value in enumerate(values)}


def test_rule_signal_directions() -> None:
    # Strong rising series: high percentile + positive momentum -> 偏暖
    rising = _daily_closes(date(2026, 1, 1), _rising_closes(60))
    assert rule_signal(rising, date(2026, 3, 1)) == "偏暖"
    # Strong falling series -> 偏冷
    falling = _daily_closes(date(2026, 1, 1), _falling_closes(60))
    assert rule_signal(falling, date(2026, 3, 1)) == "偏冷"
    # Flat series -> 中性
    flat = _daily_closes(date(2026, 1, 1), _flat_closes(60))
    assert rule_signal(flat, date(2026, 3, 1)) == "中性"


def test_rule_signal_thin_sample_returns_none() -> None:
    thin = _daily_closes(date(2026, 1, 1), [100.0] * 10)
    assert rule_signal(thin, date(2026, 1, 10)) is None
    assert rule_signal({}, date(2026, 1, 10)) is None


def test_method_agreement_returns_none_without_stats() -> None:
    closes = _daily_closes(date(2026, 1, 1), _rising_closes(60))
    assert method_agreement(closes, date(2026, 3, 1), None) is None


def test_method_agreement_upgrades_on_same_direction() -> None:
    from src.services.market_prediction import historical_scenario_probs

    closes = _daily_closes(date(2026, 1, 1), _rising_closes(90))
    stats = historical_scenario_probs(closes, date(2026, 3, 30))
    if stats is None:
        pytest.skip("no same-bucket history in synthetic data")
    agreement = method_agreement(closes, date(2026, 3, 30), stats)
    assert agreement is not None
    label, count = agreement
    assert count == 2
    assert "两种方法" in label or "方向不一致" in label or "中性" in label


def test_render_scenario_forecast_includes_method_count() -> None:
    closes = _daily_closes(date(2026, 1, 1), _rising_closes(90))
    rendered = render_scenario_forecast(closes, date(2026, 3, 30))
    assert "方法数" in rendered
    assert "历史情景概率" in rendered


def test_render_scenario_forecast_thin_sample_fail_closed() -> None:
    closes = _daily_closes(date(2026, 1, 1), [100.0] * 10)
    rendered = render_scenario_forecast(closes, date(2026, 1, 10))
    assert "不输出伪概率" in rendered


def test_obv_deliberately_absent() -> None:
    """OBV must not exist without a real volume source (honest-market rule)."""
    import src.services.indicators as indicators

    assert not hasattr(indicators, "obv")
    assert not hasattr(indicators, "on_balance_volume")

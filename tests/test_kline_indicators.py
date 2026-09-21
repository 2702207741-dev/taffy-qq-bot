"""Tests for K-line MACD/RSI indicator functions and panel rendering."""

from __future__ import annotations

import random

from src.services.kline_fast_renderer import (
    MACD_SLOW,
    RSI_PERIOD,
    _macd,
    _moving_average,
    _rsi,
    render_kline_png,
)
from src.services.steamdt_kline import validate_kline_png


def _synthetic_points(count: int, *, seed: int = 7, start: float = 100.0) -> list[list[float]]:
    random.seed(seed)
    points: list[list[float]] = []
    price = start
    for index in range(count):
        open_price = price
        close_price = open_price + random.uniform(-3.0, 3.2)
        high = max(open_price, close_price) + random.uniform(0.0, 1.5)
        low = min(open_price, close_price) - random.uniform(0.0, 1.5)
        points.append(
            [
                1_752_000_000 + index * 86_400,
                round(open_price, 2),
                round(close_price, 2),
                round(high, 2),
                round(low, 2),
            ]
        )
        price = close_price
    return points


def test_moving_average_matches_simple_mean() -> None:
    points = _synthetic_points(40)
    series = _moving_average(points, 7)
    assert series[0][0] == 6
    expected = sum(point[2] for point in points[:7]) / 7
    assert abs(series[0][1] - expected) < 1e-9


def test_macd_monotonic_uptrend_has_positive_dif() -> None:
    points = [
        [1_752_000_000 + index * 86_400, 100.0 + index, 101.0 + index, 102.0 + index, 99.0 + index]
        for index in range(60)
    ]
    dif, dea, histogram = _macd(points)
    assert len(dif) == len(dea) == len(histogram)
    assert len(dif) == 60 - MACD_SLOW + 1
    assert dif[-1][1] > 0
    assert histogram[-1][1] == (dif[-1][1] - dea[-1][1]) * 2.0


def test_macd_insufficient_history_returns_empty() -> None:
    assert _macd(_synthetic_points(20)) == ([], [], [])
    # 26 points give DIF values but not enough for a 9-period signal line.
    assert _macd(_synthetic_points(26)) == ([], [], [])


def test_macd_constant_prices_stay_at_zero() -> None:
    points = [[1_752_000_000 + index * 86_400, 50.0, 50.0, 50.0, 50.0] for index in range(50)]
    dif, dea, histogram = _macd(points)
    assert dif and dea and histogram
    assert all(abs(value) < 1e-9 for _, value in dif)
    assert all(value == 0.0 for _, value in histogram)


def test_rsi_all_gains_reaches_100_and_all_losses_0() -> None:
    rising = [
        [1_752_000_000 + index * 86_400, 100.0, 101.0 + index, 102.0, 99.0] for index in range(30)
    ]
    assert _rsi(rising)[-1][1] == 100.0

    falling = [
        [1_752_000_000 + index * 86_400, 200.0, 199.0 - index, 200.0, 198.0] for index in range(30)
    ]
    assert _rsi(falling)[-1][1] == 0.0


def test_rsi_constant_prices_are_neutral_and_short_series_empty() -> None:
    flat = [[1_752_000_000 + index * 86_400, 50.0, 50.0, 50.0, 50.0] for index in range(30)]
    series = _rsi(flat)
    assert series and all(value == 50.0 for _, value in series)
    assert _rsi(_synthetic_points(RSI_PERIOD)) == []


def test_kdj_series_rising_market_high_and_aligned() -> None:
    from src.services.kline_fast_renderer import _kdj_series

    rising = [
        [1_752_000_000 + index * 86_400, 100.0, 101.0 + index, 102.0, 99.0] for index in range(40)
    ]
    series = _kdj_series(rising)
    assert series  # window satisfied
    # Rising market keeps price at window top -> K near 100.
    assert series[-1][1][0] > 90
    # Triples align to candle indexes starting after the window.
    assert series[0][0] == len(rising) - len(series)


def test_kdj_series_flat_prices_neutral() -> None:
    from src.services.kline_fast_renderer import _kdj_series

    flat = [[1_752_000_000 + index * 86_400, 50.0, 50.0, 50.0, 50.0] for index in range(30)]
    series = _kdj_series(flat)
    assert series
    k, d, j = series[-1][1]
    assert abs(k - 50.0) < 1e-6
    assert abs(j - 50.0) < 1e-6


def test_render_kline_png_includes_indicator_panels_and_validates() -> None:
    png = render_kline_png("AK-47 | 二西莫夫 (略有磨损)", _synthetic_points(90))
    width, height = validate_kline_png(png, max_bytes=2_000_000)
    assert (width, height) == (1440, 1320)


def test_render_kline_png_skips_panels_on_short_history() -> None:
    # 34 points: RSI has data, MACD signal line does not (needs 34 total exactly).
    png = render_kline_png("短历史饰品", _synthetic_points(34))
    assert validate_kline_png(png, max_bytes=2_000_000) == (1440, 1320)


def test_render_kline_png_rejects_insufficient_history() -> None:
    import pytest

    with pytest.raises(ValueError, match="insufficient"):
        render_kline_png("样本不足", _synthetic_points(10))

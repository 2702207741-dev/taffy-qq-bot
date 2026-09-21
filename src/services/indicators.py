"""Pure technical-indicator math shared by the K-line renderer and scoring.

Every function is deterministic, dependency-free and NaN-safe: given finite
inputs it returns ``None`` (never a partial guess) whenever its sample
window is not satisfied.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

DEFAULT_RSI_PERIOD = 14
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_NDEV = 2.0
DEFAULT_KDJ_PERIOD = 9
DEFAULT_KDJ_K_SMOOTH = 3
DEFAULT_KDJ_D_SMOOTH = 3
DEFAULT_ATR_PERIOD = 14
DEFAULT_VWAP_PERIOD = 20


def _finite_series(closes: Sequence[float]) -> list[float] | None:
    values = [float(close) for close in closes]
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        return None
    return values


def wilder_rsi_series(closes: Sequence[float], *, period: int = DEFAULT_RSI_PERIOD) -> list[float]:
    """Full Wilder-smoothed RSI series; constant prices return neutral 50s."""

    if period < 1:
        return []
    values = _finite_series(closes)
    if values is None or len(values) <= period:
        return []
    gains = 0.0
    losses = 0.0
    for previous, current in zip(values[:period], values[1 : period + 1], strict=True):
        change = current - previous
        gains += max(change, 0.0)
        losses += max(-change, 0.0)

    def _value(gain: float, loss: float) -> float:
        if gain + loss == 0:
            return 50.0
        return 100.0 * gain / (gain + loss)

    series = [_value(gains, losses)]
    average_gain = gains / period
    average_loss = losses / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
        series.append(_value(average_gain, average_loss))
    return series


def wilder_rsi(closes: Sequence[float], *, period: int = DEFAULT_RSI_PERIOD) -> float | None:
    """Latest Wilder-smoothed RSI; constant prices return a neutral 50."""

    series = wilder_rsi_series(closes, period=period)
    return series[-1] if series else None


def bollinger_lower(
    closes: Sequence[float],
    *,
    period: int = DEFAULT_BOLLINGER_PERIOD,
    ndev: float = DEFAULT_BOLLINGER_NDEV,
) -> float | None:
    """Latest lower Bollinger band (SMA - ndev × population stdev)."""

    if period < 2 or ndev <= 0:
        return None
    values = _finite_series(closes)
    if values is None or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((value - mean) ** 2 for value in window) / period
    stdev = math.sqrt(variance)
    return mean - ndev * stdev


def percentile_rank(value: float, series: Sequence[float]) -> float | None:
    """Share of ``series`` entries at or below ``value`` (0..100)."""

    if not math.isfinite(value):
        return None
    values = [float(entry) for entry in series if math.isfinite(entry)]
    if not values:
        return None
    below = sum(1 for entry in values if entry <= value)
    return below / len(values) * 100


def momentum_pair(closes: Sequence[float], *, window: int = 7) -> tuple[float, float] | None:
    """``(recent, prior)`` simple momentum percentages over ``window`` closes.

    ``recent`` spans the last ``window`` closes; ``prior`` the ``window``
    before that.  ``None`` when 2×window+1 closes are unavailable.
    """

    if window < 1:
        return None
    values = _finite_series(closes)
    if values is None or len(values) < 2 * window + 1:
        return None
    recent = (values[-1] / values[-1 - window] - 1) * 100
    prior = (values[-1 - window] / values[-1 - 2 * window] - 1) * 100
    return recent, prior


def return_z_score(closes: Sequence[float], *, window: int = 14) -> float | None:
    """Z-score of the latest daily return against the prior ``window - 1`` returns."""

    if window < 3:
        return None
    values = _finite_series(closes)
    if values is None or len(values) < window + 1:
        return None
    earlier = values[-(window + 1) : -1]
    later = values[-window:]
    returns = [
        (current / previous - 1) * 100 for previous, current in zip(earlier, later, strict=True)
    ]
    latest = returns[-1]
    reference = returns[:-1]
    mean = sum(reference) / len(reference)
    variance = sum((entry - mean) ** 2 for entry in reference) / len(reference)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return 0.0
    return (latest - mean) / stdev


def _ema_series(values: Sequence[float], *, period: int) -> list[float]:
    """Exponential moving average over ``values``; returns one entry per input."""
    if period < 1 or not values:
        return []
    alpha = 2.0 / (period + 1)
    series: list[float] = []
    ema = values[0]
    for value in values:
        ema = alpha * value + (1 - alpha) * ema
        series.append(ema)
    return series


def _sma_series(values: Sequence[float], *, period: int) -> list[float]:
    """Simple moving average over ``values``; ``None`` padded until window fills."""
    if period < 1 or len(values) < period:
        return []
    series: list[float] = []
    running = sum(values[:period])
    series.append(running / period)
    for index in range(period, len(values)):
        running += values[index] - values[index - period]
        series.append(running / period)
    return series


def kdj(
    closes: Sequence[float],
    *,
    period: int = DEFAULT_KDJ_PERIOD,
    k_smooth: int = DEFAULT_KDJ_K_SMOOTH,
    d_smooth: int = DEFAULT_KDJ_D_SMOOTH,
) -> tuple[float, float, float] | None:
    """Latest (K, D, J) stochastic oscillator values (0..100, J unbounded).

    Uses the classic Chinese-market formulation (RSV over the lookback
    window, then Wilder-style smoothing of K and D).  Returns ``None`` when
    the window is not satisfied or prices are not finite.
    """

    series = kdj_series(closes, period=period, k_smooth=k_smooth, d_smooth=d_smooth)
    return series[-1] if series else None


def kdj_series(
    closes: Sequence[float],
    *,
    period: int = DEFAULT_KDJ_PERIOD,
    k_smooth: int = DEFAULT_KDJ_K_SMOOTH,
    d_smooth: int = DEFAULT_KDJ_D_SMOOTH,
) -> list[tuple[float, float, float]]:
    """Full (K, D, J) series, one aligned triple per satisfied close.

    Returns an empty list when the sample window is not satisfied.  Each
    triple uses only data up to that close (no look-ahead), so the series
    is directly plottable.
    """

    if period < 2 or k_smooth < 1 or d_smooth < 1:
        return []
    values = _finite_series(closes)
    if values is None or len(values) < period + max(k_smooth, d_smooth):
        return []
    rsv: list[float] = []
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        low = min(window)
        high = max(window)
        if high == low:
            rsv.append(50.0)
        else:
            rsv.append((values[index] - low) / (high - low) * 100)
    k_series = _ema_series(rsv, period=k_smooth)
    d_series = _ema_series(k_series, period=d_smooth)
    return [(k, d, 3 * k - 2 * d) for k, d in zip(k_series, d_series, strict=True)]


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int = DEFAULT_ATR_PERIOD,
) -> float | None:
    """Average true range over the last ``period`` bars (Wilder smoothing).

    Returns ``None`` when fewer than ``period + 1`` bars are available or
    any price is non-finite.  ``highs/lows/closes`` must be same length.
    """

    if period < 1:
        return None
    if not (len(highs) == len(lows) == len(closes)):
        return None
    values = _finite_series(closes)
    if values is None or len(values) < period + 1:
        return None
    high_values = [float(value) for value in highs]
    low_values = [float(value) for value in lows]
    if any(not math.isfinite(value) for value in high_values + low_values):
        return None
    true_ranges: list[float] = []
    for index in range(1, len(values)):
        previous_close = values[index - 1]
        high = high_values[index]
        low = low_values[index]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if not true_ranges:
        return None
    average = sum(true_ranges[:period]) / period
    for index in range(period, len(true_ranges)):
        average = (average * (period - 1) + true_ranges[index]) / period
    return average


def vwap(
    closes: Sequence[float],
    *,
    period: int = DEFAULT_VWAP_PERIOD,
    typical: Sequence[float] | None = None,
) -> float | None:
    """Volume-weighted average price of the last ``period`` bars.

    ``typical`` is the per-bar typical price ``(H+L+C)/3``; when omitted the
    close is used as the anchor (pure price VWAP, honest about the missing
    volume dimension).  Volume weights are not available for this market, so
    this is a *price* reference line — never labelled as a true VWAP.
    """

    if period < 1:
        return None
    values = _finite_series(closes)
    if values is None or len(values) < period:
        return None
    anchor = values[-period:]
    if typical is not None:
        typical_values = [float(value) for value in typical]
        if len(typical_values) < len(anchor) or any(
            not math.isfinite(value) for value in typical_values
        ):
            return None
        anchor = typical_values[-len(anchor) :]
    return sum(anchor) / len(anchor)


__all__ = [
    "DEFAULT_ATR_PERIOD",
    "DEFAULT_BOLLINGER_NDEV",
    "DEFAULT_BOLLINGER_PERIOD",
    "DEFAULT_KDJ_D_SMOOTH",
    "DEFAULT_KDJ_K_SMOOTH",
    "DEFAULT_KDJ_PERIOD",
    "DEFAULT_RSI_PERIOD",
    "DEFAULT_VWAP_PERIOD",
    "atr",
    "bollinger_lower",
    "kdj",
    "kdj_series",
    "momentum_pair",
    "percentile_rank",
    "return_z_score",
    "vwap",
    "wilder_rsi",
    "wilder_rsi_series",
]

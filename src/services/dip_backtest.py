"""Explainable walk-forward bottom-fishing scenario signal for the broad index.

The signal is deliberately conservative:

* every historical signal day uses only closes strictly before that day
  (rolling window), so there is no look-ahead bias;
* adjacent signal days are merged into independent episodes (>=20 calendar
  days apart) so one recovery cannot be counted as many samples;
* a verdict is only emitted when the primary 14-day horizon has at least
  ``MIN_EPISODES`` independent episodes and an out-of-sample win rate at or
  above ``MIN_WIN_RATE``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

MIN_TOTAL_CLOSES = 30
TRAILING_WINDOW_DAYS = 60
MIN_WINDOW_DAYS = 30
EPISODE_MIN_GAP_DAYS = 20
MIN_EPISODES = 5
MIN_WIN_RATE = 0.60
BANDS = (0.05, 0.10, 0.15)
PRIMARY_HORIZON_DAYS = 14
HORIZONS = (7, 14, 30)

#: Structural regime breaks (window-B): 2025-07-15 CS2 交易保护 7 天锁上线
#: 与 2025-10-22 汰换合同更新，两次都让大盘单日 -60%。样本跨越这些断点
#: 时，历史情景概率的统计口径跨了结构性事件，输出必须显式注明。
REGIME_BREAKS: tuple[date, ...] = (date(2025, 7, 15), date(2025, 10, 22))
REGIME_NOTE = "⚠️ 样本跨越结构性事件（2025-07 交易保护 / 2025-10 汰换合同），历史情景概率仅供参考。"


def crosses_regime_break(closes: Sequence[tuple[date, float]]) -> bool:
    """True when the closes span at least one known structural break."""

    if not closes:
        return False
    first = closes[0][0]
    last = closes[-1][0]
    return any(first <= break_date <= last for break_date in REGIME_BREAKS)


def segment_stats(
    closes: Sequence[tuple[date, float]],
    signal_indices: Sequence[int],
    *,
    horizon: int = PRIMARY_HORIZON_DAYS,
) -> tuple[dict[str, float | int] | None, dict[str, float | int] | None]:
    """Forward-return stats split around the first regime break.

    Returns ``(pre_break, post_break)`` dicts with ``scenarios``/``win_rate``
    (None when a segment has no valid signals).  Segments let the reader see
    whether a signal's history comes mostly from before or after a
    structural event instead of blending incompatible regimes into one
    number.
    """

    valid = [index for index in signal_indices if index + horizon < len(closes)]
    if not valid:
        return None, None
    break_index = next((i for i, (day, _value) in enumerate(closes) if day in REGIME_BREAKS), None)
    if break_index is None:
        return None, None
    pre = [index for index in valid if index < break_index]
    post = [index for index in valid if index >= break_index]

    def stats(indices: Sequence[int]) -> dict[str, float | int] | None:
        if not indices:
            return None
        returns = [closes[index + horizon][1] / closes[index][1] - 1 for index in indices]
        return {
            "scenarios": len(indices),
            "win_rate": sum(r > 0 for r in returns) / len(returns),
            "median_return": statistics.median(returns),
        }

    return stats(pre), stats(post)


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """One forward-return statistic set for a fixed horizon."""

    horizon_days: int
    scenarios: int
    episodes: int
    win_rate: float | None
    median_return: float | None
    mean_return: float | None
    best_return: float | None
    worst_return: float | None


@dataclass(frozen=True, slots=True)
class DipReport:
    """The full explainable answer for one ``抄底`` request."""

    total_closes: int
    current_close: float | None
    current_percentile: float | None
    drawdown_from_high: float | None
    triggered_band: float | None
    temperature_line: str
    outcomes: tuple[ScenarioOutcome, ...]
    verdict: str
    detail: str
    crosses_regime: bool = False
    segment_pre: dict[str, float | int] | None = None
    segment_post: dict[str, float | int] | None = None


def percentile_value(sorted_values: Sequence[float], ratio: float) -> float:
    """Nearest-rank percentile for a strictly ascending sequence."""

    if not sorted_values or not 0.0 < ratio <= 1.0:
        raise ValueError("percentile requires a non-empty sequence and ratio in (0,1]")
    rank = max(1, min(len(sorted_values), math.ceil(ratio * len(sorted_values))))
    return sorted_values[rank - 1]


def detect_signal_days(
    closes: Sequence[tuple[date, float]],
    *,
    band: float,
    window: int = TRAILING_WINDOW_DAYS,
    min_window: int = MIN_WINDOW_DAYS,
) -> list[int]:
    """Indexes where the close is strictly below the band of prior closes."""

    signals: list[int] = []
    for index in range(min_window, len(closes)):
        start = max(0, index - window)
        prior = [value for _, value in closes[start:index]]
        if len(prior) < min_window:
            continue
        threshold = percentile_value(sorted(prior), band)
        if closes[index][1] < threshold:
            signals.append(index)
    return signals


def count_episodes(
    indices: Sequence[int],
    closes: Sequence[tuple[date, float]],
    *,
    min_gap_days: int = EPISODE_MIN_GAP_DAYS,
) -> int:
    """Count independent clusters of signal days separated by the gap rule."""

    if not indices:
        return 0
    episodes = 1
    previous_date = closes[indices[0]][0]
    for index in indices[1:]:
        current_date = closes[index][0]
        if (current_date - previous_date).days >= min_gap_days:
            episodes += 1
        previous_date = current_date
    return episodes


def scenario_outcomes(
    closes: Sequence[tuple[date, float]],
    signal_indices: Sequence[int],
    *,
    horizons: tuple[int, ...] = HORIZONS,
) -> tuple[ScenarioOutcome, ...]:
    """Forward-return statistics for each horizon over valid signal days."""

    results: list[ScenarioOutcome] = []
    for horizon in horizons:
        valid = [index for index in signal_indices if index + horizon < len(closes)]
        returns = [closes[index + horizon][1] / closes[index][1] - 1 for index in valid]
        results.append(
            ScenarioOutcome(
                horizon_days=horizon,
                scenarios=len(valid),
                episodes=count_episodes(valid, closes),
                win_rate=sum(return_value > 0 for return_value in returns) / len(returns)
                if returns
                else None,
                median_return=statistics.median(returns) if returns else None,
                mean_return=statistics.mean(returns) if returns else None,
                best_return=max(returns) if returns else None,
                worst_return=min(returns) if returns else None,
            )
        )
    return tuple(results)


def _pct(value: float | None) -> str:
    return f"{value * 100:+.1f}%" if value is not None else "—"


def build_dip_report(
    closes: Sequence[tuple[date, float]],
    *,
    temperature_line: str = "盘面温度: 暂不可用",
) -> DipReport:
    """Build the report for the latest close, failing closed at every gate."""

    total = len(closes)
    crosses = crosses_regime_break(closes)
    if total < MIN_TOTAL_CLOSES:
        return DipReport(
            total,
            None,
            None,
            None,
            None,
            temperature_line,
            (),
            "数据不足",
            f"至少需要 {MIN_TOTAL_CLOSES} 个日样本，当前只有 {total} 个。",
            crosses_regime=crosses,
        )

    current_close = closes[-1][1]
    values = [value for _, value in closes]
    current_percentile = sum(value <= current_close for value in values) / len(values)
    drawdown = (current_close / max(values) - 1) * 100

    triggered_band: float | None = None
    outcomes: tuple[ScenarioOutcome, ...] = ()
    signals_for_segment: list[int] = []
    for band in BANDS:
        signals = detect_signal_days(closes, band=band)
        if signals and signals[-1] == total - 1:
            triggered_band = band
            outcomes = scenario_outcomes(closes, signals)
            signals_for_segment = list(signals)
            break

    if triggered_band is None:
        return DipReport(
            total,
            current_close,
            current_percentile,
            drawdown,
            None,
            temperature_line,
            (),
            "观察",
            "当前点位未触发历史低位档位（≤近60日P15），不构成抄底信号。",
            crosses_regime=crosses,
        )

    primary = next(
        (outcome for outcome in outcomes if outcome.horizon_days == PRIMARY_HORIZON_DAYS),
        None,
    )
    segment_pre, segment_post = segment_stats(closes, signals_for_segment)
    if (
        primary is None
        or primary.episodes < MIN_EPISODES
        or primary.win_rate is None
        or primary.win_rate < MIN_WIN_RATE
    ):
        episodes_text = "—" if primary is None else str(primary.episodes)
        win_rate_text = (
            "—" if primary is None or primary.win_rate is None else _pct(primary.win_rate)
        )
        return DipReport(
            total,
            current_close,
            current_percentile,
            drawdown,
            triggered_band,
            temperature_line,
            outcomes,
            "证据不足",
            (
                f"14日独立行情段 {episodes_text}<{MIN_EPISODES} "
                f"或上涨率 {win_rate_text}<{_pct(MIN_WIN_RATE)}，继续积累样本。"
            ),
            crosses_regime=crosses,
            segment_pre=segment_pre,
            segment_post=segment_post,
        )

    verdict = "深跌区" if triggered_band <= 0.05 else "低位区"
    return DipReport(
        total,
        current_close,
        current_percentile,
        drawdown,
        triggered_band,
        temperature_line,
        outcomes,
        verdict,
        f"触发 ≤近60日P{int(triggered_band * 100)}，14日回测已过门槛。",
        crosses_regime=crosses,
        segment_pre=segment_pre,
        segment_post=segment_post,
    )


def format_dip_report(report: DipReport) -> str:
    """Render the report as a compact QQ text card."""

    lines = ["📉 【抄底·大盘历史情景】"]
    if report.current_close is None:
        lines.append(f"⏳ 判定: {report.verdict}｜{report.detail}")
        lines.append(
            f"ℹ️ 当前已积累 {report.total_closes} 个日样本，达到 {MIN_TOTAL_CLOSES} 个后开始评估。"
        )
        lines.append("⚠️ 历史情景概率，不构成交易建议。")
        return "\n".join(lines)

    lines.append(f"当前指数: {report.current_close:.2f}")
    if report.current_percentile is not None:
        lines.append(
            f"历史分位: P{max(1, min(99, round(report.current_percentile * 100)))}"
            f"（近{report.total_closes}日，越低越便宜）"
        )
    if report.drawdown_from_high is not None:
        lines.append(f"距区间高点: {report.drawdown_from_high:+.1f}%")
    lines.append(report.temperature_line)
    lines.append(f"判定: {report.verdict}｜{report.detail}")

    if report.outcomes:
        lines.append("历史情景（同档位信号日的实际后续走势）:")
        for outcome in report.outcomes:
            lines.append(
                f" {outcome.horizon_days}日: {outcome.scenarios}次/{outcome.episodes}段 "
                f"上涨率{_pct(outcome.win_rate)} 中位{_pct(outcome.median_return)} "
                f"最差{_pct(outcome.worst_return)}"
            )
        lines.append("回测口径: 滚动60日窗口，只用当日之前数据；间隔≥20日算独立段。")
        if report.segment_pre is not None or report.segment_post is not None:
            pre_text = (
                f"断点前 {report.segment_pre['scenarios']}次 上涨率"
                f"{_pct(float(report.segment_pre['win_rate']))}"
                if report.segment_pre is not None
                else "断点前 样本不足"
            )
            post_text = (
                f"断点后 {report.segment_post['scenarios']}次 上涨率"
                f"{_pct(float(report.segment_post['win_rate']))}"
                if report.segment_post is not None
                else "断点后 样本不足"
            )
            lines.append(
                f"分断点统计（2025-07 交易保护 / 2025-10 汰换合同）: {pre_text}｜{post_text}"
            )
    if report.crosses_regime:
        lines.append(REGIME_NOTE)
    tendency = _dip_tendency(report)
    if tendency:
        lines.append(f"老师视角: {tendency}")
    lines.append("⚠️ 历史情景概率，不构成交易建议；样本越少，可信度越低。")
    return "\n".join(lines)


def _dip_tendency(report: DipReport) -> str:
    """Deterministic teacher-style tendency; numbers come from the report only."""

    if report.current_close is None:
        return ""
    primary = next(
        (outcome for outcome in report.outcomes if outcome.horizon_days == PRIMARY_HORIZON_DAYS),
        None,
    )
    if report.verdict == "证据不足":
        return (
            f"当前同档位历史样本不足（{primary.episodes if primary else 0} 段独立行情），"
            "上涨率暂不具备统计意义——仅作参考，先观察不急着动手。"
        )
    if (
        report.verdict in ("低位区", "深跌区")
        and primary is not None
        and primary.win_rate is not None
    ):
        win = primary.win_rate * 100
        if win >= 60:
            return (
                f"历史上这个位置 {primary.horizon_days} 日上涨率 {win:.0f}%，"
                "动能偏正面——持有者可考虑继续持有观察，空仓者可参考分批留意企稳信号。"
            )
        if win >= 50:
            return (
                f"历史上这个位置 {primary.horizon_days} 日上涨率 {win:.0f}%，"
                "多空接近——持有者可考虑设好心理价位，空仓者不急，等更强信号。"
            )
        return (
            f"历史上这个位置 {primary.horizon_days} 日上涨率仅 {win:.0f}%，"
            "动能偏弱——持有者可考虑逢反弹减仓，空仓者参考等待更低分位。"
        )
    return "当前未触发历史低位档位，不构成抄底信号——持有者无需动作，空仓者参考观望。"


__all__ = [
    "BANDS",
    "DipReport",
    "EPISODE_MIN_GAP_DAYS",
    "MIN_EPISODES",
    "MIN_TOTAL_CLOSES",
    "MIN_WIN_RATE",
    "PRIMARY_HORIZON_DAYS",
    "REGIME_BREAKS",
    "REGIME_NOTE",
    "ScenarioOutcome",
    "build_dip_report",
    "count_episodes",
    "crosses_regime_break",
    "detect_signal_days",
    "format_dip_report",
    "percentile_value",
    "scenario_outcomes",
    "segment_stats",
]

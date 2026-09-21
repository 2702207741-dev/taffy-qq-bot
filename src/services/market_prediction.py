"""Historical-scenario probability statistics for probabilistic forecasts.

Every probability is a frequency count over same-bucket historical days:
the bucket is (temperature state, 3-day momentum direction). The model
layer may interpret these numbers but may never invent probabilities.

A second, independent rule signal (price percentile + momentum structure)
can be combined with the bucket frequencies; when both methods agree, the
rendered confidence is upgraded and the method count is stated.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from .dip_backtest import REGIME_BREAKS, REGIME_NOTE, crosses_regime_break
from .indicators import momentum_pair, percentile_rank
from .market_temperature import TemperatureUnavailable, calculate_market_temperature

_MIN_CLOSES_FOR_STATS = 30
_HORIZONS = (7, 14, 30)
_CONFIDENCE_HIGH = 20
_CONFIDENCE_MEDIUM = 8
_CONFIDENCE_LOW = 3
_METHOD_BUCKET = "分桶频率"
_METHOD_RULE = "规则信号"
_Z_WILSON_95 = 1.96
_OOS_HORIZON_DAYS = 14
_OOS_MIN_DAYS = 5  # 与抄底独立行情段门槛对齐
_OOS_MIN_SAMPLES = 3  # 与 confident_enough 下限对齐
_MIN_ITEM_SCENARIO_CLOSES = 60


@dataclass(frozen=True, slots=True)
class ScenarioStats:
    horizon: int
    count: int
    up_ratio: float  # 0..1 fraction of same-bucket days that rose
    median_change: float  # median percent change over the horizon
    low_95: float = 0.0  # Wilson 95% interval lower bound of up_ratio
    high_95: float = 1.0  # Wilson 95% interval upper bound of up_ratio

    @property
    def confidence(self) -> str:
        if self.count >= _CONFIDENCE_HIGH:
            return "高"
        if self.count >= _CONFIDENCE_MEDIUM:
            return "中"
        if self.count >= _CONFIDENCE_LOW:
            return "低"
        return "极低"

    @property
    def confident_enough(self) -> bool:
        return self.count >= _CONFIDENCE_LOW


@dataclass(frozen=True, slots=True)
class Bucket:
    temperature_state: str
    momentum_positive: bool


def _bucket_for_date(
    closes: Mapping[date, float],
    target: date,
) -> Bucket | None:
    """Classify one historical day into a (temperature, momentum) bucket."""

    window = {
        target - timedelta(days=offset): closes.get(target - timedelta(days=offset))
        for offset in range(1, 8)
    }
    if any(value is None for value in window.values()) or target not in closes:
        return None
    import calendar

    day_noon = float(calendar.timegm(target.timetuple()) + 12 * 3600)
    result = calculate_market_temperature(
        current_index=closes[target],
        current_timestamp=day_noon,
        daily_closes=closes,
        now=day_noon,
    )
    if isinstance(result, TemperatureUnavailable):
        return None
    three_days_ago = window[target - timedelta(days=3)]
    if three_days_ago is None:
        return None
    three_day_change = (closes[target] / three_days_ago - 1) * 100
    return Bucket(
        temperature_state=_coarse_temperature(result.temperature_state),
        momentum_positive=three_day_change >= 0,
    )


def _coarse_temperature(state: str) -> str:
    """Coarsen the 7-level temperature label into cold/neutral/hot."""

    if state in ("极冷", "明显偏冷", "偏冷"):
        return "冷"
    if state in ("极热", "明显偏热", "偏热"):
        return "热"
    return "中性"


def wilson_interval(
    successes: int, trials: int, *, z: float = _Z_WILSON_95
) -> tuple[float, float] | None:
    """Wilson score 95% interval for a binomial proportion (stdlib only).

    Point estimates from a handful of overlapping forward windows look
    falsely precise; the interval shows the honest uncertainty. ``None``
    only when there is nothing to estimate.
    """

    if trials <= 0 or successes < 0 or successes > trials:
        return None
    prob = successes / trials
    denom = 1 + z * z / trials
    center = (prob + z * z / (2 * trials)) / denom
    half = z * math.sqrt(prob * (1 - prob) / trials + z * z / (4 * trials * trials)) / denom
    low = max(0.0, min(center - half, prob))
    high = min(1.0, max(center + half, prob))
    return (low, high)


def _regime_adjusted_closes(
    closes: Mapping[date, float],
) -> tuple[dict[date, float], bool]:
    """Restrict bucket stats to the post-break segment when spanned.

    Returns ``(effective closes, crossed_break)``.  Mixing pre/post-break
    days blends incompatible regimes (2025-07 trade hold, 2025-10 contract
    change); the post-break segment is the only honest sample.  Callers
    refuse when it falls below the 30-close floor.
    """

    ordered = sorted(closes)
    pairs = [(day, closes[day]) for day in ordered]
    if not crosses_regime_break(pairs):
        return dict(closes), False
    latest_break = max(
        break_day for break_day in REGIME_BREAKS if ordered[0] <= break_day <= ordered[-1]
    )
    return ({day: closes[day] for day in ordered if day > latest_break}, True)


def oos_direction_hits(
    closes: Mapping[date, float],
    *,
    horizon: int = _OOS_HORIZON_DAYS,
    min_history: int = _MIN_CLOSES_FOR_STATS,
    min_samples: int = _OOS_MIN_SAMPLES,
) -> tuple[int, int] | None:
    """Walk-forward directional check over history (no look-ahead).

    For each past day with enough history behind it and a closed forward
    window ahead, classify its bucket using only data available that day,
    predict up when the same-bucket history (also fully closed by then)
    rose more than half the time, and score against the actual forward
    move.  Returns ``(hits, total)`` or ``None`` when nothing qualifies.
    """

    ordered = sorted(closes)
    if len(ordered) < min_history + horizon + 1:
        return None
    hits = 0
    total = 0
    for index in range(min_history, len(ordered) - horizon):
        day = ordered[index]
        bucket = _bucket_for_date({past: closes[past] for past in ordered[: index + 1]}, day)
        if bucket is None:
            continue
        ups = 0
        examined = 0
        for prior in range(min_history, index):
            prior_day = ordered[prior]
            if (
                _bucket_for_date({past: closes[past] for past in ordered[: prior + 1]}, prior_day)
                != bucket
            ):
                continue
            if prior + horizon > index:
                continue  # forward window not closed as of the test day
            examined += 1
            if closes[ordered[prior + horizon]] > closes[prior_day]:
                ups += 1
        if examined < min_samples:
            continue
        predicted_up = ups / examined > 0.5
        actual_up = closes[ordered[index + horizon]] > closes[day]
        total += 1
        if predicted_up == actual_up:
            hits += 1
    return (hits, total) if total else None


def brief_history_lines(closes: Mapping[date, float]) -> list[str] | None:
    """Deterministic history block for 盘面解读 (no LLM, no network).

    Position (percentile/drawdown), scenario tendency and method footnote
    from the same bucket-frequency engine.  ``None`` below the 30-close
    floor — the caller serves the brief without history (fail-closed).
    """

    if len(closes) < _MIN_CLOSES_FOR_STATS:
        return None
    effective, crossed = _regime_adjusted_closes(closes)
    if not effective:
        return None
    current = max(effective)
    if len(effective) < _MIN_CLOSES_FOR_STATS or current not in effective:
        return None
    ordered = sorted(effective)
    values = [float(effective[day]) for day in ordered]
    latest = values[-1]
    peak = max(values)
    drawdown = (1 - latest / peak) * 100 if peak > 0 else 0.0
    percentile = percentile_rank(latest, values)
    position = f"历史分位 {percentile:.0f}%" if percentile is not None else "历史分位 样本不足"
    span = ""
    if len(values) >= 31:
        span = f"｜近30个收盘日 {(latest / values[-31] - 1) * 100:+.1f}%"
    lines = [
        f"📊 历史维度（{len(effective)}天日线）: {position}"
        f"｜距{len(values)}日高点回撤 {drawdown:.1f}%{span}"
    ]
    stats = historical_scenario_probs(effective, current)
    if stats:
        fourteen = next((stat for stat in stats if stat.horizon == 14), None)
        if fourteen is not None:
            lines.append(
                f"14日情景: 上涨率 {fourteen.up_ratio * 100:.0f}%"
                f"（95%区间 {fourteen.low_95 * 100:.0f}-{fourteen.high_95 * 100:.0f}%）"
                f"｜样本{fourteen.count}次"
            )
        agreement = method_agreement(effective, current, stats)
        lines.append(
            f"预测倾向: {agreement[0]}" if agreement else f"预测倾向: {_tendency_text(stats)}"
        )
        best = max(stats, key=lambda stat: stat.count)
        lines.append(f"口径: 分桶频率（同档样本{best.count}次，区间与回检见预测）")
    if crossed:
        lines.append("样本含结构断点，仅用断点后统计")
    return lines


def item_scenario_lines(
    closes: Mapping[date, float],
    *,
    min_closes: int = _MIN_ITEM_SCENARIO_CLOSES,
) -> list[str] | None:
    """Compact per-item history-scenario block for 买卖参考.

    Same bucket-frequency engine as the broad forecast, but bucketed on
    the item's own closes (its own momentum state).  ``None`` below
    ``min_closes`` daily closes or without same-bucket history — the
    caller omits the section silently (fail-closed).
    """

    if len(closes) < min_closes or len(closes) < _MIN_CLOSES_FOR_STATS:
        return None
    effective, crossed = _regime_adjusted_closes(closes)
    if not effective:
        return None
    current = max(effective)
    if len(effective) < _MIN_CLOSES_FOR_STATS or current not in effective:
        return None
    stats = historical_scenario_probs(effective, current)
    if not stats:
        return None
    lines = [
        f"未来{stat.horizon}日 上涨率 {stat.up_ratio * 100:.0f}%"
        f"（95%区间 {stat.low_95 * 100:.0f}-{stat.high_95 * 100:.0f}%）｜样本{stat.count}次"
        for stat in stats
    ]
    agreement = method_agreement(effective, current, stats)
    lines.append(f"倾向: {agreement[0]}" if agreement else f"倾向: {_tendency_text(stats)}")
    oos = oos_direction_hits(effective)
    if oos is not None and oos[1] >= _OOS_MIN_DAYS:
        lines.append(f"样本外回检: 命中 {oos[0]}/{oos[1]}")
    if crossed:
        lines.append("样本含结构断点，仅用断点后统计")
    return lines


def historical_scenario_probs(
    closes: Mapping[date, float],
    current_date: date,
    *,
    horizons: tuple[int, ...] = _HORIZONS,
) -> list[ScenarioStats] | None:
    """Return same-bucket forward statistics for each horizon, or None when thin."""

    if len(closes) < _MIN_CLOSES_FOR_STATS or current_date not in closes:
        return None
    current_bucket = _bucket_for_date(closes, current_date)
    if current_bucket is None:
        return None
    dates = sorted(closes)
    position = {day: index for index, day in enumerate(dates)}
    buckets: dict[Bucket, dict[int, list[float]]] = {}
    for day in dates:
        if day >= current_date:
            break
        bucket = _bucket_for_date(closes, day)
        if bucket is None or bucket != current_bucket:
            continue
        base_index = position[day]
        for horizon in horizons:
            # "N days" means N complete closed samples later (same as the dip
            # backtest), never a calendar offset that could land on a missing
            # or not-yet-closed day.
            forward_index = base_index + horizon
            if forward_index >= len(dates):
                continue
            change = (closes[dates[forward_index]] / closes[day] - 1) * 100
            buckets.setdefault(bucket, {}).setdefault(horizon, []).append(change)
    sample_lists = buckets.get(current_bucket)
    if not sample_lists:
        return None
    results: list[ScenarioStats] = []
    for horizon in horizons:
        changes = sample_lists.get(horizon)
        if not changes:
            continue
        up_count = sum(1 for change in changes if change > 0)
        interval = wilson_interval(up_count, len(changes))
        results.append(
            ScenarioStats(
                horizon=horizon,
                count=len(changes),
                up_ratio=up_count / len(changes),
                median_change=float(statistics.median(changes)),
                low_95=interval[0] if interval else 0.0,
                high_95=interval[1] if interval else 1.0,
            )
        )
    return results


def rule_signal(closes: Mapping[date, float], current_date: date) -> str | None:
    """Second, independent signal: price percentile + momentum structure.

    Returns ``"偏暖"`` / ``"偏冷"`` / ``"中性"``, or ``None`` when the
    sample is too thin.  The rule is deliberately simple and fully
    deterministic: percentile ≥70 with positive recent momentum is bullish,
    percentile ≤30 with negative momentum is bearish, anything else neutral.
    """

    if current_date not in closes or len(closes) < _MIN_CLOSES_FOR_STATS:
        return None
    values = [float(value) for value in closes.values()]
    current = float(closes[current_date])
    pct = percentile_rank(current, values)
    if pct is None:
        return None
    ordered_dates = sorted(closes)
    recent = [float(closes[day]) for day in ordered_dates[-15:]]
    pair = momentum_pair(recent, window=7)
    if pair is None:
        return None
    recent_momentum, _prior = pair
    if pct >= 70 and recent_momentum > 0:
        return "偏暖"
    if pct <= 30 and recent_momentum < 0:
        return "偏冷"
    return "中性"


def method_agreement(
    closes: Mapping[date, float],
    current_date: date,
    stats: list[ScenarioStats] | None,
) -> tuple[str, int] | None:
    """Combine the bucket-frequency stats with the rule signal.

    Returns ``(label, method_count)`` when both methods exist and the
    bucket-frequency tendency is confident enough to state; ``None`` when
    either method is unavailable.  Agreement upgrades the confidence label;
    disagreement keeps the bucket-frequency label but states the split.
    """

    if not stats:
        return None
    rule = rule_signal(closes, current_date)
    if rule is None:
        return None
    bucket_tendency = _tendency_text(stats)
    if "偏暖" in bucket_tendency:
        bucket_dir = "偏暖"
    elif "偏冷" in bucket_tendency:
        bucket_dir = "偏冷"
    else:
        bucket_dir = "中性"
    if rule == bucket_dir:
        return (f"{bucket_dir}（两种方法同向：{_METHOD_BUCKET}+{_METHOD_RULE}）", 2)
    if bucket_dir == "中性":
        return (f"中性（{_METHOD_BUCKET}拉锯，{_METHOD_RULE}亦中性）", 2)
    return (f"{bucket_dir}（{_METHOD_BUCKET}与{_METHOD_RULE}方向不一致，倾向减弱）", 2)


def render_scenario_forecast(
    closes: Mapping[date, float],
    current_date: date,
    *,
    temperature_line: str = "",
) -> str:
    """Render the probabilistic forecast card; numbers come from statistics only."""

    if len(closes) < _MIN_CLOSES_FOR_STATS or current_date not in closes:
        return (
            "🔮 【短期走势预测｜历史情景】\n──────────\n"
            "⏳ 历史日线不足 30 天，暂无法形成统计样本。\n"
            "本次不输出伪概率。"
        )
    effective, crossed_break = _regime_adjusted_closes(closes)
    if len(effective) < _MIN_CLOSES_FOR_STATS or current_date not in effective:
        return (
            "🔮 【短期走势预测｜历史情景】\n──────────\n"
            "⏳ 样本跨越结构性事件断点，断点后有效日线不足 30 天。\n"
            "本次不输出伪概率。"
        )
    stats = historical_scenario_probs(effective, current_date)
    if not stats:
        return (
            "🔮 【短期走势预测｜历史情景】\n──────────\n"
            "⏳ 当前档位（温度+动量）缺乏历史同档样本，暂无法统计。\n"
            "本次不输出伪概率。"
        )
    lines = ["🔮 【短期走势预测｜历史情景】", "──────────"]
    if temperature_line:
        lines.append(temperature_line)
    lines.append("同档位历史样本:（温度+3日动量方向一致的日子）")
    for stat in stats:
        lines.append(
            f"  未来{stat.horizon}个已收盘日 上涨率 {stat.up_ratio * 100:.0f}%"
            f"（95%区间 {stat.low_95 * 100:.0f}-{stat.high_95 * 100:.0f}%）｜"
            f"中位 {stat.median_change:+.1f}%｜样本 {stat.count} 次（置信度 {stat.confidence}）"
        )
    tendency = _tendency_text(stats)
    agreement = method_agreement(effective, current_date, stats)
    if agreement is not None:
        label, method_count = agreement
        lines.append(f"倾向: {label}")
        lines.append(f"方法数: {method_count}（历史情景频率 + 规则信号），同向才上调置信度。")
    else:
        lines.append(f"倾向: {tendency}")
        lines.append("方法数: 1（历史情景频率；规则信号样本不足未计入）。")
    oos = oos_direction_hits(effective)
    if oos is not None and oos[1] >= _OOS_MIN_DAYS:
        hits, total = oos
        lines.append(
            f"样本外回检（{_OOS_HORIZON_DAYS}日方向）: 命中 {hits}/{total}"
            f"（{hits / total * 100:.0f}%）"
        )
    if crossed_break:
        lines.append(REGIME_NOTE)
    lines.append("注：远期窗口相互重叠，样本非完全独立；独立行情段计数见抄底。")
    lines.append("⚠️ 历史情景概率，非确定性预测；样本越少可信度越低。")
    return "\n".join(lines)


def _tendency_text(stats: list[ScenarioStats]) -> str:
    if not stats:
        return "数据不足，暂不判断"
    thirty_day = next((stat for stat in stats if stat.horizon == 30), None)
    seven_day = next((stat for stat in stats if stat.horizon == 7), None)
    reliable = [stat for stat in stats if stat.confident_enough]
    if not reliable:
        strongest = max(stats, key=lambda stat: stat.count)
        direction = "偏暖" if strongest.up_ratio > 0.5 else "偏冷"
        return f"历史样本极少（{strongest.count} 次），仅作参考：短期{strongest.up_ratio * 100:.0f}% 上涨率，{direction}"
    if (
        thirty_day is not None
        and thirty_day.up_ratio >= 0.55
        and thirty_day.count >= _CONFIDENCE_MEDIUM
    ):
        return "中期偏暖（30日上涨率 ≥55%，样本充足）"
    if (
        thirty_day is not None
        and thirty_day.up_ratio <= 0.45
        and thirty_day.count >= _CONFIDENCE_MEDIUM
    ):
        return "中期偏冷（30日上涨率 ≤45%，样本充足）"
    if seven_day is not None and seven_day.up_ratio >= 0.6:
        return "短期偏暖（7日上涨率 ≥60%）"
    if seven_day is not None and seven_day.up_ratio <= 0.4:
        return "短期偏冷（7日上涨率 ≤40%）"
    return "多空拉锯（各期限上涨率均接近 50%）"


__all__ = [
    "Bucket",
    "ScenarioStats",
    "brief_history_lines",
    "historical_scenario_probs",
    "item_scenario_lines",
    "method_agreement",
    "oos_direction_hits",
    "render_scenario_forecast",
    "rule_signal",
    "wilson_interval",
]

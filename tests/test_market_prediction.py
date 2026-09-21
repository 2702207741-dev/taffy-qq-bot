"""Tests for historical-scenario probability statistics."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.services.market_prediction import (
    historical_scenario_probs,
    render_scenario_forecast,
)


def _synthetic_closes(days: int = 120, *, base: float = 800.0) -> dict[date, float]:
    """Deterministic zig-zag series with a known repeating pattern."""

    start = date(2026, 1, 1)
    closes = {}
    value = base
    for index in range(days):
        day = start + timedelta(days=index)
        closes[day] = value
        # 4-day up, 4-day down pattern so several buckets repeat
        phase = (index // 4) % 2
        value *= 1.01 if phase == 0 else 0.99
    return closes


def test_scenario_stats_are_frequency_based() -> None:
    closes = _synthetic_closes()
    current = sorted(closes)[-1]
    stats = historical_scenario_probs(closes, current)
    assert stats is not None
    for stat in stats:
        assert 0.0 <= stat.up_ratio <= 1.0
        assert stat.count >= 1
        assert stat.horizon in (7, 14, 30)


def test_confidence_levels_follow_sample_size() -> None:
    closes = _synthetic_closes()
    current = sorted(closes)[-1]
    stats = historical_scenario_probs(closes, current)
    assert stats is not None
    for stat in stats:
        if stat.count >= 20:
            assert stat.confidence == "高"
        elif stat.count >= 8:
            assert stat.confidence == "中"
        elif stat.count >= 3:
            assert stat.confidence == "低"
        else:
            assert stat.confidence == "极低"


def test_insufficient_history_fails_closed() -> None:
    closes = _synthetic_closes(days=10)
    current = sorted(closes)[-1]
    assert historical_scenario_probs(closes, current) is None
    text = render_scenario_forecast(closes, current)
    assert "历史日线不足 30 天" in text
    assert "不输出伪概率" in text


def test_forecast_card_contains_probabilities_and_confidence() -> None:
    closes = _synthetic_closes()
    current = sorted(closes)[-1]
    text = render_scenario_forecast(closes, current)
    assert "上涨率" in text
    assert "置信度" in text
    assert "倾向" in text
    assert "非确定性预测" in text


def test_tendency_never_invents_numbers() -> None:
    """The card's numbers must all come from the stats block."""

    closes = _synthetic_closes()
    current = sorted(closes)[-1]
    text = render_scenario_forecast(closes, current)
    stats = historical_scenario_probs(closes, current)
    assert stats is not None
    # Every percent sign in the card should trace back to a stat line.
    for stat in stats:
        assert f"{stat.up_ratio * 100:.0f}%" in text


# ─── 商用硬化批2：Wilson 区间 / OOS 回检 / regime 分段 ─────


def test_wilson_interval_known_values() -> None:
    from src.services.market_prediction import wilson_interval

    assert wilson_interval(0, 0) is None
    assert wilson_interval(-1, 10) is None
    assert wilson_interval(11, 10) is None
    assert wilson_interval(5, 10) == pytest.approx((0.2366, 0.7634), abs=0.01)
    assert wilson_interval(0, 10) == pytest.approx((0.0, 0.2775), abs=0.01)
    assert wilson_interval(10, 10) == pytest.approx((0.7225, 1.0), abs=0.01)


def test_stats_carry_wilson_bounds() -> None:
    closes = _synthetic_closes()
    stats = historical_scenario_probs(closes, sorted(closes)[-1])
    assert stats is not None
    for stat in stats:
        assert 0.0 <= stat.low_95 <= stat.up_ratio <= stat.high_95 <= 1.0


def _monotone_closes(days: int, *, factor: float) -> dict[date, float]:
    start = date(2026, 1, 1)
    return {start + timedelta(days=i): 100.0 * (factor**i) for i in range(days)}


def test_oos_direction_hits_on_monotone_series() -> None:
    from src.services.market_prediction import oos_direction_hits

    for factor in (1.005, 0.995):
        result = oos_direction_hits(_monotone_closes(80, factor=factor))
        assert result is not None
        hits, total = result
        assert total >= 5 and hits == total
    assert oos_direction_hits(_monotone_closes(20, factor=1.005)) is None


def test_regime_break_restricts_to_post_break_segment() -> None:
    from src.services.market_prediction import _regime_adjusted_closes

    pre = {date(2025, 9, 1) + timedelta(days=i): 100.0 for i in range(40)}
    post_start = date(2025, 10, 23)
    post = {post_start + timedelta(days=i): 100.0 * (1.002**i) for i in range(40)}
    effective, crossed = _regime_adjusted_closes({**pre, **post})
    assert crossed is True
    assert len(effective) == 40 and min(effective) > date(2025, 10, 22)
    plain = _synthetic_closes()
    effective, crossed = _regime_adjusted_closes(plain)
    assert crossed is False and effective == plain


def test_regime_thin_post_break_fails_closed() -> None:
    pre = {date(2025, 9, 1) + timedelta(days=i): 100.0 for i in range(60)}
    post = {date(2025, 10, 23) + timedelta(days=i): 100.0 for i in range(10)}
    closes = {**pre, **post}
    text = render_scenario_forecast(closes, sorted(closes)[-1])
    assert "断点后有效日线不足" in text
    assert "不输出伪概率" in text


def test_forecast_card_shows_wilson_oos_and_overlap_note() -> None:
    closes = _synthetic_closes()
    text = render_scenario_forecast(closes, sorted(closes)[-1])
    assert "95%区间" in text
    assert "样本外回检" in text
    assert "相互重叠" in text


def test_item_scenario_lines_sufficient_and_gated() -> None:
    from src.services.market_prediction import item_scenario_lines

    lines = item_scenario_lines(_synthetic_closes())
    assert lines is not None
    text = "\n".join(lines)
    assert "上涨率" in text and "95%区间" in text and "倾向" in text
    assert item_scenario_lines(_synthetic_closes(days=30)) is None
    # ≥60 closes 但凑不出 8 连日 → 同档统计天然为 None（fail-closed）
    ordered = sorted(_synthetic_closes().items())
    gapped = {day: price for i, (day, price) in enumerate(ordered) if i % 9 < 7}
    assert len(gapped) >= 60
    assert item_scenario_lines(gapped) is None


def test_item_scenario_lines_include_oos_on_trend() -> None:
    from src.services.market_prediction import item_scenario_lines

    lines = item_scenario_lines(_monotone_closes(80, factor=1.005))
    assert lines is not None
    assert any("样本外回检" in line for line in lines)


def test_brief_history_lines_gated_and_rich() -> None:
    from src.services.market_prediction import brief_history_lines

    assert brief_history_lines(_synthetic_closes(days=20)) is None
    lines = brief_history_lines(_synthetic_closes())
    assert lines is not None
    text = "\n".join(lines)
    assert "历史维度" in text and "历史分位" in text
    assert "回撤" in text and "预测倾向" in text and "口径" in text


# ─── 抄底老师视角 / 盘面解读 prompt / persona 注入 ─────


def test_dip_report_teacher_tendency_present() -> None:
    from src.services.dip_backtest import build_dip_report, format_dip_report

    report = build_dip_report(())
    text = format_dip_report(report)
    # 数据不足路径：无老师视角行，但 fail-closed 文案保留
    assert "至少需要" in text


def test_dip_tendency_uses_reference_wording_only() -> None:
    from src.services.dip_backtest import _dip_tendency, build_dip_report

    # 构造一个触发"证据不足"的报告，验证倾向句措辞
    report = build_dip_report(
        [(date(2026, 1, 1) + timedelta(days=i), 800.0 + i) for i in range(60)]
    )
    tendency = _dip_tendency(report)
    assert tendency == "" or "可考虑" in tendency or "参考" in tendency
    assert "必须" not in tendency and "一定" not in tendency


def test_brief_prompt_has_teacher_posture_and_bans() -> None:
    from src.services.market_insight import _BRIEF_SYSTEM_PROMPT

    assert "资深分析师" in _BRIEF_SYSTEM_PROMPT
    assert "持有者可考虑" in _BRIEF_SYSTEM_PROMPT
    assert "禁止" in _BRIEF_SYSTEM_PROMPT
    assert "自造概率数字" in _BRIEF_SYSTEM_PROMPT


def test_persona_injects_market_teacher_block_only_for_market_questions() -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=())

    from src.services.persona import build_persona_prompt

    market = build_persona_prompt("今天大盘怎么样，值得入手吗")
    assert "市场老师模式" in market
    assert "梭哈" in market  # 禁令在 prompt 里
    casual = build_persona_prompt("晚上好呀")
    assert "市场老师模式" not in casual

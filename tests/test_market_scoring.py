"""V0.4 P2: deterministic buy/sell reference scoring (no LLM anywhere)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.services import market_scoring as scoring_module
from src.services.indicators import (
    bollinger_lower,
    momentum_pair,
    percentile_rank,
    return_z_score,
    wilder_rsi,
)
from src.services.market_history import MarketHistoryStore
from src.services.market_scoring import (
    MIN_DAILY_SAMPLES,
    compute_trade_score,
    render_trade_score,
)

NOW = 1_800_000_000  # fixed wall clock for the history store


def _falling_closes(days: int = 40) -> list[float]:
    """Steady decline from 200 to ~140: deep drawdown, low RSI."""

    return [200.0 - index * 1.5 for index in range(days)]


def _rising_closes(days: int = 40) -> list[float]:
    return [140.0 + index * 1.5 for index in range(days)]


def _flat_closes(days: int = 40) -> list[float]:
    return [100.0] * days


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def test_wilder_rsi_known_values() -> None:
    assert wilder_rsi(_flat_closes()) == 50.0
    falling = wilder_rsi(_falling_closes())
    rising = wilder_rsi(_rising_closes())
    assert falling is not None and falling < 25
    assert rising is not None and rising > 75
    assert wilder_rsi([100.0, 101.0, 102.0]) is None  # sample window not met
    assert wilder_rsi([100.0, float("nan")] * 10) is None  # NaN never scored


def test_bollinger_lower_known_band() -> None:
    closes = [float(100 + (index % 5)) for index in range(30)]
    lower = bollinger_lower(closes)
    assert lower is not None
    assert 90 < lower < 100
    assert bollinger_lower(closes[:10]) is None  # below the 20 period


def test_percentile_rank_bounds() -> None:
    series = [1.0, 2.0, 3.0, 4.0]
    assert percentile_rank(3.0, series) == 75.0
    assert percentile_rank(0.5, series) == 0.0
    assert percentile_rank(99.0, series) == 100.0
    assert percentile_rank(float("nan"), series) is None


def test_momentum_pair_requires_two_windows() -> None:
    assert momentum_pair(_rising_closes()) is not None
    assert momentum_pair(_rising_closes()[:14]) is None


def test_return_z_score_flat_returns_zero() -> None:
    closes = []
    price = 100.0
    for _day in range(20):
        price *= 1.01  # perfectly constant returns
        closes.append(price)
    z = return_z_score(closes)
    assert z is not None and abs(z) < 1e-9


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------


def test_compute_trade_score_requires_sample_gate() -> None:
    assert (
        compute_trade_score(
            [100.0] * 10, sample_days=10, sell_count=1, bid_count=1, spread_ratio=None
        )
        is None
    )
    score = compute_trade_score(
        _flat_closes(MIN_DAILY_SAMPLES),
        sample_days=MIN_DAILY_SAMPLES,
        sell_count=100,
        bid_count=5,
        spread_ratio=None,
    )
    assert score is not None
    for factor in (*score.buy.factors, *score.sell.factors):
        assert math.isfinite(factor.score)
        assert 0 <= factor.score <= 100


def test_dip_scenario_scores_buy_above_sell() -> None:
    score = compute_trade_score(
        _falling_closes(), sample_days=40, sell_count=100, bid_count=10, spread_ratio=None
    )
    assert score is not None
    assert score.buy.total > score.sell.total
    oversold = next(factor for factor in score.buy.factors if factor.name == "超卖")
    assert oversold.score == 100.0
    drawdown = next(factor for factor in score.buy.factors if factor.name == "回撤位")
    assert "回撤" in drawdown.basis and drawdown.score >= 90


def test_peak_scenario_scores_sell_above_buy() -> None:
    score = compute_trade_score(
        _rising_closes(), sample_days=40, sell_count=100, bid_count=10, spread_ratio=None
    )
    assert score is not None
    assert score.sell.total > score.buy.total
    percentile = next(factor for factor in score.sell.factors if factor.name == "价格分位")
    assert percentile.score == 100.0


def test_missing_spread_redistributes_weight_with_note() -> None:
    score = compute_trade_score(
        _flat_closes(), sample_days=40, sell_count=100, bid_count=10, spread_ratio=None
    )
    assert score is not None
    assert score.buy.note is not None and "CSQAQ" in score.buy.note
    assert all(factor.name != "跨平台价差" for factor in score.buy.factors)
    total_weight = sum(factor.weight for factor in score.buy.factors)
    assert abs(total_weight - 1.0) < 1e-9


def test_present_spread_scores_full_when_ratio_meets_threshold() -> None:
    score = compute_trade_score(
        _flat_closes(), sample_days=40, sell_count=100, bid_count=10, spread_ratio=0.99
    )
    assert score is not None
    assert score.buy.note is None
    spread = next(factor for factor in score.buy.factors if factor.name == "跨平台价差")
    assert spread.score == 100.0 and abs(spread.weight - 0.20) < 1e-9
    vwap_factor = next(factor for factor in score.buy.factors if factor.name == "VWAP位置")
    assert abs(vwap_factor.weight - 0.10) < 1e-9
    # Flat closes sit at the VWAP anchor -> full score.
    assert vwap_factor.score == 100.0


def test_vwap_factor_honest_about_price_anchor() -> None:
    """VWAP here is a pure-price anchor — no fake volume weighting."""
    score = compute_trade_score(
        _falling_closes(), sample_days=40, sell_count=100, bid_count=10, spread_ratio=None
    )
    assert score is not None
    vwap_factor = next(factor for factor in score.buy.factors if factor.name == "VWAP位置")
    assert "价格锚" in vwap_factor.basis or "样本不足" in vwap_factor.basis


def test_missing_counts_redistribute_sell_liquidity() -> None:
    score = compute_trade_score(
        _flat_closes(), sample_days=40, sell_count=None, bid_count=None, spread_ratio=None
    )
    assert score is not None
    assert score.sell.note is not None and "流动性" in score.sell.note
    assert all(factor.name != "流动性" for factor in score.sell.factors)


def test_render_contains_weights_basis_and_disclaimer() -> None:
    score = compute_trade_score(
        _falling_closes(), sample_days=40, sell_count=100, bid_count=10, spread_ratio=0.95
    )
    assert score is not None
    text = render_trade_score("AK-47 | Redline (Field-Tested)", score)
    assert "AK-47 | Redline (Field-Tested) 买卖参考" in text
    assert "📈 买入参考" in text and "📉 卖出参考" in text
    assert "权重" in text and "样本：40 个日线" in text
    assert "不构成交易建议" in text


# ---------------------------------------------------------------------------
# History store integration
# ---------------------------------------------------------------------------


def _catalog(count: int) -> Any:
    return [f"Item {index}" for index in range(count)]


async def _seed_daily_history(
    store: MarketHistoryStore, *, days: int = 30, item: str = "AK-47 | Redline (Field-Tested)"
) -> None:
    await store.initialize()
    latest = NOW // 3600 * 3600

    def seed() -> None:
        connection = store._connect()
        try:
            rows = []
            for bucket_index in range(days * 24):
                bucket = latest - bucket_index * 3600
                price = 200.0 - bucket_index * 0.1
                rows.append((bucket, item, price, price - 1, 100, 10, bucket))
            connection.executemany(
                """
                INSERT INTO quote_snapshots (
                    bucket_at, market_name, sell_price, bid_price,
                    sell_count, bid_count, data_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()
        finally:
            connection.close()

    await __import__("asyncio").to_thread(seed)


@pytest.mark.asyncio
async def test_item_daily_closes_resamples_to_market_days(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "history.db",
        catalog=_catalog(1),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    await _seed_daily_history(store, days=30)

    result = await store.item_daily_closes("AK-47 | Redline (Field-Tested)", days=90)
    assert result is not None
    closes, sell_count, bid_count = result
    assert 28 <= len(closes) <= 31, "hourly buckets must collapse to one close per day"
    assert all(price > 0 for _day, price in closes)
    assert closes == sorted(closes, key=lambda entry: entry[0])
    assert sell_count == 100 and bid_count == 10
    assert await store.item_daily_closes("Unknown Item") is None


@pytest.mark.asyncio
async def test_build_reply_and_summary_with_real_store(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "history.db",
        catalog=_catalog(1),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    await _seed_daily_history(store, days=40)

    with (
        patch.object(scoring_module, "market_history", store),
        patch.object(scoring_module.csqaq_service, "configured", staticmethod(lambda: False)),
    ):
        reply = await scoring_module.build_trade_score_reply("AK-47 | Redline (Field-Tested)")
        assert "买卖参考" in reply and "样本：" in reply
        assert "CSQAQ，价差项权重已归还" in reply
        assert "历史情景" not in reply  # 40 日线未达单品情景 60 日线门槛

        summary = await scoring_module.score_summary_line("AK-47 | Redline (Field-Tested)")
        assert summary is not None
        assert summary.startswith("买卖参考：买入 ")
        assert summary.endswith("（发送 买卖参考 查看明细）")

        # An item outside the pool must stay silent for 查询 and honest for 命令.
        assert await scoring_module.score_summary_line("Unknown Item") is None
        outside = await scoring_module.build_trade_score_reply("Unknown Item")
        assert "暂不评分" in outside

        # Too few daily samples refuses to score at all.
        await _seed_daily_history(store, days=1, item="Short Item")
        short = await scoring_module.build_trade_score_reply("Short Item", user_query="x")
        assert "样本不足" in short
        assert await scoring_module.score_summary_line("Short Item", user_query="x") is None


@pytest.mark.asyncio
async def test_build_reply_appends_item_scenario_above_60_closes(tmp_path: Path) -> None:
    store = MarketHistoryStore(
        path=tmp_path / "history.db",
        catalog=_catalog(1),  # type: ignore[arg-type]
        wall_clock=lambda: NOW,
    )
    await _seed_daily_history(store, days=70)

    with (
        patch.object(scoring_module, "market_history", store),
        patch.object(scoring_module.csqaq_service, "configured", staticmethod(lambda: False)),
    ):
        reply = await scoring_module.build_trade_score_reply("AK-47 | Redline (Field-Tested)")
        assert "历史情景（自身动量同档）" in reply
        assert "上涨率" in reply and "95%区间" in reply and "倾向" in reply


# ---------------------------------------------------------------------------
# 窗口B：挂刀时机第 9 因子
# ---------------------------------------------------------------------------


def test_hoard_ratio_present_scores_and_keeps_weight() -> None:
    score = compute_trade_score(
        _flat_closes(),
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        hoard_ratio=0.85,
    )
    assert score is not None
    hoard = next(factor for factor in score.sell.factors if factor.name == "挂刀时机")
    assert hoard.score == 85.0
    assert "Steam 挂刀比例" in hoard.basis
    # 权重归还：CSQAQ 三项缺失会把权重按比例摊回留存项（含挂刀时机），
    # 但总权重恒为 1
    total_weight = sum(factor.weight for factor in score.sell.factors)
    assert abs(total_weight - 1.0) < 1e-9
    assert hoard.weight > 0.04


def test_hoard_ratio_present_with_csqaq_full_keeps_04() -> None:
    score = compute_trade_score(
        _flat_closes(),
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=0.95,
        rent_annual=5.0,
        platform_sell_counts=(100, 50, 30, 20, 10, 5),
        hoard_ratio=0.85,
    )
    assert score is not None
    hoard = next(factor for factor in score.sell.factors if factor.name == "挂刀时机")
    assert abs(hoard.weight - 0.04) < 1e-9
    assert score.sell.note is None


def test_hoard_ratio_missing_redistributes_with_note() -> None:
    score = compute_trade_score(
        _flat_closes(),
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        hoard_ratio=None,
    )
    assert score is not None
    assert all(factor.name != "挂刀时机" for factor in score.sell.factors)
    assert score.sell.note is not None and "挂刀" in score.sell.note
    total_weight = sum(factor.weight for factor in score.sell.factors)
    assert abs(total_weight - 1.0) < 1e-9


def test_hoard_ratio_low_scores_low() -> None:
    score = compute_trade_score(
        _flat_closes(),
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        hoard_ratio=0.5,
    )
    assert score is not None
    hoard = next(factor for factor in score.sell.factors if factor.name == "挂刀时机")
    assert hoard.score == 50.0

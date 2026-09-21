"""Tests for CSQAQ snapshot accumulation (independent store, QQ trend line)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.chat import compare as compare_module
from src.services.csqaq_history import CsqaqHistoryStore


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_CSQAQ_HISTORY_ENABLED", "true")


@pytest.fixture()
def store(tmp_path: Path) -> CsqaqHistoryStore:
    return CsqaqHistoryStore(path=tmp_path / "csqaq.db")


async def test_record_and_trend_roundtrip(store: CsqaqHistoryStore, enabled: None) -> None:
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.95, day="2026-09-01")
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.98, day="2026-09-03")
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.97, day="2026-09-02")
    series = await store.spread_series("Item A", days=3650)
    assert series == [("2026-09-01", 0.95), ("2026-09-02", 0.97), ("2026-09-03", 0.98)]
    assert await store.render_trend_line("Item A", days=3650) == (
        "📉 价差趋势：0.950→0.980（近 3 次快照）"
    )


async def test_disabled_is_noop(store: CsqaqHistoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_CSQAQ_HISTORY_ENABLED", "false")
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.95) is False
    assert await store.spread_series("Item A") == []
    assert await store.render_trend_line("Item A") is None


async def test_same_day_upsert_trend_gate_and_rejects(
    store: CsqaqHistoryStore, enabled: None
) -> None:
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.90, day="2026-09-01")
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.95, day="2026-09-01")
    assert await store.spread_series("Item A", days=3650) == [("2026-09-01", 0.95)]
    assert await store.render_trend_line("Item A", days=3650) is None
    assert await store.record_snapshot(0, "Item A", spread_ratio=0.9) is False
    assert await store.record_snapshot(11, "  ", spread_ratio=0.9) is False
    assert await store.record_snapshot(11, "Item A") is False


async def test_prune_drops_old_rows(store: CsqaqHistoryStore, enabled: None) -> None:
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.5, day="2020-01-01")
    assert await store.record_snapshot(11, "Item A", spread_ratio=0.9, day="2026-09-01")
    series = await store.spread_series("Item A", days=3650)
    assert [day for day, _ in series] == ["2026-09-01"]


async def test_scoring_inputs_record_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    import src.services.market_scoring as scoring

    store = CsqaqHistoryStore(path=tmp_path / "csqaq.db")
    monkeypatch.setattr(scoring, "csqaq_history_store", store)
    detail = SimpleNamespace(
        good_id=77,
        quotes=[
            SimpleNamespace(buy_price=95.0, sell_num=3),
            SimpleNamespace(buy_price=None, sell_num=5),
        ],
        rent=None,
    )

    async def fake_detail(query: str, *, market_hash_name: str, max_candidates: int = 3):
        return {"success": True, "detail": detail}

    monkeypatch.setattr(scoring.csqaq_service, "verified_detail", fake_detail)
    inputs = await scoring._csqaq_inputs("q", "Item A", 100.0)
    assert inputs.spread_ratio == pytest.approx(0.95)
    series = await store.spread_series("Item A", days=3650, basis="score")
    assert len(series) == 1 and series[0][1] == pytest.approx(0.95)


def _fake_quotes() -> list[SimpleNamespace]:
    return [SimpleNamespace(sell_price=100.0)]


async def test_compare_record_and_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """Full record_and_trend_line path with a FIXED market clock.

    The trend window is ``today - _TREND_DAYS`` in the UTC+8 market
    timezone.  The test pins ``csqaq_history.datetime.now`` (the single
    module both ``_today()`` and ``spread_series`` read) to a fixed
    instant, so the recorded day, the window cutoff and the assertions can
    never drift out of sync with the real wall clock — a hard-coded
    absolute date would silently expire once the calendar moves past it.
    In-window records are included and an out-of-window record is excluded.
    """
    import datetime as real_datetime

    from src.services import csqaq_history as csqaq_module
    from src.services.csqaq import CsqaqGoodDetail, CsqaqPlatformQuote

    fixed_now = real_datetime.datetime(
        2026, 9, 2, 12, 0, 0, tzinfo=real_datetime.timezone(real_datetime.timedelta(hours=8))
    )

    class FixedMarketClock(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(csqaq_module, "datetime", FixedMarketClock)

    store = CsqaqHistoryStore(path=tmp_path / "csqaq.db")
    monkeypatch.setattr(compare_module, "csqaq_history_store", store)
    monkeypatch.setattr(
        compare_module,
        "validate_quote_response",
        lambda response, now=None: _fake_quotes(),
    )
    detail = CsqaqGoodDetail(
        good_id=7310,
        market_hash_name="Item A",
        name_cn="物品A",
        supply_count=None,
        data_age_seconds=None,
        quotes=(CsqaqPlatformQuote("buff", 8400.0, 445, 95.0, 29),),
        rent=None,
        price_change_7d=None,
        price_change_30d=None,
    )
    # Market clock is fixed at 2026-09-02: the first call records TODAY
    # (2026-09-02) only -> a single point, no trend line yet.
    first = await compare_module.record_and_trend_line("Item A", {}, {"detail": detail})
    assert first is None  # 单点不成趋势，但快照已落盘
    # Yesterday's snapshot (2026-09-01) sits INSIDE the 7-day window.
    await store.record_snapshot(7310, "Item A", spread_ratio=0.90, day="2026-09-01")
    second = await compare_module.record_and_trend_line("Item A", {}, {"detail": detail})
    assert second is not None and second.startswith("📉 价差趋势：")
    assert await compare_module.record_and_trend_line("Item A", {}, {}) == second
    assert await compare_module.record_and_trend_line("Item B", {}, {}) is None
    # Window check: the two in-window days are both present...
    series = await store.spread_series("Item A")
    assert [day for day, _ in series] == ["2026-09-01", "2026-09-02"]
    # ...and a record older than the cutoff (2026-08-25 < 2026-09-02 - 7d)
    # is excluded from the trend window.
    await store.record_snapshot(7310, "Item A", spread_ratio=0.50, day="2026-08-25")
    series = await store.spread_series("Item A")
    assert [day for day, _ in series] == ["2026-09-01", "2026-09-02"]
    assert await store.render_trend_line("Item A") == second


def test_format_appends_trend_line() -> None:
    import time

    from src.plugins.chat.compare import format_price_comparison

    now = time.time()
    steamdt_ok = {
        "success": True,
        "data": [
            {
                "platform": "BUFF",
                "sellPrice": 100.0,
                "sellCount": 5,
                "biddingPrice": 99.0,
                "biddingCount": 1,
                "updateTime": int(now * 1000),
            }
        ],
    }
    csqaq_miss = {"success": False, "errorMsg": "x"}
    without = format_price_comparison("Item A", steamdt_ok, csqaq_miss, now=now)
    assert "价差趋势" not in without
    with_trend = format_price_comparison(
        "Item A",
        steamdt_ok,
        csqaq_miss,
        now=now,
        trend_line="📉 价差趋势：0.900→0.950（近 2 次快照）",
    )
    assert "📉 价差趋势：0.900→0.950（近 2 次快照）" in with_trend

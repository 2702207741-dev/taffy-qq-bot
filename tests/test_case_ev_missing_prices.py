"""Case EV: partially missing wear prices keep the missing wear in the
denominator (missing wear contributes zero; it is not dropped from the
average).  Regression batch 3.

All prices come from a synthetic ``_price_one`` stub (the only patched
boundary); the crate, pool and catalog are synthetic.  Expected numbers
are independent constants: milspec tier odds are 0.7992, so a single
item with two equal-weight wears prices at ``avg * 0.7992``.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.services import case_ev as case_ev_module
from src.services.case_ev import (
    CaseEvResult,
    case_ev_payload,
    compute_case_ev,
    render_case_ev,
)
from src.services.case_meta import CrateInfo, CrateItem

CASE_PRICE = 60.0
MILSPEC_ODDS = 0.7992  # one item in the milspec tier bucket
HALF_MILSPEC_ODDS = 0.3996  # two items share the tier odds


def _crate() -> CrateInfo:
    return CrateInfo(
        crate_id="crate-test",
        name_en="Test Case",
        name_zh="测试箱",
        market_hash_name="Test Case",
        crate_type="Case",
        first_sale_date="2026-01-01",
        rare_count=0,
        unknown_count=0,
        fetched_at=int(time.time()),
    )


def _item(name: str = "Test Skin") -> CrateItem:
    return CrateItem(name, None, "milspec")


def _catalog(wears: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"Test Skin ({wear})" for wear in wears)


def _two_item_catalog() -> tuple[str, ...]:
    return (
        "Skin A (Factory New)",
        "Skin A (Field-Tested)",
        "Skin B (Factory New)",
        "Skin B (Field-Tested)",
    )


async def _compute(
    prices: dict[str, float | None],
    *,
    catalog: tuple[str, ...] | None = None,
    items: tuple[CrateItem, ...] | None = None,
) -> CaseEvResult:
    """Run the real compute with a price stub keyed by market name.

    The crate itself always prices at CASE_PRICE unless overridden.
    """

    async def fake_price(name: str) -> dict | None:
        if name == "Test Case":
            return {"min_sell": CASE_PRICE}
        if name in prices:
            value = prices[name]
            return {"min_sell": value} if value is not None else None
        return None

    pool = items if items is not None else (_item(),)
    cat = catalog if catalog is not None else _catalog(("Factory New", "Field-Tested"))
    with patch.object(case_ev_module, "_price_one", side_effect=fake_price):
        return await compute_case_ev(_crate(), pool, cat)


def _full_wear(wear: str) -> str:
    return f"Test Skin ({wear})"


@pytest.mark.asyncio
async def test_two_wears_priced_average() -> None:
    """100 + 20 over two wears -> avg 60, EV 47.952."""
    result = await _compute({_full_wear("Factory New"): 100.0, _full_wear("Field-Tested"): 20.0})
    item = result.items[0]
    assert item.avg_price == pytest.approx(60.0)
    assert result.ev == pytest.approx(47.952)
    assert item.contribution == pytest.approx(47.952)


@pytest.mark.asyncio
async def test_partial_missing_wear_keeps_missing_in_denominator() -> None:
    """100 + missing over two wears -> avg 50, EV 39.96 (was 79.92 before
    the fix, because the missing wear was dropped from the average)."""
    result = await _compute({_full_wear("Factory New"): 100.0, _full_wear("Field-Tested"): None})
    item = result.items[0]
    assert item.avg_price == pytest.approx(50.0)
    assert result.ev == pytest.approx(39.96)
    # Counters and the original None price are preserved.
    assert result.missing_prices == 1
    assert result.unpriced_items == 0
    assert result.unavailable_prices == 0
    fn = next(q for q in item.wear_quotes if q.wear == "Factory New")
    ft = next(q for q in item.wear_quotes if q.wear == "Field-Tested")
    assert fn.price == 100.0
    assert ft.price is None, "missing quote must stay None, not zero"


@pytest.mark.asyncio
async def test_all_wears_missing_keeps_none_and_zero() -> None:
    """All wears missing -> avg None, EV 0, unpriced."""
    result = await _compute({_full_wear("Factory New"): None, _full_wear("Field-Tested"): None})
    item = result.items[0]
    assert item.avg_price is None
    assert result.ev == 0.0
    assert item.contribution == 0.0
    assert result.missing_prices == 2
    assert result.unpriced_items == 1
    assert result.unavailable_prices == 1


@pytest.mark.asyncio
async def test_single_catalog_wear_priced() -> None:
    """Catalog holds exactly one wear priced at 100 -> avg 100, EV 79.92."""
    result = await _compute({_full_wear("Factory New"): 100.0}, catalog=_catalog(("Factory New",)))
    item = result.items[0]
    assert item.avg_price == pytest.approx(100.0)
    assert result.ev == pytest.approx(79.92)


@pytest.mark.asyncio
async def test_no_catalog_variants_unpriced() -> None:
    """No wear variants in catalog -> avg None, EV 0."""
    result = await _compute({}, catalog=())
    item = result.items[0]
    assert item.avg_price is None
    assert result.ev == 0.0
    assert result.missing_prices == 0
    assert result.unpriced_items == 1
    assert result.unavailable_prices == 0


@pytest.mark.asyncio
async def test_two_items_in_tier_share_odds_and_missing_stays_in_denominator() -> None:
    """A: 100 + missing -> avg 50; B: 40 + 20 -> avg 30; each odds 0.3996.

    A contribution 0.3996*50 = 19.98, B contribution 0.3996*30 = 11.988,
    EV 31.968.
    """
    catalog = (
        "Skin A (Factory New)",
        "Skin A (Field-Tested)",
        "Skin B (Factory New)",
        "Skin B (Field-Tested)",
    )
    prices = {
        "Skin A (Factory New)": 100.0,
        "Skin A (Field-Tested)": None,
        "Skin B (Factory New)": 40.0,
        "Skin B (Field-Tested)": 20.0,
    }

    async def fake_price(name: str) -> dict | None:
        if name == "Test Case":
            return {"min_sell": CASE_PRICE}
        value = prices.get(name)
        return {"min_sell": value} if value is not None else None

    items = (_item("Skin A"), _item("Skin B"))
    with patch.object(case_ev_module, "_price_one", side_effect=fake_price):
        result = await compute_case_ev(_crate(), items, catalog)

    by_name = {item.item_name: item for item in result.items}
    a, b = by_name["Skin A"], by_name["Skin B"]
    assert a.avg_price == pytest.approx(50.0)
    assert b.avg_price == pytest.approx(30.0)
    assert a.odds == pytest.approx(HALF_MILSPEC_ODDS)
    assert b.odds == pytest.approx(HALF_MILSPEC_ODDS)
    assert a.contribution == pytest.approx(19.98)
    assert b.contribution == pytest.approx(11.988)
    assert result.ev == pytest.approx(31.968)


@pytest.mark.asyncio
async def test_missing_second_price_lowers_ev() -> None:
    """Same catalog, second wear 20 -> missing: EV drops 47.952 -> 39.96."""
    priced = await _compute({_full_wear("Factory New"): 100.0, _full_wear("Field-Tested"): 20.0})
    missing = await _compute({_full_wear("Factory New"): 100.0, _full_wear("Field-Tested"): None})
    assert priced.ev == pytest.approx(47.952)
    assert missing.ev == pytest.approx(39.96)
    assert missing.ev < priced.ev


@pytest.mark.asyncio
async def test_partial_missing_payload_and_text_consistent() -> None:
    """Payload numbers, per-wear None, and rendered text all show 39.96 /
    66.6% with the missing-wear note."""
    result = await _compute({_full_wear("Factory New"): 100.0, _full_wear("Field-Tested"): None})
    payload = case_ev_payload(result)
    assert payload["ev_lower_bound"] == pytest.approx(39.96)
    assert payload["return_rate_percent"] == pytest.approx(66.6)
    item_payload = payload["items"][0]
    assert item_payload["avg_price"] == pytest.approx(50.0)
    assert item_payload["wear_prices"]["Factory New"] == pytest.approx(100.0)
    assert item_payload["wear_prices"]["Field-Tested"] is None

    text = render_case_ev(result)
    assert "39.96" in text
    assert "66.6%" in text
    assert "缺价" in text or "缺失" in text or "无价" in text

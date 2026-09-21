from __future__ import annotations

import pytest

from src.services.market_validation import (
    MarketDataError,
    freshness_error,
    mapping_timestamp,
    parse_timestamp,
    preferred_quote,
    response_data_timestamp,
    validate_broad_market_response,
    validate_quote_response,
)


def test_timestamp_parser_accepts_seconds_milliseconds_and_iso() -> None:
    assert parse_timestamp("1700000000") == 1_700_000_000
    assert parse_timestamp(1_700_000_000_000) == 1_700_000_000
    assert parse_timestamp("2023-11-14T22:13:20Z") == 1_700_000_000
    assert parse_timestamp(True) is None
    assert parse_timestamp(float("nan")) is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp(0) is None


def test_response_timestamp_uses_oldest_nested_valid_time() -> None:
    response = {
        "updateTime": 1_700_000_100,
        "data": [
            {"timestamp": 1_700_000_050},
            {"updatedAt": "bad"},
            "not-a-row",
        ],
    }
    assert mapping_timestamp(response) == 1_700_000_100
    assert response_data_timestamp(response) == 1_700_000_050
    assert response_data_timestamp({"data": {"timestamp": 1_700_000_000}}) == 1_700_000_000
    assert response_data_timestamp({"data": "bad"}) is None


def test_freshness_errors_cover_missing_future_stale_and_current() -> None:
    now = 1_700_000_000
    assert "缺少" in (freshness_error(None, now=now, max_age=60, subject="数据") or "")
    assert "异常" in (freshness_error(now + 301, now=now, max_age=60, subject="数据") or "")
    assert "过期" in (freshness_error(now - 61, now=now, max_age=60, subject="数据") or "")
    assert freshness_error(now, now=now, max_age=60, subject="数据") is None


def test_quote_validation_keeps_only_fresh_positive_rows() -> None:
    now = 1_700_000_100
    quotes = validate_quote_response(
        {
            "success": True,
            "updateTime": now - 30,
            "data": [
                {"platform": "BUFF", "sellPrice": 100, "biddingPrice": 220},
                {"platform": "C5", "sellPrice": 0},
                {
                    "platform": "OLD",
                    "sellPrice": 80,
                    "updateTime": now - 86_400,
                },
            ],
        },
        now=now,
    )

    assert [(quote.platform, quote.sell_price) for quote in quotes] == [("BUFF", 100)]
    assert quotes[0].bid_price is None


def test_quote_validation_sanitizes_counts_and_keeps_valid_bid() -> None:
    now = 1_700_000_100
    quotes = validate_quote_response(
        {
            "success": True,
            "updateTime": now,
            "data": [
                {
                    "sellPrice": 100,
                    "biddingPrice": 99,
                    "sellCount": True,
                    "biddingCount": 3,
                },
                {
                    "platform": "C5",
                    "sellPrice": 101,
                    "sellCount": 0,
                    "biddingCount": -1,
                },
            ],
        },
        now=now,
    )
    assert quotes[0].platform == "未知"
    assert quotes[0].sell_count is None
    assert quotes[0].bid_price == 99 and quotes[0].bid_count == 3
    assert quotes[1].sell_count == 0 and quotes[1].bid_count is None


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"success": False}, "quote_request_failed"),
        ({"success": True, "data": []}, "quote_rows_missing"),
        (
            {"success": True, "updateTime": 1_700_000_100, "data": ["bad"]},
            "quote_values_invalid",
        ),
        (
            {
                "success": True,
                "updateTime": 1_700_000_100,
                "data": [{"sellPrice": float("inf")}],
            },
            "quote_values_invalid",
        ),
    ],
)
def test_quote_validation_rejects_request_shape_and_invalid_values(
    response: dict,
    code: str,
) -> None:
    with pytest.raises(MarketDataError) as raised:
        validate_quote_response(response, now=1_700_000_100)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"success": True, "data": [{"platform": "BUFF", "sellPrice": 100}]}, "quote_stale"),
        (
            {
                "success": True,
                "updateTime": 1_700_000_100 - 601,
                "data": [{"platform": "BUFF", "sellPrice": 100}],
            },
            "quote_stale",
        ),
        (
            {
                "success": True,
                "updateTime": 1_700_000_100 + 301,
                "data": [{"platform": "BUFF", "sellPrice": 100}],
            },
            "quote_stale",
        ),
    ],
)
def test_quote_validation_rejects_missing_stale_or_future_time(response: dict, code: str) -> None:
    with pytest.raises(MarketDataError) as raised:
        validate_quote_response(response, now=1_700_000_100)
    assert raised.value.code == code


def test_preferred_quote_never_uses_an_unvalidated_row() -> None:
    quotes = validate_quote_response(
        {
            "success": True,
            "updateTime": 1_700_000_000,
            "data": [
                {"platform": "BUFF", "sellPrice": 101},
                {"platform": "YOUPIN", "sellPrice": 99},
            ],
        },
        now=1_700_000_100,
    )
    assert preferred_quote(quotes).platform == "YOUPIN"
    buff_only = tuple(quote for quote in quotes if quote.platform == "BUFF")
    assert preferred_quote(buff_only).platform == "BUFF"
    other = validate_quote_response(
        {
            "success": True,
            "timestamp": 1_700_000_000,
            "data": [
                {"platform": "A", "sellPrice": 20},
                {"platform": "B", "sellPrice": 10},
            ],
        },
        now=1_700_000_000,
    )
    assert preferred_quote(other).platform == "B"
    with pytest.raises(MarketDataError):
        preferred_quote(())


def test_broad_market_requires_fresh_payload_timestamp() -> None:
    now = 1_700_000_100
    snapshot = validate_broad_market_response(
        {
            "success": True,
            "data": {
                "broadMarketIndex": 917.2,
                "diffYesterdayRatio": -0.57,
                "diffYesterday": -5.26,
                "updateTime": now - 30,
            },
        },
        now=now,
    )
    assert snapshot.index == 917.2
    assert snapshot.change_percent == -0.57
    assert snapshot.timestamp == now - 30

    with pytest.raises(MarketDataError) as raised:
        validate_broad_market_response(
            {
                "success": True,
                "data": {
                    "broadMarketIndex": 917.2,
                    "diffYesterdayRatio": -0.57,
                },
            },
            now=now,
        )
    assert raised.value.code == "broad_stale"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"success": False}, "broad_request_failed"),
        ({"success": True, "data": []}, "broad_request_failed"),
        (
            {
                "success": True,
                "data": {
                    "updateTime": 1_700_000_100,
                    "broadMarketIndex": True,
                    "diffYesterdayRatio": 0,
                },
            },
            "broad_values_invalid",
        ),
        (
            {
                "success": True,
                "data": {
                    "updateTime": 1_700_000_100,
                    "broadMarketIndex": 900,
                    "diffYesterdayRatio": float("nan"),
                },
            },
            "broad_values_invalid",
        ),
    ],
)
def test_broad_market_rejects_request_shape_and_non_finite_fields(
    response: dict,
    code: str,
) -> None:
    with pytest.raises(MarketDataError) as raised:
        validate_broad_market_response(response, now=1_700_000_100)
    assert raised.value.code == code

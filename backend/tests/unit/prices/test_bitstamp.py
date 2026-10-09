"""Bitstamp daily OHLC into daily prices (PLAN §6, ADR 0039; THREAT_MODEL T-303, T-304)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from typing import Any

import pytest

from coinacct.prices import DailyPrice, PriceError
from coinacct.prices.bitstamp import typical_by_day

JAN1 = 1_704_067_200  # 2024-01-01T00:00:00Z
DAY = 86_400
LATER = date(2100, 1, 1)


# --- daily OHLC


def candle(t: int = JAN1, **values: str) -> dict[str, Any]:
    base = {"open": "42000", "high": "43000", "low": "41000", "close": "42500", "volume": "123.4"}
    return {"timestamp": str(t), **base, **values}


def page(*candles: dict[str, Any], pair: str = "BTC/USD") -> str:
    return json.dumps({"data": {"pair": pair, "ohlc": list(candles)}})


def test_each_daily_candle_gets_its_typical_price() -> None:
    second = candle(JAN1 + DAY, open="1", high="3", low="1", close="2")
    got = typical_by_day(page(candle(), second), "USD", LATER)
    # (43000 + 41000 + 42500) / 3 = 42166.666… → 42166.67; (3 + 1 + 2) / 3 = 2
    assert got == [
        DailyPrice(date(2024, 1, 1), "USD", Decimal("42166.67"), "typical", "bitstamp:ohlc:btcusd"),
        DailyPrice(date(2024, 1, 2), "USD", Decimal("2.00"), "typical", "bitstamp:ohlc:btcusd"),
    ]


def test_other_fiat_pairs_are_parsed_for_display() -> None:
    (p,) = typical_by_day(page(candle(), pair="BTC/EUR"), "EUR", LATER)
    assert (p.currency, p.source) == ("EUR", "bitstamp:ohlc:btceur")


def test_a_candle_without_volume_and_partial_days_get_no_price() -> None:
    text = page(candle(volume="0"), candle(JAN1 + DAY), candle(JAN1 + 2 * DAY))
    assert [p.day for p in typical_by_day(text, "USD", date(2024, 1, 3))] == [date(2024, 1, 2)]


@pytest.mark.parametrize(
    ("text", "currency", "message"),
    [
        (page(candle()), "JPY", "unsupported currency"),
        ("{", "USD", "not valid JSON"),
        ('{"data": {"pair": "BTC/USD", "ohlc": [], "pair": "BTC/EUR"}}', "USD", "not valid JSON"),
        ('{"data": {"pair": "BTC/USD", "ohlc": [{"timestamp": 1}]}}', "USD", "not valid JSON"),
        ('{"data": {"pair": "BTC/USD", "ohlc": [{"open": 1.5}]}}', "USD", "not valid JSON"),
        ('{"data": {"pair": "BTC/USD", "ohlc": [{"open": NaN}]}}', "USD", "not valid JSON"),
        ("[]", "USD", "expected data for the pair BTC/USD"),
        ('{"data": []}', "USD", "expected data for the pair"),
        (page(candle(), pair="BTC/EUR"), "USD", "expected data for the pair BTC/USD"),
        ('{"data": {"pair": "BTC/USD", "ohlc": {}}}', "USD", "expected a list of candles"),
        (page({"timestamp": str(JAN1)}), "USD", "candle 1: expected timestamp, open"),
        (page([]), "USD", "candle 1: expected timestamp, open"),  # type: ignore[arg-type]
        (page({**candle(), "extra": "1"}), "USD", "candle 1: expected timestamp"),
        (page({**candle(), "open": None}), "USD", "candle 1: expected string values"),
        (page(candle(JAN1 + 3600)), "USD", "not a daily candle"),
        (page(candle(low="42100")), "USD", "needs 0 < low <= open, close <= high"),
        (page(candle(high="42400")), "USD", "needs 0 < low"),
        (page(candle(low="0", open="0", close="0")), "USD", "needs 0 < low"),
        (page(candle(open="4.2e4")), "USD", "not a plain decimal"),
        (page(candle(volume="-1")), "USD", "not a plain decimal"),
        (page(candle(), candle()), "USD", "candle 2: out of day order"),
        (page(candle(JAN1 + DAY), candle()), "USD", "candle 2: out of day order"),
        (
            page(candle(volume="0"), candle()),
            "USD",
            "candle 2: out of day order",
        ),  # unpriced, then the same day
        (page(candle(JAN1 + DAY, volume="0"), candle()), "USD", "candle 2: out of day order"),
        (
            page(candle(), candle(JAN1 + 2 * DAY, volume="0"), candle(JAN1 + DAY)),
            "USD",
            "candle 3: out of day",
        ),
    ],
)
def test_malformed_ohlc_is_refused(text: str, currency: str, message: str) -> None:
    with pytest.raises(PriceError, match=message):
        typical_by_day(text, currency, LATER)


def test_a_typical_price_that_rounds_below_a_cent_is_refused() -> None:
    with pytest.raises(PriceError, match="a positive whole number of cents"):
        typical_by_day(page(candle(open="0.001", high="0.001", low="0.001", close="0.001")), "USD", LATER)


def test_the_callers_decimal_context_changes_nothing() -> None:
    with localcontext(Context(prec=3, traps=[Inexact, Rounded])):
        (p,) = typical_by_day(page(candle()), "USD", LATER)
    assert p.price == Decimal("42166.67")
    assert str(p.price) == "42166.67"


def test_a_price_that_rounds_past_the_largest_is_refused_naming_its_candle() -> None:
    top = "999999999999999.995"  # 15 digits, which round half to even past MAX_PRICE
    with pytest.raises(PriceError, match="candle 1: the price is out of range"):
        typical_by_day(page(candle(open=top, high=top, low=top, close=top)), "USD", LATER)
    near = "999999999999999.994"  # rounds down to MAX_PRICE: accepted
    (p,) = typical_by_day(page(candle(open=near, high=near, low=near, close=near)), "USD", LATER)
    assert p.price == Decimal("999999999999999.99")


def test_deeply_nested_json_is_refused_as_invalid() -> None:
    with pytest.raises(PriceError, match="not valid JSON"):
        typical_by_day("[" * 100_000 + "]" * 100_000, "USD", LATER)


@pytest.mark.parametrize("t", ["-1", "1.5", "abc", "1230940799", "253402300800"])  # before 2009, past 9999
def test_a_candle_whose_time_isnt_a_unix_time_from_2009_on_is_refused(t: str) -> None:
    with pytest.raises(PriceError, match=r"candle 1: '.*' is not a unix time from 2009 on"):
        typical_by_day(page({**candle(), "timestamp": t}), "USD", LATER)

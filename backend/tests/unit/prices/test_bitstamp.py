"""Bitstamp trade dumps and daily OHLC into daily prices (PLAN §6, ADR 0007; THREAT_MODEL T-303, T-304)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from coinacct.prices import DailyPrice, PriceError
from coinacct.prices.bitstamp import DUMP_SOURCE, typical_by_day, vwap_by_day

JAN1 = 1_704_067_200  # 2024-01-01T00:00:00Z
DAY = 86_400
LATER = date(2100, 1, 1)


def usd(day: date, price: str) -> DailyPrice:
    return DailyPrice(day, "USD", Decimal(price), "vwap", DUMP_SOURCE)


# --- the trade dump


def test_each_day_gets_its_volume_weighted_average() -> None:
    lines = [
        f"{JAN1},40000.000000000000,0.500000000000\n",
        f"{JAN1 + 60},42000,1.5\n",
        f"{JAN1 + DAY - 1},1,0\r\n",
        f"{JAN1 + DAY},45000.5,2\n",
    ]
    # 2024-01-01: (40000 * 0.5 + 42000 * 1.5) / 2 = 41500; the zero-amount trade adds nothing
    assert vwap_by_day(lines, LATER) == [usd(date(2024, 1, 1), "41500.00"), usd(date(2024, 1, 2), "45000.50")]


@pytest.mark.parametrize(
    ("price", "cents"), [("1.005", "1.00"), ("1.015", "1.02"), ("1.0049", "1.00"), ("1.0051", "1.01")]
)
def test_the_average_is_rounded_once_half_to_even(price: str, cents: str) -> None:
    assert vwap_by_day([f"{JAN1},{price},1"], LATER)[0].price == Decimal(cents)


def test_a_day_on_or_after_the_download_day_is_partial_and_dropped() -> None:
    lines = [f"{JAN1},10,1", f"{JAN1 + DAY},20,1"]
    assert vwap_by_day(lines, date(2024, 1, 2)) == [usd(date(2024, 1, 1), "10.00")]
    assert vwap_by_day(lines, date(2024, 1, 1)) == []


def test_a_day_without_volume_gets_no_price() -> None:
    assert vwap_by_day([f"{JAN1},10,0", f"{JAN1 + DAY},20,1"], LATER) == [usd(date(2024, 1, 2), "20.00")]
    assert vwap_by_day([], LATER) == []


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([f"{JAN1},10"], "line 1: expected time,price,amount"),
        ([f"{JAN1},10,1,2"], "line 1: expected time,price,amount"),
        ([""], "line 1: expected time,price,amount"),
        ([f"{JAN1 + 1},10,1", f"{JAN1},10,1"], "line 2: out of time order"),
        ([f"{JAN1},0,1"], "line 1: a price must be positive"),
        ([f"{JAN1},0.000,1"], "line 1: a price must be positive"),
        ([f"{JAN1},-1,1"], "not a plain decimal"),
        ([f"{JAN1},1e5,1"], "not a plain decimal"),
        ([f"{JAN1},10,.5"], "not a plain decimal"),
        ([f"{JAN1},10,1."], "not a plain decimal"),
        ([f"{JAN1}, 10,1"], "not a plain decimal"),
        ([f"{JAN1},\u0661\u0660,1"], "not a plain decimal"),  # Arabic-Indic digits: int() would take them
        ([f"{JAN1},10,0.0000000000001"], "not a plain decimal"),  # 13 decimals
        ([f"{JAN1},1000000000000000,1"], "not a plain decimal"),  # 16 digits
        (["1230940799,10,1"], "not a unix time from 2009 on"),
        (["253402300800,10,1"], "not a unix time from 2009 on"),
        (["-1,10,1"], "not a unix time"),
        ([f"{JAN1}.5,10,1"], "not a unix time"),
    ],
)
def test_a_malformed_dump_is_refused_with_its_line(lines: list[str], message: str) -> None:
    with pytest.raises(PriceError, match=message):
        vwap_by_day(lines, LATER)


def test_a_bad_line_after_good_days_still_refuses_the_whole_dump() -> None:
    with pytest.raises(PriceError, match="line 3"):
        vwap_by_day([f"{JAN1},10,1", f"{JAN1 + DAY},20,1", "junk"], LATER)


_value = st.decimals(min_value=Decimal("0.000000000001"), max_value=Decimal("999999999999999"), places=12)
_amount = st.decimals(min_value=Decimal(0), max_value=Decimal("1000"), places=12)


@given(st.lists(st.tuples(st.integers(0, DAY - 1), _value, _amount), min_size=1, max_size=30))
def test_the_average_is_exact_and_ignores_order_within_a_day(
    trades: list[tuple[int, Decimal, Decimal]],
) -> None:
    volume = sum((Fraction(a) for _, _, a in trades), Fraction(0))
    trades.sort(key=lambda t: t[0])
    got = vwap_by_day([f"{JAN1 + s},{p:f},{a:f}" for s, p, a in trades], LATER)
    if volume == 0:
        assert got == []
        return
    exact = sum((Fraction(p) * Fraction(a) for _, p, a in trades), Fraction(0)) / volume
    assert got == [usd(date(2024, 1, 1), str(Decimal(round(exact * 100)) / 100))]  # round(): half to even


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
    ],
)
def test_malformed_ohlc_is_refused(text: str, currency: str, message: str) -> None:
    with pytest.raises(PriceError, match=message):
        typical_by_day(text, currency, LATER)


def test_deeply_nested_json_is_refused_as_invalid() -> None:
    with pytest.raises(PriceError, match="not valid JSON"):
        typical_by_day("[" * 100_000 + "]" * 100_000, "USD", LATER)

"""Display prices from USD and the ECB's reference rates (PLAN §6, ADR 0007; THREAT_MODEL T-301, T-304)."""

from __future__ import annotations

from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from coinacct.prices import DailyPrice, PriceError
from coinacct.prices.fx import DISPLAY, ecb_rates, to_display

HEADER = "Date,USD,JPY,GBP,CYP,\n"  # the real file's shape: a trailing comma, retired currencies


def usd(day: date, price: str, source: str = "bitstamp") -> DailyPrice:
    return DailyPrice(day, "USD", Decimal(price), "vwap", source)


def shown(day: date, currency: str, price: str, source: str = "bitstamp") -> DailyPrice:
    return DailyPrice(day, currency, Decimal(price), "fx", f"ecb:eurofxref-hist*{source}")


def test_the_ecb_file_gives_usd_and_display_rates_per_day() -> None:
    lines = [HEADER, "2024-01-03,1.0919,155.45,0.86518,N/A,\n", "2024-01-02,1.0956,155.69,N/A,N/A,\r\n"]
    assert ecb_rates(lines) == {
        date(2024, 1, 3): {"USD": 1_091_900, "GBP": 865_180},
        date(2024, 1, 2): {"USD": 1_095_600},
    }


def test_each_usd_price_is_shown_in_every_display_currency() -> None:
    rates = {date(2024, 1, 2): {"USD": 1_100_000, "GBP": 880_000}}
    got = to_display([usd(date(2024, 1, 2), "44000.00")], rates)
    # EUR: 44000 / 1.1 = 40000; GBP: 40000 * 0.88 = 35200
    assert got == [shown(date(2024, 1, 2), "EUR", "40000.00"), shown(date(2024, 1, 2), "GBP", "35200.00")]
    assert DISPLAY == ("EUR", "GBP")


def test_conversion_is_rounded_once_half_to_even() -> None:
    rates = {date(2024, 1, 2): {"USD": 3_000_000, "GBP": 1_000_000}}
    # 100.10 / 3 = 33.3666… → 33.37
    assert to_display([usd(date(2024, 1, 2), "100.10")], rates)[0].price == Decimal("33.37")
    rates2 = {date(2024, 1, 2): {"USD": 2_000_000, "GBP": 1_000_000}}
    # ties at a USD rate of 2: 1.01 / 2 = 0.505 → 0.50 (even); 1.03 / 2 = 0.515 → 0.52 (even)
    assert [p.price for p in to_display([usd(date(2024, 1, 2), "1.01")], rates2)] == [Decimal("0.50")] * 2
    assert [p.price for p in to_display([usd(date(2024, 1, 2), "1.03")], rates2)] == [Decimal("0.52")] * 2


def test_a_weekend_uses_the_latest_earlier_rate_within_a_week() -> None:
    rates = {date(2024, 1, 5): {"USD": 1_000_000, "GBP": 500_000}}
    prices = [usd(date(2024, 1, d), "10.00") for d in (4, 5, 6, 12, 13)]
    got = to_display(prices, rates)
    assert [(p.currency, p.day.day) for p in got] == [
        ("EUR", 5),
        ("EUR", 6),
        ("EUR", 12),
        ("GBP", 5),
        ("GBP", 6),
        ("GBP", 12),
    ]
    # the 4th is before any rate; the 13th is eight days after the last one: gaps, not stale values


def test_a_day_missing_one_currency_falls_back_for_that_currency_only() -> None:
    rates = {date(2024, 1, 2): {"USD": 1_000_000, "GBP": 500_000}, date(2024, 1, 3): {"USD": 2_000_000}}
    got = to_display([usd(date(2024, 1, 3), "10.00")], rates)
    # EUR on the 3rd: 10 / 2 = 5; GBP falls back to the 2nd: 10 / 1 * 0.5 = 5
    assert got == [shown(date(2024, 1, 3), "EUR", "5.00"), shown(date(2024, 1, 3), "GBP", "5.00")]


def test_a_display_price_that_rounds_below_a_cent_is_a_gap_like_a_missing_rate() -> None:
    rates = {date(2024, 1, 2): {"USD": 3_000_000, "GBP": 9_000_000}}
    # EUR: 0.01 / 3 → 0.00, no price; GBP: 0.01 / 3 * 9 = 0.03
    assert to_display([usd(date(2024, 1, 2), "0.01")], rates) == [shown(date(2024, 1, 2), "GBP", "0.03")]


def test_only_usd_market_prices_are_converted() -> None:
    eur = DailyPrice(date(2024, 1, 2), "EUR", Decimal("1.00"), "typical", "bitstamp")
    with pytest.raises(PriceError, match="expected a USD market price"):
        to_display([eur], {})


@pytest.mark.parametrize("days", [(2, 2), (3, 2)])
def test_a_usd_series_out_of_order_or_with_a_day_twice_is_refused(days: tuple[int, int]) -> None:
    with pytest.raises(PriceError, match="isn't sorted by day, or prices a day twice"):
        to_display([usd(date(2024, 1, d), "1.00") for d in days], {})


@pytest.mark.parametrize("bad", [0, -1, 1.1, True, Decimal(1)])
def test_a_rate_from_elsewhere_must_be_a_positive_scaled_integer(bad: object) -> None:
    for row in ({"USD": bad, "GBP": 1}, {"USD": 1, "GBP": bad}):
        with pytest.raises(PriceError, match="a rate must be a positive integer"):
            to_display([usd(date(2024, 1, 2), "1.00")], {date(2024, 1, 2): row})  # type: ignore[dict-item]


def test_the_callers_decimal_context_changes_nothing() -> None:
    rates = {date(2024, 1, 2): {"USD": 3_000_000, "GBP": 1_000_000}}
    with localcontext(Context(prec=2, traps=[Inexact, Rounded])):
        got = to_display([usd(date(2024, 1, 2), "100.10")], rates)
    assert [str(p.price) for p in got] == ["33.37", "33.37"]  # 100.10 / 3 = 33.3666…


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "the file is empty"),
        (["\n", "\n"], "the file is empty"),
        (["Day,USD,GBP\n"], "line 1: expected the header"),
        (["Date\n"], "line 1: expected the header"),
        (["Date,USD,usd,GBP\n"], "three-letter currency codes"),
        (["Date,USD,GBP,USD\n"], "distinct three-letter"),
        (["Date,USD,JPY\n"], "lacks the USD or a display"),
        (["Date,GBP,JPY\n"], "lacks the USD or a display"),
        ([HEADER, "2024-01-02,1.0956,155.69,0.86,\n"], "line 2: expected 5 fields"),
        ([HEADER, "2024-1-02,1.0956,155.69,0.86,N/A,\n"], "not a date"),
        ([HEADER, "2024-02-30,1.0956,155.69,0.86,N/A,\n"], "not a date"),
        (
            [HEADER, "2024-01-02,1.0956,1,0.86,N/A,\n", "2024-01-02,1.0956,1,0.86,N/A,\n"],
            "line 3: out of date",
        ),
        ([HEADER, "2024-01-02,1.0956,1,0.86,N/A,\n", "2024-01-03,1.0956,1,0.86,N/A,\n"], "newest first"),
        ([HEADER, "2024-01-02,1,1,0.86,N/A,\n"], "not a positive rate"),
        ([HEADER, "2024-01-02,0.000000,1,0.86,N/A,\n"], "not a positive rate"),
        ([HEADER, "2024-01-02,1.0956,1,0.8600001,N/A,\n"], "not a positive rate"),
        ([HEADER, "2024-01-02,-1.0956,1,0.86,N/A,\n"], "not a positive rate"),
        ([HEADER, "2024-01-02,1e3,1,0.86,N/A,\n"], "not a positive rate"),
        ([HEADER, f"2024-01-02,{chr(0x661)}.0,1,0.86,N/A,\n"], "not a positive rate"),
    ],
)
def test_a_malformed_ecb_file_is_refused_with_its_line(lines: list[str], message: str) -> None:
    with pytest.raises(PriceError, match=message):
        ecb_rates(lines)


def test_a_byte_order_mark_and_blank_lines_at_the_end_are_ignored() -> None:
    lines = ["\ufeff" + HEADER, "2024-01-02,1.0956,155.69,0.86,N/A,\n", "\n", ""]
    assert ecb_rates(lines) == {date(2024, 1, 2): {"USD": 1_095_600, "GBP": 860_000}}
    assert ecb_rates("".join(lines).split("\n")) == ecb_rates(lines)


def test_a_blank_line_before_the_end_is_refused() -> None:
    with pytest.raises(PriceError, match="line 2: a blank line before the end"):
        ecb_rates([HEADER, "\n", "2024-01-02,1.0956,155.69,0.86,N/A,\n"])


def test_rates_of_currencies_never_shown_are_not_checked() -> None:
    assert ecb_rates([HEADER, "2024-01-02,1.0956,garbage,0.86,,\n"]) == {
        date(2024, 1, 2): {"USD": 1_095_600, "GBP": 860_000}
    }


_cents = st.integers(1, 10**15).map(lambda c: Decimal(c).scaleb(-2))
_rate = st.integers(1, 10**12)


@given(_cents, _rate, _rate)
def test_conversion_is_exact(price: Decimal, per_usd: int, per_gbp: int) -> None:
    day = date(2024, 1, 2)
    eur = Fraction(price) / Fraction(per_usd, 10**6)
    gbp = eur * Fraction(per_gbp, 10**6)
    rates = {day: {"USD": per_usd, "GBP": per_gbp}}
    if max(round(eur * 100), round(gbp * 100)) >= 10**17:  # beyond MAX_PRICE: only an absurd rate
        with pytest.raises(PriceError, match="out of range"):
            to_display([usd(day, str(price))], rates)
        return
    want = []
    for currency, exact in (("EUR", eur), ("GBP", gbp)):
        cents = round(exact * 100)  # half to even
        if cents:
            want.append(shown(day, currency, str(Decimal(cents).scaleb(-2))))
    assert to_display([usd(day, str(price))], rates) == want  # a conversion that rounds away is a gap

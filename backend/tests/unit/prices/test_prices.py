"""Combining and checking daily price series (PLAN §6, ADR 0007; THREAT_MODEL T-303, T-304)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from typing import Any

import pytest

from coinacct.prices import CENT, DailyPrice, Gap, Method, Outlier, PriceError, check, combine, content_hash


def p(day: int, price: str, method: Method = "vwap", currency: str = "USD") -> DailyPrice:
    return DailyPrice(date(2024, 1, day), currency, Decimal(price).quantize(CENT), method, "test")


@pytest.mark.parametrize(
    "fields",
    [
        {"price": Decimal("0.00")},
        {"price": Decimal("-1.00")},
        {"price": Decimal("0.009")},
        {"price": Decimal("1.005")},
        {"price": Decimal("1")},  # cents must be explicit: every parser makes two decimals
        {"price": Decimal("1.000")},
        {"price": Decimal("NaN")},
        {"price": Decimal("Infinity")},
        {"price": 1.0},
        {"price": "1.00"},
        {"currency": "JPY"},
        {"method": "close"},
        {"method": "fx"},  # an FX-derived price is never a USD (tax) price
        {"price": Decimal("1000000000000000.00")},  # 16 integer digits
        {"price": Decimal("1E+60")},
        {"source": ""},
        {"day": datetime(2024, 1, 1, tzinfo=UTC)},
    ],
)
def test_only_a_positive_whole_number_of_cents_is_a_price(fields: dict[str, Any]) -> None:
    good: dict[str, Any] = {
        "day": date(2024, 1, 1),
        "currency": "USD",
        "price": Decimal("1.00"),
        "method": "vwap",
        "source": "test",
    }
    DailyPrice(**good)
    with pytest.raises(PriceError):
        DailyPrice(**{**good, **fields})


def test_the_largest_price_is_accepted_and_checked_without_a_decimal_error() -> None:
    top = DailyPrice(date(2024, 1, 2), "USD", Decimal("999999999999999.99"), "vwap", "test")
    assert check([p(1, "1.00"), top])[1] == [Outlier(date(2024, 1, 2), Decimal("1.00"), top.price)]
    DailyPrice(date(2024, 1, 2), "EUR", Decimal("1.00"), "fx", "test")


def test_the_trade_average_wins_and_the_typical_price_fills_the_other_days() -> None:
    typical = [p(1, "1", "typical"), p(2, "2", "typical"), p(3, "3", "typical")]
    got = combine([p(2, "20"), p(4, "40")], typical)
    assert got == [p(1, "1", "typical"), p(2, "20"), p(3, "3", "typical"), p(4, "40")]


@pytest.mark.parametrize(
    ("vwap", "typical", "message"),
    [
        ([p(1, "1", "typical")], [], "expected a vwap price"),
        ([], [p(1, "1")], "expected a typical price"),
        ([p(1, "1"), p(1, "2")], [], "priced twice"),
        ([], [p(1, "1", "typical"), p(1, "1", "typical")], "priced twice"),
        ([p(1, "1")], [p(2, "1", "typical", "EUR")], "mix currencies"),
        ([p(1, "1")], [p(1, "1", "typical", "EUR")], "mix currencies"),  # even when VWAP would win the day
    ],
)
def test_combining_bad_series_is_refused(
    vwap: list[DailyPrice], typical: list[DailyPrice], message: str
) -> None:
    with pytest.raises(PriceError, match=message):
        combine(vwap, typical)


def test_empty_and_display_only_series_combine() -> None:
    assert combine([], []) == []
    eur = [p(1, "1", "typical", "EUR"), p(2, "2", "typical", "EUR")]
    assert combine([], eur) == eur


def test_gaps_are_reported_with_their_missing_days() -> None:
    assert check([p(1, "10"), p(2, "10"), p(5, "10"), p(7, "10")]) == (
        [Gap(date(2024, 1, 3), date(2024, 1, 4)), Gap(date(2024, 1, 6), date(2024, 1, 6))],
        [],
    )
    assert check([]) == ([], []) and check([p(1, "10")]) == ([], [])


def test_a_rise_over_half_or_a_fall_over_a_third_is_flagged_for_review() -> None:
    series = [p(1, "100"), p(2, "150"), p(3, "100"), p(4, "150.01"), p(5, "100.00")]
    assert check(series)[1] == [
        Outlier(date(2024, 1, 4), Decimal("100"), Decimal("150.01")),
        Outlier(date(2024, 1, 5), Decimal("150.01"), Decimal("100.00")),
    ]


def test_the_outlier_check_ignores_the_callers_decimal_context() -> None:
    series = [p(1, "100.00"), p(2, "150.00"), p(3, "225.01")]
    with localcontext(Context(prec=2, traps=[Inexact, Rounded])):
        assert check(series)[1] == [Outlier(date(2024, 1, 3), Decimal("150.00"), Decimal("225.01"))]


def test_a_move_across_a_gap_is_flagged_too() -> None:
    assert check([p(1, "100"), p(3, "1000"), p(5, "1001")]) == (
        [Gap(date(2024, 1, 2), date(2024, 1, 2)), Gap(date(2024, 1, 4), date(2024, 1, 4))],
        [Outlier(date(2024, 1, 3), Decimal("100"), Decimal("1000"))],
    )


@pytest.mark.parametrize("series", [[p(2, "1"), p(1, "1")], [p(1, "1"), p(1, "2")]])
def test_an_unsorted_series_is_refused(series: list[DailyPrice]) -> None:
    with pytest.raises(PriceError, match="isn't sorted by day"):
        check(series)


def test_the_content_hash_is_the_files_sha256() -> None:
    assert content_hash(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

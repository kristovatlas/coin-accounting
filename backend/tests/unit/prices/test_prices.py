"""Combining and checking daily price series (PLAN §6, ADR 0007; THREAT_MODEL T-303, T-304)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from coinacct.prices import DailyPrice, Gap, Method, Outlier, PriceError, check, combine, content_hash


def p(day: int, price: str, method: Method = "vwap", currency: str = "USD") -> DailyPrice:
    return DailyPrice(date(2024, 1, day), currency, Decimal(price), method, "test")


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
    ],
)
def test_combining_bad_series_is_refused(
    vwap: list[DailyPrice], typical: list[DailyPrice], message: str
) -> None:
    with pytest.raises(PriceError, match=message):
        combine(vwap, typical)


def test_gaps_are_reported_with_their_missing_days() -> None:
    assert check([p(1, "10"), p(2, "10"), p(5, "10"), p(7, "10")]) == (
        [Gap(date(2024, 1, 3), date(2024, 1, 4)), Gap(date(2024, 1, 6), date(2024, 1, 6))],
        [],
    )
    assert check([]) == ([], []) and check([p(1, "10")]) == ([], [])


def test_a_move_by_more_than_half_either_way_is_flagged_for_review() -> None:
    series = [p(1, "100"), p(2, "150"), p(3, "100"), p(4, "150.01"), p(5, "100.00")]
    assert check(series)[1] == [
        Outlier(date(2024, 1, 4), Decimal("100"), Decimal("150.01")),
        Outlier(date(2024, 1, 5), Decimal("150.01"), Decimal("100.00")),
    ]


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

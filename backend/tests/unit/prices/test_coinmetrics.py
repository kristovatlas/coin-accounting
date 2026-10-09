"""Coin Metrics' daily reference rate into daily USD prices (ADR 0039; THREAT_MODEL T-303, T-304)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from coinacct.prices import PriceError
from coinacct.prices.coinmetrics import SOURCE, reference_page

BEFORE = date(2024, 1, 10)


def row(day: str, price: Any = "1", **extra: Any) -> dict[str, Any]:
    return {"asset": "btc", "time": f"{day}T00:00:00.000000000Z", "PriceUSD": price, **extra}


def page(*rows: Any, **top: Any) -> str:
    return json.dumps({"data": list(rows), **top})


def prices(text: str, after: date | None = None) -> list[tuple[str, str]]:
    return [(p.day.isoformat(), str(p.price)) for p in reference_page(text, BEFORE, after).prices]


def test_a_row_dated_d_prices_day_d() -> None:
    # ADR 0039: the value dated D is D's close. Pinned against the API on 2026-10-09: 2024-01-01's
    # value was 44049.47, near that day's close (about 44,180), not its open (about 42,280)
    got = reference_page(page(row("2024-01-01", "44049.4735534775")), BEFORE, None)
    assert [(p.day, p.price, p.method, p.source, p.currency) for p in got.prices] == [
        (date(2024, 1, 1), Decimal("44049.47"), "reference", SOURCE, "USD")
    ]
    assert (got.last, got.next_token) == (date(2024, 1, 1), None)


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        ("1.005", "1.00"),  # half to even: down to the even cent
        ("1.015", "1.02"),  # and up to it
        ("1.0050000000000000001", "1.01"),  # past the half, at the 19th decimal
        ("0.08584", "0.09"),  # 2010-07-18's value, coarse as the ADR says
        ("7", "7.00"),
        ("999999999999999.99", "999999999999999.99"),  # the largest price
        ("0.12345678901234567890", "0.12"),  # 20 decimals
    ],
)
def test_values_are_rounded_exactly_half_to_even_to_cents(value: str, cents: str) -> None:
    assert prices(page(row("2024-01-01", value))) == [("2024-01-01", cents)]


@pytest.mark.parametrize("value", [None, "0", "0.004", "0.005"])
def test_a_day_without_a_value_or_below_a_cent_is_a_gap(value: str | None) -> None:
    got = reference_page(page(row("2024-01-01", value), row("2024-01-02", "2")), BEFORE, None)
    assert [p.day.day for p in got.prices] == [2]
    assert got.last == date(2024, 1, 2)


@pytest.mark.parametrize(
    "value",
    [
        "1e3",
        "-1",
        "+1",
        ".5",
        "1.",
        " 1",
        "1,000",
        "\u0661",  # an Arabic-Indic digit, which int() would take
        "1.000000000000000000001",  # 21 decimals
        "1000000000000000",  # 16 integer digits
        "NaN",
        "",
    ],
)
def test_a_value_that_isnt_a_plain_decimal_is_refused(value: str) -> None:
    with pytest.raises(PriceError, match=r"row 1: .* is not a plain decimal number"):
        prices(page(row("2024-01-01", value)))


def test_a_value_rounding_past_the_largest_price_is_refused() -> None:
    with pytest.raises(PriceError, match="out of range"):
        prices(page(row("2024-01-01", "999999999999999.995")))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "not valid JSON"),
        ('{"data": [], "data": []}', "not valid JSON"),  # a duplicate key
        ('{"data": [{"asset": "btc", "time": "2024-01-01T00:00:00Z", "PriceUSD": 1.5}]}', "not valid JSON"),
        ("[]", "expected data"),
        ("{}", "expected data"),
        ('{"data": [], "error": {}}', "expected data"),
        ('{"data": {}}', "a list of rows"),
        (page(row("2024-01-01", 5)), "not valid JSON"),  # a bare number, even an integer
        (page("a row"), "expected asset, time and PriceUSD"),
        (page(row("2024-01-01", extra="x")), "expected asset, time and PriceUSD"),
        (page({"asset": "btc", "time": "2024-01-01T00:00:00Z"}), "expected asset, time and PriceUSD"),
        (page({**row("2024-01-01"), "asset": "eth"}), "not BTC"),
        (page({**row("2024-01-01"), "PriceUSD": True}), "price as a string"),
        (page({**row("2024-01-01"), "time": "2024-01-01T01:00:00.000000000Z"}), "not a day's 00:00 UTC"),
        (page({**row("2024-01-01"), "time": "2024-01-01"}), "not a day's 00:00 UTC"),
        (page({**row("2024-01-01"), "time": 20240101}), "not valid JSON"),
        (page({**row("2024-01-01"), "time": ["2024-01-01"]}), "not a day's 00:00 UTC"),
        (page({**row("2024-01-01"), "time": "2024-02-30T00:00:00Z"}), "not a real date"),
        (page(row("2010-07-17")), "before Coin Metrics' first day"),
        (page(row("2024-01-02"), row("2024-01-01")), "row 2: 2024-01-01 is out of day order"),
        (page(row("2024-01-02"), row("2024-01-02", None)), "row 2: 2024-01-02 is out of day order"),
        (page(row("2024-01-10")), "2024-01-10 isn't over yet"),  # the refresh's own day
        (page(row("2024-01-09"), row("2024-02-01", None)), "row 2: 2024-02-01 isn't over yet"),
    ],
)
def test_anything_unexpected_fails_the_whole_page(text: str, message: str) -> None:
    with pytest.raises(PriceError, match=message):
        prices(text)


def test_the_days_must_follow_the_last_page() -> None:
    assert prices(page(row("2024-01-02")), after=date(2024, 1, 1)) == [("2024-01-02", "1.00")]
    with pytest.raises(PriceError, match="row 1: 2024-01-01 is out of day order"):
        prices(page(row("2024-01-01")), after=date(2024, 1, 1))


def test_the_next_pages_token_is_read_and_its_url_ignored() -> None:
    got = reference_page(
        page(row("2024-01-01"), next_page_token="0.MjAyNC0wMS0wMVQwMDowMDowMFo", next_page_url="https://x/"),
        BEFORE,
        None,
    )
    assert got.next_token == "0.MjAyNC0wMS0wMVQwMDowMDowMFo"


@pytest.mark.parametrize(
    "token",
    ["", "abc", "0.a&b=c", "0.a/b", "0.a b", "1." + "a" * 201, "1234.a", "0.a\n", 5, None, ["0.a"]],
)
def test_a_token_not_in_the_expected_form_is_refused(token: Any) -> None:
    text = page(row("2024-01-01"), next_page_token=token)
    if token is None:  # JSON null: the same as no next page
        assert reference_page(text, BEFORE, None).next_token is None
        return
    with pytest.raises(PriceError, match=r"token|not valid JSON"):
        reference_page(text, BEFORE, None)


def test_an_empty_page_has_no_last_day() -> None:
    got = reference_page(page(), BEFORE, date(2024, 1, 1))
    assert (got.prices, got.last, got.next_token) == ([], None, None)

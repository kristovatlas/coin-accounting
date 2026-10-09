"""A price CSV the user uploads, the fallback when a source disappears (ADR 0007; THREAT_MODEL T-304,
T-701, TB6)."""

from __future__ import annotations

from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext

import pytest

from coinacct.prices import DailyPrice, PriceError, csv_import
from coinacct.prices.csv_import import HEADER, parse


def row(day: str, currency: str, price: str) -> DailyPrice:
    return DailyPrice(date.fromisoformat(day), currency, Decimal(price), "import", "csv-import")


def test_each_currencys_series_comes_back_sorted_by_day() -> None:
    data = (f"{HEADER}\n2024-01-03,USD,45000\n2024-01-02,USD,44950.12\n2024-01-02,EUR,41000.5\r\n").encode()
    assert parse(data) == {
        "EUR": [row("2024-01-02", "EUR", "41000.50")],
        "USD": [row("2024-01-02", "USD", "44950.12"), row("2024-01-03", "USD", "45000.00")],
    }


def test_blank_lines_may_end_the_file_and_an_empty_file_body_is_empty() -> None:
    assert parse(f"{HEADER}\n2024-01-02,GBP,1\n\n\n".encode()) == {"GBP": [row("2024-01-02", "GBP", "1.00")]}
    assert parse(f"{HEADER}\n".encode()) == {}


def test_the_prices_are_exact_whatever_the_callers_decimal_context() -> None:
    with localcontext(Context(prec=2, traps=[Inexact, Rounded])):
        (p,) = parse(f"{HEADER}\n2024-01-02,USD,123456.7\n".encode())["USD"]
    assert str(p.price) == "123456.70"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "line 1: expected the header"),
        ("Date,Currency,Price\n", "line 1: expected the header"),
        (f"{HEADER},extra\n", "line 1: expected the header"),
        (f"{HEADER}\n2024-01-02,USD\n", "line 2: expected date,currency,price"),
        (f"{HEADER}\n2024-01-02,USD,1,2\n", "line 2: expected date,currency,price"),
        (f"{HEADER}\n\n2024-01-02,USD,1\n", "line 2: expected date,currency,price"),  # a blank line inside
        (f"{HEADER}\n2024-1-02,USD,1\n", "line 2: '2024-1-02' is not a date"),
        (f"{HEADER}\n2024-02-30,USD,1\n", "line 2: '2024-02-30' is not a date"),
        (f"{HEADER}\n2024-01-02,usd,1\n", "line 2: the currency must be one of USD, EUR, GBP"),
        (f"{HEADER}\n2024-01-02,JPY,1\n", "line 2: the currency must be one of"),
        (f"{HEADER}\n2024-01-02,USD,1.005\n", "line 2: the price must be a plain amount"),  # no rounding
        (f"{HEADER}\n2024-01-02,USD,-1\n", "line 2: the price must be a plain amount"),
        (f"{HEADER}\n2024-01-02,USD,1e5\n", "line 2: the price must be a plain amount"),
        (f"{HEADER}\n2024-01-02,USD, 1\n", "line 2: the price must be a plain amount"),
        (f"{HEADER}\n2024-01-02,USD,1.\n", "line 2: the price must be a plain amount"),
        (f"{HEADER}\n2024-01-02,USD,1000000000000000\n", "line 2: the price must be a plain amount"),
        (f"{HEADER}\n2024-01-02,USD,0\n", "line 2: 2024-01-02: a price must be a positive whole number"),
        (f"{HEADER}\n2024-01-02,USD,0.00\n", "line 2: 2024-01-02: a price must be a positive"),
        (f"{HEADER}\n2024-01-02,USD,1\n2024-01-02,USD,2\n", "line 3: USD on 2024-01-02 is priced twice"),
        (f"{HEADER}\n2024-01-02,USD,1\r\n2024-01-03,USD,2\r", "carriage return that doesn't end a line"),
        (f"{HEADER}\n2024-01-02,USD,1\t\n", "isn't printable ASCII"),
        (f"{HEADER}\n2024-01-02,USD,1\x0c\n", "isn't printable ASCII"),
        (f"{HEADER}\n2024-01-02,USD,{chr(0x661)}\n", "isn't printable ASCII"),
        (f"{chr(0xFEFF)}{HEADER}\n", "isn't printable ASCII"),  # a byte-order mark: say it, don't guess
    ],
)
def test_a_malformed_file_is_refused_whole_naming_its_line(body: str, message: str) -> None:
    with pytest.raises(PriceError, match=message):
        parse(body.encode("utf-8"))


def test_a_file_too_large_or_too_long_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PriceError, match="larger than 8 MB"):
        parse(b"x" * (csv_import.MAX_BYTES + 1))
    monkeypatch.setattr(csv_import, "MAX_ROWS", 2)
    body = f"{HEADER}\n2024-01-01,USD,1\n2024-01-02,USD,1\n2024-01-03,USD,1\n"
    with pytest.raises(PriceError, match="more than 2 rows"):
        parse(body.encode())


def test_a_bad_row_after_good_ones_still_refuses_the_whole_file() -> None:
    with pytest.raises(PriceError, match="line 4"):
        parse(f"{HEADER}\n2024-01-01,USD,1\n2024-01-02,USD,2\njunk\n".encode())


def test_imported_prices_are_marked_as_imported_for_every_currency() -> None:
    got = parse(f"{HEADER}\n2024-01-02,USD,1\n2024-01-02,EUR,1\n2024-01-02,GBP,1\n".encode())
    assert {p.method for s in got.values() for p in s} == {"import"}
    assert {p.source for s in got.values() for p in s} == {"csv-import"}

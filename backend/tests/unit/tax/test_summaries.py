"""The income summary, year summary and holdings reports (PLAN §8): hand-worked figures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from coinacct.tax.engine import Acquisition, Disposal, EngineError, Event, run
from coinacct.tax.reports import (
    Blocked,
    Holding,
    IncomeRow,
    Term,
    YearSummary,
    holdings,
    income_summary,
    value_of,
    year_summary,
)

D = Decimal
BTC = 100_000_000


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def sell(id: str, on: date, sats: int, proceeds: str) -> Disposal:
    return Disposal(id, "x", on, at(on), "sell", sats, D(proceeds))


def test_the_income_summary_lists_the_years_income_with_its_fmv_and_source() -> None:
    events: list[Event] = [
        Acquisition("pay1", "w", date(2025, 1, 31), "income", 1_000, D("1.00")),
        Acquisition("buy", "w", date(2025, 2, 1), "buy", 1_000, D("1.00")),
        Acquisition("pay0", "w", date(2025, 1, 15), "income", 500, D("0.60")),
        Acquisition("pay24", "w", date(2024, 12, 31), "income", 1, D("0.01")),
    ]
    summary = income_summary(events, 2025, {"pay1": "Employer"})
    assert summary.rows == (
        IncomeRow("pay0", date(2025, 1, 15), 500, D("0.60"), None),
        IncomeRow("pay1", date(2025, 1, 31), 1_000, D("1.00"), "Employer"),
    )
    assert summary.total == D("1.60")


def test_income_without_its_fmv_raises() -> None:
    with pytest.raises(EngineError, match="FMV at receipt"):
        income_summary([Acquisition("pay", "w", date(2025, 1, 1), "income", 1, None)], 2025)


def test_the_year_summary_totals_each_term() -> None:
    events: list[Event] = [
        Acquisition("old", "x", date(2023, 1, 10), "buy", 1_000, D("10.00")),
        Acquisition("new", "x", date(2025, 2, 1), "buy", 1_000, D("40.00")),
        sell("s1", date(2025, 6, 1), 1_500, "60.00"),  # old 1,000 (long) for 40.00, new 500 (short) for 20.00
        sell("s2", date(2026, 1, 5), 500, "30.00"),
    ]
    summary = year_summary(run(events), 2025)
    assert summary == YearSummary(
        2025,
        short=Term(1, D("20.00"), D("20.00"), D("0.00")),
        long=Term(1, D("40.00"), D("10.00"), D("30.00")),
        replay_stopped=None,
    )


def test_the_year_summary_is_blocked_while_anything_blocks() -> None:
    result = run([sell("s", date(2025, 6, 1), 10, "1.00")])
    assert year_summary(result, 2025) == Blocked(result.blocking)


def test_holdings_show_the_holding_period_on_the_date_and_the_unrealized_gain() -> None:
    events: list[Event] = [
        Acquisition("old", "x", date(2024, 1, 10), "buy", BTC + 50_000_000, D("60000.00")),
        Acquisition("new", "x", date(2025, 2, 1), "buy", 25_000_000, D("25000.00")),
        Acquisition("inh", "w", date(2025, 3, 1), "inherit", 1_000, D("1.00")),
    ]
    rows = holdings(run(events), date(2025, 3, 1), D("100000.00"))
    assert rows == (
        Holding("inh", "w", date(2025, 3, 1), 1_000, D("1.00"), None, True, False, D("1.00"), D("0.00")),
        Holding(
            "old",
            "x",
            date(2024, 1, 10),
            BTC + 50_000_000,
            D("60000.00"),
            None,
            True,
            False,
            D("150000.00"),
            D("90000.00"),
        ),
        Holding(
            "new",
            "x",
            date(2025, 2, 1),
            25_000_000,
            D("25000.00"),
            None,
            False,
            False,
            D("25000.00"),
            D("0.00"),
        ),
    )


def test_holdings_without_a_price_have_no_value() -> None:
    (row,) = holdings(run([Acquisition("b", "x", date(2025, 1, 1), "buy", 1, D("1.00"))]), date(2025, 1, 2))
    assert (row.value, row.unrealized) == (None, None)


def test_holdings_as_of_a_date_before_a_lot_raise() -> None:
    with pytest.raises(EngineError, match="run the engine up to that date"):
        holdings(run([Acquisition("b", "x", date(2025, 1, 1), "buy", 1, D("1.00"))]), date(2024, 12, 31))


@pytest.mark.parametrize(
    ("sats", "price", "value"),
    [
        (1, "100000.00", "0.00"),
        (500, "100000.00", "0.50"),
        (150_000_000, "100000.00", "150000.00"),
        (5, "100000.00", "0.00"),
    ],
)
def test_the_value_of_sats_is_to_the_cent(sats: int, price: str, value: str) -> None:
    assert value_of(sats, D(price)) == D(value)

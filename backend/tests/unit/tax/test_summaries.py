"""The income summary, year summary and holdings reports (PLAN §8): hand-worked figures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Any

import pytest

from coinacct.domain.money import value_of
from coinacct.tax.engine import Acquisition, Disposal, EngineError, Event, Pick, run
from coinacct.tax.reports import (
    Blocked,
    Holding,
    Holdings,
    IncomeRow,
    IncomeSummary,
    Sale,
    Term,
    YearSummary,
    form_8949,
    holdings,
    income_summary,
    year_summary,
)

D = Decimal
BTC = 100_000_000


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def sell(id: str, on: date, sats: int, proceeds: str) -> Disposal:
    return Disposal(id, "x", on, at(on), "sell", sats, D(proceeds))


INCOME: list[Event] = [
    Acquisition("pay1", "w", date(2025, 1, 31), "income", 1_000, D("1.00")),
    Acquisition("buy", "w", date(2025, 2, 1), "buy", 1_000, D("1.00")),
    Acquisition("pay0", "w", date(2025, 1, 15), "income", 500, D("0.60")),
    Acquisition("pay24", "w", date(2024, 12, 31), "income", 1, D("0.01")),
]


def income(
    events: list[Event], year: int = 2025, *, blocking: Any = (), sources: Any = None
) -> IncomeSummary | Blocked:
    return income_summary(events, run(events), year, blocking=blocking, sources=sources)


def test_the_income_summary_lists_the_years_income_with_its_fmv_and_source() -> None:
    summary = income(sorted(INCOME, key=lambda e: e.on), sources={"pay1": "Employer"})
    assert isinstance(summary, IncomeSummary)
    assert summary.rows == (
        IncomeRow("pay0", date(2025, 1, 15), 500, D("0.60"), None),
        IncomeRow("pay1", date(2025, 1, 31), 1_000, D("1.00"), "Employer"),
    )
    assert summary.total == D("1.60")
    assert summary.ruleset and summary.positions


def test_an_empty_income_year_totals_nothing() -> None:
    summary = income(sorted(INCOME, key=lambda e: e.on), 2023)
    assert isinstance(summary, IncomeSummary)
    assert (summary.rows, summary.total) == ((), D("0.00"))


def test_the_income_summary_is_blocked_while_anything_blocks_t509() -> None:
    gift = Acquisition("g", "w", date(2025, 1, 2), "gift_in", 1, None, D("1.00"), date(2024, 1, 1))
    events: list[Event] = [gift, INCOME[0]]
    assert income(events) == Blocked(run(events).blocking)
    assert income([INCOME[0]], blocking=["unconfirmed tx"]) == Blocked(("unconfirmed tx",))


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"sources": {"buy": "Shop"}}, "aren't income"),
        ({"sources": {"pay1": 1}}, "names of who paid"),
        ({"blocking": iter(())}, "list or tuple"),
    ],
)
def test_the_income_summary_checks_its_inputs(kw: dict[str, Any], match: str) -> None:
    with pytest.raises(EngineError, match=match):
        income(sorted(INCOME, key=lambda e: e.on), **kw)


def test_the_income_summary_checks_its_year() -> None:
    with pytest.raises(EngineError):
        income([INCOME[0]], 2012)


def test_income_without_its_fmv_raises() -> None:
    events: list[Event] = [Acquisition("pay", "w", date(2025, 1, 1), "income", 1, D("1.00"))]
    bad = [Acquisition("pay", "w", date(2025, 1, 1), "income", 1, None)]
    with pytest.raises(EngineError, match="FMV at receipt"):
        income_summary(bad, run(events), 2025, blocking=())


def test_the_income_summary_needs_the_run_of_its_events() -> None:
    events = sorted(INCOME, key=lambda e: e.on)
    with pytest.raises(EngineError, match="run of these events"):
        income_summary(events, run(events[:-1]), 2025, blocking=())


def test_the_income_summary_shows_the_late_warnings_and_a_stopped_replay_t508() -> None:
    late = Disposal(
        "s",
        "x",
        date(2025, 6, 1),
        at(date(2025, 6, 1)),
        "sell",
        500,
        D("30.00"),
        picks=(Pick("new", 500),),
        identified_at=datetime(2025, 6, 2, tzinfo=UTC),
    )
    events: list[Event] = [*YEAR[:2], late]
    summary = income(events)
    assert isinstance(summary, IncomeSummary)
    assert [w.disposal for w in summary.late] == ["s"]
    assert summary.replay_stopped is None


YEAR: list[Event] = [
    Acquisition("old", "x", date(2023, 1, 10), "buy", 1_000, D("10.00")),
    Acquisition("new", "x", date(2025, 2, 1), "buy", 1_000, D("40.00")),
    sell("s1", date(2025, 6, 1), 1_500, "60.00"),  # old 1,000 (long) for 40.00, new 500 (short) for 20.00
    sell("s2", date(2025, 7, 1), 250, "5.00"),  # new 250 (short): a 5.00 loss
    sell("s3", date(2026, 1, 5), 250, "30.00"),
]


def test_the_year_summary_totals_each_term() -> None:
    summary = year_summary(run(YEAR), 2025, blocking=())
    assert summary == YearSummary(
        2025,
        short=Term(2, D("25.00"), D("30.00"), D("-5.00")),
        long=Term(1, D("40.00"), D("10.00"), D("30.00")),
        replay_stopped=None,
    )


def test_the_year_summary_matches_form_8949s_totals() -> None:
    result = run(YEAR)
    summary = year_summary(result, 2025, blocking=())
    form = form_8949(result, {s: Sale("exchange", True) for s in ("s1", "s2", "s3")}, 2025, blocking=())
    assert isinstance(summary, YearSummary) and not isinstance(form, Blocked)
    for term, boxes in ((summary.short, "GHI"), (summary.long, "JKL")):
        totals = [t for t in form.totals if t.box in boxes]
        assert term.rows == sum(t.rows for t in totals)
        assert term.gain == sum((t.gain for t in totals), D("0.00"))


def test_the_year_summary_is_blocked_while_anything_blocks() -> None:
    result = run([sell("s", date(2025, 6, 1), 10, "1.00")])
    assert year_summary(result, 2025, blocking=()) == Blocked(result.blocking)


def test_the_year_summary_is_blocked_by_the_callers_conditions_too() -> None:
    result = run([Acquisition("b", "x", date(2025, 1, 2), "buy", 10, D("1.00"))])
    assert year_summary(result, 2025, blocking=["unconfirmed tx"]) == Blocked(("unconfirmed tx",))
    with pytest.raises(EngineError, match="list or tuple"):
        year_summary(result, 2025, blocking=iter(()))  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        year_summary(result, 2012, blocking=())


def test_holdings_show_the_holding_period_on_the_date_and_the_unrealized_gain() -> None:
    events: list[Event] = [
        Acquisition("old", "x", date(2024, 3, 1), "buy", BTC + 50_000_000, D("60000.00")),
        Acquisition("new", "x", date(2025, 2, 1), "buy", 25_000_000, D("25000.00")),
        Acquisition("inh", "w", date(2025, 3, 1), "inherit", 1_000, D("1.00")),
    ]
    found = holdings(run(events), date(2025, 3, 1), D("100000.00"), blocking=())
    assert isinstance(found, Holdings)
    assert found.rows == (
        Holding(
            "inh",
            "w",
            date(2025, 3, 1),
            1_000,
            D("1.00"),
            None,
            None,
            True,
            None,
            False,
            D("1.00"),
            D("0.00"),
        ),
        # 2024-03-01 to 2025-03-01 is exactly a year: not more than one, so short-term
        Holding(
            "old",
            "x",
            date(2024, 3, 1),
            BTC + 50_000_000,
            D("60000.00"),
            None,
            None,
            False,
            None,
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
            None,
            False,
            None,
            False,
            D("25000.00"),
            D("0.00"),
        ),
    )
    assert (found.as_of, found.replay_stopped, found.late) == (date(2025, 3, 1), None, ())


@pytest.mark.parametrize(
    ("price", "unrealized", "long_term"),
    [
        ("12000.00", "2.00", True),  # above the donor's $10 basis: a gain, the donor's date
        ("8000.00", "0.00", True),  # between the $6 FMV and $10: neither
        ("5000.00", "-1.00", False),  # under the $6 FMV: a loss against it, from the gift date
    ],
)
def test_a_gifts_holding_follows_its_dual_basis_t510(price: str, unrealized: str, long_term: bool) -> None:
    # 1,000 sats... scaled: 0.001 BTC; donor basis $10 from 2020, FMV $6 at the gift on 2025-01-10
    gift = Acquisition(
        "g", "w", date(2025, 1, 10), "gift_in", 100_000, D("10.00"), D("6.00"), date(2020, 1, 1)
    )
    found = holdings(run([gift]), date(2025, 6, 1), D(price), blocking=())
    assert isinstance(found, Holdings)
    (row,) = found.rows
    assert (row.loss_basis, row.loss_from) == (D("6.00"), date(2025, 1, 10))
    assert (row.long_term, row.loss_long_term) == (True, False)
    assert row.unrealized == D(unrealized)
    side = row.long_term if row.unrealized is None or row.unrealized >= 0 else row.loss_long_term
    assert side is long_term


def test_holdings_without_a_price_have_no_value() -> None:
    found = holdings(
        run([Acquisition("b", "x", date(2025, 1, 1), "buy", 1, D("1.00"))]), date(2025, 1, 2), blocking=()
    )
    assert isinstance(found, Holdings)
    (row,) = found.rows
    assert (row.value, row.unrealized) == (None, None)


def test_holdings_are_blocked_while_anything_blocks_t509() -> None:
    gift = Acquisition("g", "w", date(2025, 1, 2), "gift_in", 1, None, D("1.00"), date(2024, 1, 1))
    result = run([gift])
    assert holdings(result, date(2025, 2, 1), blocking=()) == Blocked(result.blocking)
    ok = run([Acquisition("b", "x", date(2025, 1, 1), "buy", 1, D("1.00"))])
    assert holdings(ok, date(2025, 2, 1), blocking=["unconfirmed tx"]) == Blocked(("unconfirmed tx",))


def test_holdings_need_a_run_up_to_the_date() -> None:
    # a sale after the date would have used lots held on it: the run must stop at the date
    events: list[Event] = [
        Acquisition("b", "x", date(2025, 1, 1), "buy", 10, D("1.00")),
        sell("s", date(2025, 3, 1), 5, "1.00"),
    ]
    with pytest.raises(EngineError, match="events after"):
        holdings(run(events), date(2025, 2, 1), blocking=())
    with pytest.raises(EngineError, match="as of a date"):
        holdings(run(events), datetime(2025, 3, 2, tzinfo=UTC), blocking=())  # a datetime is no tax date


@pytest.mark.parametrize("price", ["-1.00", "0.00", "0.001"])
def test_a_bad_price_raises(price: str) -> None:
    with pytest.raises(EngineError, match="the price"):
        holdings(run([]), date(2025, 1, 1), D(price), blocking=())


def test_a_value_too_large_raises() -> None:
    big = run([Acquisition("b", "x", date(2025, 1, 1), "buy", 2_000_000_000_000_000, D("1.00"))])
    with pytest.raises(EngineError, match="sats at"):
        holdings(big, date(2025, 1, 2), D("999999999999999.99"), blocking=())


def test_an_inherited_gifts_loss_side_runs_from_the_gift_date_t510() -> None:
    # a donor's inherited lot (always long-term on the donor's basis) given at an FMV below that basis:
    # under the FMV, the loss is short-term until a year after the gift, as a sale would figure it
    gift = Acquisition(
        "g", "w", date(2025, 1, 10), "gift_in", 100_000, D("10.00"), D("6.00"), date(2024, 12, 1), True
    )
    found = holdings(run([gift]), date(2025, 6, 1), D("5000.00"), blocking=())
    assert isinstance(found, Holdings)
    (row,) = found.rows
    assert (row.long_term, row.loss_long_term, row.unrealized) == (True, False, D("-1.00"))
    sale = Disposal("s", "w", date(2025, 6, 1), at(date(2025, 6, 1)), "sell", 100_000, D("5.00"))
    (sold,) = run([gift, sale]).allocations
    assert (sold.long_term, sold.gain) == (row.loss_long_term, row.unrealized)


@pytest.mark.parametrize(
    ("price", "unrealized"),
    [("10000.00", "0.00"), ("6000.00", "0.00"), ("5990.00", "-0.01")],
)
def test_a_gifts_unrealized_edges_match_a_sale_t510(price: str, unrealized: str) -> None:
    gift = Acquisition(
        "g", "w", date(2025, 1, 10), "gift_in", 100_000, D("10.00"), D("6.00"), date(2020, 1, 1)
    )
    found = holdings(run([gift]), date(2025, 6, 1), D(price), blocking=())
    assert isinstance(found, Holdings)
    (row,) = found.rows
    assert row.unrealized == D(unrealized)
    value = value_of(100_000, D(price))
    sale = Disposal("s", "w", date(2025, 6, 1), at(date(2025, 6, 1)), "sell", 100_000, value)
    (sold,) = run([gift, sale]).allocations
    assert sold.gain == row.unrealized


@pytest.mark.parametrize(
    ("sats", "price", "value"),
    [
        (1, "100000.00", "0.00"),
        (500, "100000.00", "0.50"),
        (150_000_000, "100000.00", "150000.00"),
        (5, "100000.00", "0.00"),
        (150_000_000, "1.01", "1.52"),  # 1.515, rounded once (half to even), not 1.01 + 0.51
    ],
)
def test_the_value_of_sats_is_rounded_once_to_the_cent_t502(sats: int, price: str, value: str) -> None:
    assert value_of(sats, D(price)) == D(value)
    with localcontext() as ctx:  # the caller's context changes nothing
        ctx.prec = 3
        assert value_of(sats, D(price)) == D(value)


def test_the_year_summary_lists_the_late_choices_its_totals_rest_on_t508() -> None:
    late = Disposal(
        "s",
        "x",
        date(2025, 6, 1),
        at(date(2025, 6, 1)),
        "sell",
        500,
        D("30.00"),
        picks=(Pick("new", 500),),
        identified_at=datetime(2025, 6, 2, tzinfo=UTC),
    )
    summary = year_summary(run([*YEAR[:2], late]), 2025, blocking=())
    assert isinstance(summary, YearSummary)
    assert [w.disposal for w in summary.late] == ["s"]

"""Form 8949 rows and boxes (ADR 0011, ADR 0043; T-510): hand-worked figures from engine runs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from coinacct.tax.engine import Acquisition, Disposal, EngineError, Event, Pick, run
from coinacct.tax.reports import POSITIONS, Blocked, Form8949, Row, Sale, Total, describe, form_8949
from coinacct.tax.rules import RULESET, box

D = Decimal


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def sell(id: str, on: date, sats: int, proceeds: str, **kw: object) -> Disposal:
    return Disposal(id, kw.pop("account", "x"), on, at(on), "sell", sats, D(proceeds), **kw)  # type: ignore[arg-type]


BUYS: list[Event] = [
    Acquisition("old", "x", date(2023, 1, 10), "buy", 1_000, D("10.00")),
    Acquisition("new", "x", date(2025, 2, 1), "buy", 1_000, D("40.00")),
]


def report(events: list[Event], sales: dict[str, Sale], year: int = 2025) -> Form8949:
    out = form_8949(run(events), sales, year)
    assert isinstance(out, Form8949)
    return out


def test_an_exchange_sale_in_2025_goes_in_k_and_h_by_term() -> None:
    # FIFO: the 2023 lot (long-term) then the 2025 one (short-term); proceeds split by sats
    s = sell("s", date(2025, 6, 1), 2_000, "100.00")
    form = report([*BUYS, s], {"s": Sale.default("exchange", 2025)})
    assert form.rows == (
        Row(
            "H",
            "0.00001000 BTC",
            date(2025, 2, 1),
            date(2025, 6, 1),
            D("50.00"),
            D("40.00"),
            D("10.00"),
            False,
            "s",
            "new",
        ),
        Row(
            "K",
            "0.00001000 BTC",
            date(2023, 1, 10),
            date(2025, 6, 1),
            D("50.00"),
            D("10.00"),
            D("40.00"),
            True,
            "s",
            "old",
        ),
    )
    assert form.totals == (
        Total("H", 1, D("50.00"), D("40.00"), D("10.00")),
        Total("K", 1, D("50.00"), D("10.00"), D("40.00")),
    )
    assert form.ruleset == RULESET and form.positions == POSITIONS and form.replay_stopped is None


@pytest.mark.parametrize(
    ("year", "reported", "basis", "short", "long"),
    [
        (2025, False, False, "I", "L"),
        (2026, True, True, "G", "J"),  # covered coins, basis reported
        (2025, True, False, "H", "K"),
        (2024, False, False, "C", "F"),
        (2024, True, True, "A", "D"),
        (2024, True, False, "B", "E"),
        (2020, False, False, "C", "F"),
    ],
)
def test_the_box_follows_the_year_and_the_forms(
    year: int, reported: bool, basis: bool, short: str, long: str
) -> None:
    assert box(year, False, reported, basis) == short
    assert box(year, True, reported, basis) == long


def test_the_defaults_follow_adr_0011() -> None:
    assert Sale.default("onchain", 2025) == Sale("onchain", reported=False)
    assert Sale.default("exchange", 2025) == Sale("exchange", reported=True)
    assert Sale.default("exchange", 2024) == Sale("exchange", reported=False)


def test_only_the_tax_years_disposals_have_rows_and_gifts_have_none() -> None:
    events: list[Event] = [
        BUYS[0],
        sell("s24", date(2024, 3, 1), 500, "20.00"),
        BUYS[1],
        Disposal("g", "x", date(2025, 3, 1), at(date(2025, 3, 1)), "gift_out", 500, D("0.00")),
        sell("s25", date(2025, 6, 1), 500, "30.00"),
    ]
    sales = {"s24": Sale.default("exchange", 2024), "s25": Sale.default("exchange", 2025)}
    assert [r.disposal for r in report(events, sales).rows] == ["s25"]
    assert [r.disposal for r in report(events, sales, 2024).rows] == ["s24"]


def test_an_override_replaces_the_box_and_records_the_reason() -> None:
    s = sell("s", date(2025, 6, 1), 2_000, "100.00")
    sale = Sale("exchange", True, override_short="I", override_long="L", reason="no 1099-DA received")
    rows = report([*BUYS, s], {"s": sale}).rows
    assert [(r.box, r.override_reason) for r in rows] == [
        ("I", "no 1099-DA received"),
        ("L", "no 1099-DA received"),
    ]


@pytest.mark.parametrize(
    ("sale", "match"),
    [
        (Sale("exchange", True, override_long="H", reason="x"), "long-term rows"),
        (Sale("exchange", True, override_long="F", reason="x"), "isn't used for digital assets in 2025"),
        (Sale("exchange", True, override_long="L"), "the user's reason"),
        (Sale("exchange", True, override_long="L", reason="  "), "the user's reason"),
        (Sale("exchange", True, override_long="Z", reason="x"), "isn't one"),  # type: ignore[arg-type]
    ],
)
def test_a_bad_override_raises(sale: Sale, match: str) -> None:
    with pytest.raises(EngineError, match=match):
        form_8949(run([BUYS[0], sell("s", date(2025, 6, 1), 1_000, "50.00")]), {"s": sale}, 2025)


def test_basis_without_a_form_raises() -> None:
    with pytest.raises(EngineError, match="only on a form"):
        box(2025, False, False, True)


@pytest.mark.parametrize("year", [2008, "2025", True])
def test_a_tax_year_is_a_whole_number(year: object) -> None:
    with pytest.raises(EngineError, match="tax year"):
        box(year, False, False, False)  # type: ignore[arg-type]


def test_a_disposal_without_its_sale_facts_raises() -> None:
    with pytest.raises(EngineError, match="channel and forms"):
        form_8949(run([BUYS[0], sell("s", date(2025, 6, 1), 1_000, "50.00")]), {}, 2025)


def test_a_blocking_condition_blocks_the_report() -> None:
    result = run([BUYS[0], sell("s", date(2025, 6, 1), 2_000, "50.00")])  # 1,000 sats have no lots
    out = form_8949(result, {"s": Sale.default("exchange", 2025)}, 2025)
    assert out == Blocked(result.blocking)


def test_a_late_choice_flags_its_rows_but_keeps_them() -> None:
    late = sell(
        "s",
        date(2025, 6, 1),
        1_000,
        "50.00",
        picks=(Pick("new", 1_000),),
        identified_at=at(date(2025, 6, 1), 18),
    )
    rows = report([*BUYS, late], {"s": Sale.default("exchange", 2025)}).rows
    assert [(r.lot, r.late) for r in rows] == [("new", True)]


@pytest.mark.parametrize(
    ("sats", "text"),
    [(1, "0.00000001 BTC"), (123_456_789, "1.23456789 BTC"), (100_000_000, "1.00000000 BTC")],
)
def test_the_description_names_the_amount_in_btc(sats: int, text: str) -> None:
    assert describe(sats) == text

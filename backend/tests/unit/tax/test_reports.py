"""Form 8949 rows and boxes (ADR 0011, ADR 0040, ADR 0043; T-510, T-508): hand-worked figures from
engine runs. Box rules cite the Instructions for Form 8949 (2024: A-F; 2025: G-L)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from coinacct.tax.engine import Acquisition, Disposal, EngineError, Event, Pick, Transfer, run
from coinacct.tax.reports import (
    POSITIONS,
    Blocked,
    Broker,
    BrokerFee,
    Form8949,
    Row,
    Sale,
    Total,
    describe,
    form_8949,
)
from coinacct.tax.rules import RULESET, box

D = Decimal


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def sell(id: str, on: date, sats: int, proceeds: str, **kw: Any) -> Disposal:
    return Disposal(id, kw.pop("account", "x"), on, at(on), kw.pop("kind", "sell"), sats, D(proceeds), **kw)


BUYS: list[Event] = [
    Acquisition("old", "x", date(2023, 1, 10), "buy", 1_000, D("10.00")),
    Acquisition("new", "x", date(2025, 2, 1), "buy", 1_000, D("40.00")),
]
EXCHANGE_2025 = Sale.default("exchange", 2025)


def report(events: list[Event], sales: dict[str, Sale], year: int = 2025) -> Form8949:
    out = form_8949(run(events), sales, year, blocking=())
    assert isinstance(out, Form8949)
    return out


def test_an_exchange_sale_in_2025_goes_in_h_and_k_by_term_t510() -> None:
    # 2025 i8949: reported on a 1099-DA without basis, H (short) / K (long). FIFO: the 2023 lot
    # (long-term) then the 2025 one (short-term); the proceeds split by sats
    form = report([*BUYS, sell("s", date(2025, 6, 1), 2_000, "100.00")], {"s": EXCHANGE_2025})
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
    assert (form.ruleset, form.positions, form.replay_stopped, form.late) == (RULESET, POSITIONS, None, ())


def test_a_boxs_totals_sum_all_its_rows_t510() -> None:
    # two sales in box H: s1 takes a 500 ($5.00) for $6.00; s2 takes a's other 500 ($5.00) and b 1,000
    # ($20.00) for $36.00 (split $12.00 / $24.00)
    events: list[Event] = [
        Acquisition("a", "x", date(2025, 1, 1), "buy", 1_000, D("10.00")),
        Acquisition("b", "x", date(2025, 1, 2), "buy", 1_000, D("20.00")),
        sell("s1", date(2025, 6, 1), 500, "6.00"),
        sell("s2", date(2025, 7, 1), 1_500, "36.00"),
    ]
    form = report(events, {"s1": EXCHANGE_2025, "s2": EXCHANGE_2025})
    assert form.totals == (Total("H", 3, D("42.00"), D("30.00"), D("12.00")),)


def test_a_spend_from_a_wallet_goes_in_i_t510() -> None:
    # 2025 i8949: an on-chain disposal no 1099-DA reports, I (short-term)
    events: list[Event] = [
        Acquisition("a", "w", date(2025, 1, 1), "buy", 1_000, D("20.00")),
        sell("s", date(2025, 7, 1), 1_000, "18.00", account="w", kind="spend"),
    ]
    assert report(events, {"s": Sale.default("onchain", 2025)}).totals == (
        Total("I", 1, D("18.00"), D("20.00"), D("-2.00")),
    )


@pytest.mark.parametrize(
    ("year", "reported", "basis", "short", "long"),
    [
        (2025, False, False, "I", "L"),  # 2025 i8949: not on a 1099-DA
        (2026, True, True, "G", "J"),  # covered coins, basis reported
        (2025, True, False, "H", "K"),
        (2024, False, False, "C", "F"),  # 2024 i8949: no 1099-B
        (2024, True, True, "A", "D"),
        (2024, True, False, "B", "E"),
        (2013, False, False, "C", "F"),
    ],
)
def test_the_box_follows_the_year_and_the_forms_t510(
    year: int, reported: bool, basis: bool, short: str, long: str
) -> None:
    assert box(year, False, reported, basis) == short
    assert box(year, True, reported, basis) == long


def test_no_coin_is_covered_in_2025_t510() -> None:
    # ADR 0011: covered coins are bought in the exchange account from 2026-01-01
    with pytest.raises(EngineError, match="covered coins, bought from 2026"):
        box(2025, False, True, True)


def test_the_default_needs_a_year_with_rules() -> None:
    with pytest.raises(EngineError, match="tax year"):
        Sale.default("exchange", 2030)


def test_the_defaults_follow_adr_0011() -> None:
    assert Sale.default("onchain", 2025) == Sale("onchain", reported=False)
    assert Sale.default("exchange", 2025) == Sale("exchange", reported=True)
    assert Sale.default("exchange", 2024) == Sale("exchange", reported=False)


def test_only_the_tax_years_disposals_have_rows_and_gifts_have_none_adr0011() -> None:
    events: list[Event] = [
        BUYS[0],
        sell("s24", date(2024, 3, 1), 500, "20.00"),
        BUYS[1],
        Disposal("g", "x", date(2025, 3, 1), at(date(2025, 3, 1)), "gift_out", 500, D("0.00")),
        sell("s25", date(2025, 6, 1), 500, "30.00"),
    ]
    sales = {"s24": Sale.default("exchange", 2024), "s25": EXCHANGE_2025}
    assert [r.disposal for r in report(events, sales).rows] == ["s25"]
    assert [r.disposal for r in report(events, sales, 2024).rows] == ["s24"]


def test_an_override_replaces_the_box_and_records_the_reason_adr0011() -> None:
    sale = Sale("exchange", True, override_short="I", override_long="L", reason="no 1099-DA received")
    rows = report([*BUYS, sell("s", date(2025, 6, 1), 2_000, "100.00")], {"s": sale}).rows
    assert [(r.box, r.override_reason) for r in rows] == [
        ("I", "no 1099-DA received"),
        ("L", "no 1099-DA received"),
    ]


@pytest.mark.parametrize(
    ("sale", "year", "match"),
    [
        (Sale("exchange", True, override_long="H", reason="x"), 2025, "long-term rows"),
        (Sale("exchange", True, override_short="J", reason="x"), 2025, "short-term rows"),  # no short rows
        (
            Sale("exchange", True, override_long="F", reason="x"),
            2025,
            "isn't used for digital assets in 2025",
        ),
        (
            Sale("exchange", False, override_long="L", reason="x"),
            2024,
            "isn't used for digital assets in 2024",
        ),
        (Sale("exchange", True, override_long=["L"], reason="x"), 2025, "isn't one"),  # type: ignore[arg-type]
    ],
)
def test_a_bad_override_raises_adr0011(sale: Sale, year: int, match: str) -> None:
    sold = date(year, 6, 1)
    with pytest.raises(EngineError, match=match):
        form_8949(run([BUYS[0], sell("s", sold, 1_000, "50.00")]), {"s": sale}, year, blocking=())


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"override_long": "L"}, "the user's reason, and only then"),
        ({"override_long": "L", "reason": "  "}, "the user's reason, and only then"),
        ({"reason": "why"}, "the user's reason, and only then"),
        ({"channel": "wallet"}, "exchange or onchain"),
        ({"reported": "no"}, "True or False"),
        ({"basis_reported": 1}, "True or False"),
        ({"broker": D("1.00")}, "a Broker"),
        ({"reported": False, "broker": Broker(D("1.00"))}, "a sale a form reports"),
        ({"reported": False, "basis_reported": True}, "only on a form"),
        ({"reason": 5, "override_long": "L"}, "reason"),
    ],
)
def test_a_sales_facts_are_checked(kw: dict[str, Any], match: str) -> None:
    fields: dict[str, Any] = {"channel": "exchange", "reported": True} | kw
    with pytest.raises(EngineError, match=match):
        Sale(**fields)


def test_basis_without_a_form_raises() -> None:
    with pytest.raises(EngineError, match="only on a form"):
        box(2025, False, False, True)


@pytest.mark.parametrize("year", [2012, "2025", True])
def test_a_tax_year_needs_its_rule_set_even_without_rows(year: object) -> None:
    with pytest.raises(EngineError, match="tax year"):
        box(year, False, False, False)  # type: ignore[arg-type]
    with pytest.raises(EngineError, match="tax year"):
        form_8949(run([]), {}, year, blocking=())  # type: ignore[arg-type]


def test_a_disposal_without_its_sale_facts_raises() -> None:
    with pytest.raises(EngineError, match="channel and forms"):
        form_8949(run([BUYS[0], sell("s", date(2025, 6, 1), 1_000, "50.00")]), {}, 2025, blocking=())
    with pytest.raises(EngineError, match="its facts are a Sale"):
        form_8949(run([]), {"s": "exchange"}, 2025, blocking=())  # type: ignore[dict-item]


def test_a_blocking_condition_blocks_the_report_t509() -> None:
    result = run([BUYS[0], sell("s", date(2025, 6, 1), 2_000, "50.00")])  # 1,000 sats have no lots
    assert form_8949(result, {"s": EXCHANGE_2025}, 2025, blocking=()) == Blocked(result.blocking)


def test_the_callers_blocking_conditions_block_the_report_too_t506() -> None:
    # e.g. an unconfirmed transaction, or one tracing couldn't follow: only the caller knows of them
    result = run([BUYS[0], sell("s", date(2025, 6, 1), 1_000, "50.00")])
    assert form_8949(result, {"s": EXCHANGE_2025}, 2025, blocking=("unconfirmed tx",)) == Blocked(
        ("unconfirmed tx",)
    )


def test_a_late_choice_flags_its_rows_and_the_report_lists_the_warning_t508() -> None:
    late = sell(
        "s",
        date(2025, 6, 1),
        1_000,
        "50.00",
        picks=(Pick("new", 1_000),),
        identified_at=at(date(2025, 6, 1), 18),
    )
    form = report([*BUYS, late], {"s": EXCHANGE_2025})
    assert [(r.lot, r.late, r.late_replayed) for r in form.rows] == [("new", True, True)]
    (warning,) = form.late
    assert (warning.disposal, [a.lot for a in warning.standing]) == ("s", ["old"])  # FIFO's figures


def test_a_late_withdrawal_choice_is_listed_though_it_has_no_row_t508() -> None:
    late = at(date(2025, 5, 1), 18)
    events: list[Event] = [
        *BUYS,
        Transfer(
            "wd",
            "x",
            "w",
            date(2025, 5, 1),
            at(date(2025, 5, 1)),
            "withdrawal",
            1_000,
            picks=(Pick("new", 1_000),),
            identified_at=late,
        ),
        sell("s", date(2025, 6, 1), 1_000, "50.00", account="w"),
    ]
    form = report(events, {"s": EXCHANGE_2025})
    assert [w.disposal for w in form.late] == ["wd"]
    assert [(r.lot, r.late) for r in form.rows] == [("new@wd", False)]


def test_a_stopped_replay_is_shown_and_a_later_warning_says_it_used_the_users_lots_adr0040() -> None:
    # test_engine_replay's diverged history: the real run creates a lot in x for wd2's unrecorded sats,
    # the replay can't, and stops at wd2. The later late sale s2 is then judged on the user's lots.
    late = at(date(2025, 3, 10), 18)
    noon = at
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        Transfer(
            "wd0",
            "e",
            "w",
            date(2025, 2, 1),
            noon(date(2025, 2, 1)),
            "withdrawal",
            10,
            missing_basis=D("2.00"),
            missing_acquired=date(2024, 6, 1),
        ),
        Transfer(
            "t1",
            "w",
            "x",
            date(2025, 3, 1),
            noon(date(2025, 3, 1)),
            "deposit",
            10,
            picks=(Pick("wd0@@unrecorded@wd0", 10),),
            identified_at=late,
        ),
        sell("s1", date(2025, 3, 2), 10, "50.00"),
        Transfer(
            "wd2",
            "x",
            "y",
            date(2025, 3, 3),
            noon(date(2025, 3, 3)),
            "withdrawal",
            10,
            missing_basis=D("3.00"),
            missing_acquired=date(2024, 7, 1),
        ),
        Acquisition("b9", "w", date(2025, 3, 4), "buy", 10, D("9.00")),
        sell("s2", date(2025, 3, 4), 10, "5.00", account="w", picks=(Pick("b9", 10),), identified_at=late),
    ]
    form = report(events, {"s1": EXCHANGE_2025, "s2": EXCHANGE_2025})
    assert form.replay_stopped == "wd2"
    assert [(w.disposal, w.replayed) for w in form.late] == [("t1", True), ("s2", False)]
    assert [(r.disposal, r.late, r.late_replayed) for r in form.rows if r.disposal == "s2"] == [
        ("s2", True, False)
    ]


def test_an_inherited_lots_row_reads_inherited_t510() -> None:
    # Instructions for Form 8949, column (b): "INHERITED" instead of a date; always long-term
    events: list[Event] = [
        Acquisition("inh", "x", date(2025, 1, 1), "inherit", 1_000, D("10.00")),
        sell("s", date(2025, 2, 1), 1_000, "20.00"),
    ]
    (row,) = report(events, {"s": EXCHANGE_2025}).rows
    assert (row.box, row.inherited, row.long_term) == ("K", True, True)


def test_a_gift_lots_row_is_not_inherited_and_takes_its_basis_rule() -> None:
    # a gift (donor basis $20, FMV $8 at the gift) sold for $10, between the two: no gain or loss
    events: list[Event] = [
        Acquisition(
            "g",
            "x",
            date(2025, 1, 1),
            "gift_in",
            1_000,
            D("20.00"),
            D("8.00"),
            date(2020, 1, 1),
            donor_always_long=True,
        ),
        sell("s", date(2025, 2, 1), 1_000, "10.00"),
    ]
    (row,) = report(events, {"s": EXCHANGE_2025}).rows
    assert (row.basis, row.gain, row.inherited) == (D("10.00"), D("0.00"), False)


def test_rows_whose_figures_differ_from_the_brokers_are_flagged_adr0011() -> None:
    sale = Sale("exchange", True, True, broker=Broker(D("99.00"), D("50.00"), date(2023, 1, 10)))
    events: list[Event] = [BUYS[0], Acquisition("b2", "x", date(2024, 2, 1), "buy", 1_000, D("40.00"))]
    rows = report([*events, sell("s", date(2024, 6, 1), 2_000, "100.00")], {"s": sale}, 2024).rows
    # proceeds $100 vs $99; basis $50 matches; the rows' dates are two, the broker's one
    assert {r.review for r in rows} == {
        ("proceeds differ from the broker's", "acquisition date differs from the broker's")
    }


def test_rows_matching_the_brokers_figures_are_not_flagged_adr0011() -> None:
    sale = Sale("exchange", True, True, broker=Broker(D("50.00"), D("10.00"), date(2023, 1, 10)))
    (row,) = report([BUYS[0], sell("s", date(2024, 6, 1), 1_000, "50.00")], {"s": sale}, 2024).rows
    assert (row.box, row.review) == ("D", ())


def test_a_basis_difference_is_flagged_adr0011() -> None:
    sale = Sale("exchange", True, True, broker=Broker(D("50.00"), D("11.00")))
    (row,) = report([BUYS[0], sell("s", date(2024, 6, 1), 1_000, "50.00")], {"s": sale}, 2024).rows
    assert row.review == ("basis differs from the broker's: Form 8949 needs code B and the adjustment",)


@pytest.mark.parametrize(
    ("sats", "text"),
    [
        (1, "0.00000001 BTC"),
        (123_456_789, "1.23456789 BTC"),
        (100_000_000, "1.00000000 BTC"),
        (2_099_999_997_690_000, "20999999.97690000 BTC"),
    ],
)
def test_the_description_names_the_amount_in_btc(sats: int, text: str) -> None:
    assert describe(sats) == text


def test_only_covered_lots_get_the_basis_reported_box_t510() -> None:
    # A 2026 sale with basis reported, taking a 2025 lot (noncovered: H) and a 2026 lot (covered: G).
    # Treas. Reg. §1.6045-1(a)(15)(i)(J): covered from 2026-01-01, bought in that account (ADR 0011)
    events: list[Event] = [
        Acquisition("y25", "x", date(2025, 6, 1), "buy", 1_000, D("10.00")),
        Acquisition("y26", "x", date(2026, 1, 5), "buy", 1_000, D("20.00")),
        sell("s", date(2026, 3, 1), 2_000, "40.00"),
    ]
    rows = report(events, {"s": Sale("exchange", True, basis_reported=True)}, 2026).rows
    assert [(r.lot, r.box) for r in rows] == [("y26", "G"), ("y25", "H")]


def test_a_lot_moved_in_or_inherited_is_never_covered_t510() -> None:
    events: list[Event] = [
        Acquisition("w1", "w", date(2026, 1, 5), "buy", 1_000, D("10.00")),
        Transfer("d", "w", "x", date(2026, 1, 6), at(date(2026, 1, 6)), "deposit", 1_000),
        Acquisition("inh", "x", date(2026, 1, 7), "inherit", 1_000, D("10.00")),
        sell("s", date(2026, 3, 1), 2_000, "40.00"),
    ]
    rows = report(events, {"s": Sale("exchange", True, basis_reported=True)}, 2026).rows
    assert {(r.lot, r.box) for r in rows} == {("w1@d", "H"), ("inh", "K")}


def test_an_onchain_sale_a_1099_da_reports_takes_the_reported_box_adr0043() -> None:
    # ADR 0043: a sale sent straight from an exchange that a 1099-DA reports anyway takes H/K
    events: list[Event] = [BUYS[0], sell("s", date(2025, 6, 1), 1_000, "50.00")]
    assert [r.box for r in report(events, {"s": Sale("onchain", reported=True)}).rows] == ["K"]


def test_a_1099_b_with_basis_puts_a_2024_sale_in_a_or_d_t510() -> None:
    # 2024 i8949: box A/D for sales a 1099-B reports with basis (no covered-coin test before 1099-DAs)
    events: list[Event] = [BUYS[0], sell("s", date(2024, 6, 1), 1_000, "50.00")]
    assert [r.box for r in report(events, {"s": Sale("exchange", True, basis_reported=True)}, 2024).rows] == [
        "D"
    ]


@pytest.mark.parametrize("kind", ["income", "p2p_buy"])
def test_only_a_buy_in_the_exchange_account_is_covered_t510(kind: str) -> None:
    # Treas. Reg. §1.6045-1(a)(15)(i)(J): coins bought in that account; income or a peer-to-peer
    # purchase received into it isn't covered
    events: list[Event] = [
        Acquisition("a", "x", date(2026, 1, 5), kind, 1_000, D("10.00")),  # type: ignore[arg-type]
        sell("s", date(2026, 3, 1), 1_000, "20.00"),
    ]
    assert [r.box for r in report(events, {"s": Sale("exchange", True, basis_reported=True)}, 2026).rows] == [
        "H"
    ]


def test_a_gift_lot_is_never_covered_t510() -> None:
    events: list[Event] = [
        Acquisition("g", "x", date(2026, 1, 5), "gift_in", 1_000, D("10.00"), D("12.00"), date(2026, 1, 2)),
        sell("s", date(2026, 3, 1), 1_000, "20.00"),
    ]  # the donor's 2026 date passes the date test: only the bought gate keeps it out of G
    assert [r.box for r in report(events, {"s": Sale("exchange", True, basis_reported=True)}, 2026).rows] == [
        "H"
    ]


def test_another_years_override_is_checked_in_its_own_years_report_only() -> None:
    events: list[Event] = [
        BUYS[0],
        sell("s24", date(2024, 3, 1), 500, "20.00"),
        sell("s25", date(2025, 6, 1), 500, "30.00"),
    ]
    sales = {"s24": Sale("exchange", False, override_long="F", reason="no 1099-B"), "s25": EXCHANGE_2025}
    assert [r.box for r in report(events, sales, 2025).rows] == ["K"]
    assert [r.box for r in report(events, sales, 2024).rows] == ["F"]


@pytest.mark.parametrize(
    "broker",
    [
        {"proceeds": 1.5},
        {"proceeds": "1.50"},
        {"proceeds": D("1.505")},
        {"proceeds": D("1.00"), "basis": D("-1.00")},
        {"proceeds": D("1.00"), "acquired": datetime(2025, 1, 1, tzinfo=UTC)},
    ],
)
def test_broker_figures_are_checked(broker: dict[str, Any]) -> None:
    with pytest.raises(EngineError, match="broker's"):
        Broker(**broker)


@pytest.mark.parametrize("year", [2027, 9999])
def test_a_year_past_the_checked_rules_is_refused(year: int) -> None:
    with pytest.raises(EngineError, match="2013 to 2026"):
        box(year, False, False, False)


@pytest.mark.parametrize("blocking", ["x", iter(())])
def test_blocking_is_a_list_or_tuple(blocking: Any) -> None:
    with pytest.raises(EngineError, match="list or tuple"):
        form_8949(run([]), {}, 2025, blocking=blocking)


def test_a_fee_the_broker_lists_as_a_sale_is_listed_for_review_adr0043() -> None:
    fee = BrokerFee("0.00000100 BTC (fee)", date(2025, 6, 1), D("0.10"))
    old = BrokerFee("fee", date(2024, 6, 1), D("0.10"))
    form = form_8949(run([]), {}, 2025, blocking=(), broker_fees=[fee, old])
    assert isinstance(form, Form8949) and form.broker_fees == (fee,)
    with pytest.raises(EngineError, match="BrokerFees"):
        form_8949(run([]), {}, 2025, blocking=(), broker_fees=["fee"])  # type: ignore[list-item]
    with pytest.raises(EngineError, match="broker's"):
        BrokerFee("fee", date(2025, 1, 1), 0.1)  # type: ignore[arg-type]


def test_the_fee_position_prints_all_five_of_adr_0043s_bullets() -> None:
    text = " ".join(p for p in POSITIONS if p.startswith("ADR 0043"))
    for words in (
        "sales and spends",
        "buys",
        "transfers, deposits and withdrawals",
        "gifts",
        "brokers",
        "§1.1001-7(b)(2)",
    ):
        assert words in text


def test_a_reported_basis_on_uncovered_coins_is_flagged_t510() -> None:
    # a 2025 1099-DA can't report basis (no coin is covered before 2026): the rows are H/K, flagged
    events: list[Event] = [BUYS[0], sell("s", date(2025, 6, 1), 1_000, "50.00")]
    (row,) = report(events, {"s": Sale("exchange", True, basis_reported=True)}).rows
    assert row.box == "K"
    assert row.review == ("the form reports basis, but these coins aren't covered: check the broker's box",)


def test_broker_fees_are_a_list_or_tuple() -> None:
    fee = BrokerFee("fee", date(2025, 6, 1), D("0.10"))
    with pytest.raises(EngineError, match="list or tuple"):
        form_8949(run([]), {}, 2025, blocking=(), broker_fees=iter([fee]))  # type: ignore[arg-type]


def test_a_broker_basis_needs_a_form_that_reports_basis() -> None:
    with pytest.raises(EngineError, match="reports basis"):
        Sale("exchange", True, broker=Broker(D("1.00"), D("1.00")))

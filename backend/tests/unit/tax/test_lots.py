"""The lot engine: acquisitions, disposals per account, identification, holding periods, exact splits
(PLAN §7; ADRs 0008, 0009, 0021; T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.tax.lots import (
    Acquisition,
    Allocation,
    Disposal,
    EngineError,
    Event,
    LateIdentification,
    Lot,
    MissingLots,
    Pick,
    long_term,
    run,
)

BTC = 100_000_000
D = Decimal


def buy(id: str, on: date, sats: int, basis: str, **kw: str) -> Acquisition:
    """A lot; `account` defaults to "exch" and `kind` to "buy"."""
    return Acquisition(id, kw.get("account", "exch"), on, kw.get("kind", "buy"), sats, D(basis))  # type: ignore[arg-type]


def sell(id: str, on: date, sats: int, proceeds: str, account: str = "exch", **kw: object) -> Disposal:
    return Disposal(id, account, on, "sell", sats, D(proceeds), **kw)  # type: ignore[arg-type]


TWO_BUYS: list[Event] = [
    buy("b1", date(2024, 1, 10), BTC, "10000.00"),
    buy("b2", date(2024, 6, 1), BTC // 2, "15000.00"),
]


def test_fifo_uses_the_oldest_lot_first_and_splits_basis_and_proceeds_by_sats() -> None:
    result = run([*TWO_BUYS, sell("s1", date(2025, 3, 1), 120_000_000, "60000.00")])

    assert result.allocations == (
        # 100M of the 120M sold: 5/6 of the proceeds; all of b1's basis; held 14 months.
        Allocation("s1", "b1", BTC, D("10000.00"), D("50000.00"), date(2024, 1, 10), date(2025, 3, 1), True),
        # 20M of b2's 50M: 2/5 of its basis; the rest of the proceeds; held 9 months.
        Allocation(
            "s1", "b2", 20_000_000, D("6000.00"), D("10000.00"), date(2024, 6, 1), date(2025, 3, 1), False
        ),
    )
    assert [a.gain for a in result.allocations] == [D("40000.00"), D("4000.00")]
    assert result.holdings == (Lot("b2", "exch", date(2024, 6, 1), 30_000_000, D("9000.00"), False),)
    assert result.late == () and result.blocking == ()


def test_splits_take_a_share_of_what_remains_so_no_cent_is_lost() -> None:
    # A lot of 3 sats with $0.10 basis, sold one sat at a time: 0.0333 -> 0.03, 0.035 -> 0.04 (half
    # to even), then the remaining 0.03. Proportional rounding of each would give 0.03 * 3 = 0.09.
    events: list[Event] = [buy("b", date(2024, 1, 1), 3, "0.10")] + [
        sell(f"s{i}", date(2024, 2, i), 1, "1.00") for i in (1, 2, 3)
    ]
    result = run(events)
    assert [a.basis for a in result.allocations] == [D("0.03"), D("0.04"), D("0.03")]
    assert result.holdings == ()


def test_proceeds_split_across_lots_add_up_to_the_disposal() -> None:
    events: list[Event] = [buy(f"b{i}", date(2024, 1, i), 1, "1.00") for i in (1, 2, 3)]
    result = run([*events, sell("s", date(2024, 2, 1), 3, "0.10")])
    assert [a.proceeds for a in result.allocations] == [D("0.03"), D("0.04"), D("0.03")]


@pytest.mark.parametrize(
    ("acquired", "last_short", "first_long"),
    [
        (date(2024, 3, 15), date(2025, 3, 15), date(2025, 3, 16)),
        (date(2024, 2, 29), date(2025, 2, 28), date(2025, 3, 1)),  # leap day: anniversary 28 Feb
        (date(2023, 2, 28), date(2024, 2, 29), date(2024, 3, 1)),  # last day of Feb: Rev. Rul. 66-6
        (date(2024, 4, 30), date(2025, 4, 30), date(2025, 5, 1)),
        (date(2024, 1, 31), date(2025, 1, 31), date(2025, 2, 1)),
        (date(2024, 12, 31), date(2025, 12, 31), date(2026, 1, 1)),
    ],
)
def test_long_term_means_held_more_than_one_year(acquired: date, last_short: date, first_long: date) -> None:
    assert not long_term(acquired, last_short)
    assert long_term(acquired, first_long)


def test_the_engine_applies_the_holding_period_at_the_boundary() -> None:
    events: list[Event] = [
        buy("b", date(2024, 3, 15), 2, "2.00"),
        sell("s1", date(2025, 3, 15), 1, "5.00"),
        sell("s2", date(2025, 3, 16), 1, "5.00"),
    ]
    assert [a.long_term for a in run(events).allocations] == [False, True]


def test_an_inherited_lot_is_long_term_the_next_day() -> None:
    events: list[Event] = [
        buy("i", date(2025, 5, 1), BTC, "90000.00", kind="inherit"),
        sell("s", date(2025, 5, 2), BTC, "91000.00"),
    ]
    (a,) = run(events).allocations
    assert a.long_term and a.gain == D("1000.00")


def test_income_and_p2p_buys_create_lots_like_buys() -> None:
    events: list[Event] = [
        buy("w", date(2024, 1, 1), 10, "5.00", kind="income"),
        buy("p", date(2024, 1, 2), 10, "6.00", kind="p2p_buy"),
        sell("s", date(2024, 1, 3), 20, "20.00"),
    ]
    assert [(a.lot, a.basis) for a in run(events).allocations] == [("w", D("5.00")), ("p", D("6.00"))]


def test_a_disposal_uses_only_its_own_accounts_lots_adr0008() -> None:
    events: list[Event] = [
        buy("b", date(2024, 1, 1), BTC, "100.00", account="wallet"),
        sell("s", date(2024, 2, 1), BTC, "200.00", account="exch"),
    ]
    result = run(events)
    assert result.allocations == ()
    assert result.blocking == (MissingLots("s", BTC, D("200.00")),)
    assert result.holdings[0].account == "wallet" and result.holdings[0].sats == BTC


def test_a_shortfall_uses_what_exists_and_blocks_on_the_rest_adr0009() -> None:
    events: list[Event] = [
        buy("b", date(2024, 1, 1), 50_000_000, "1000.00"),
        sell("s", date(2024, 2, 1), 80_000_000, "8000.00"),
    ]
    result = run(events)
    assert [(a.sats, a.basis, a.proceeds) for a in result.allocations] == [
        (50_000_000, D("1000.00"), D("5000.00"))
    ]
    assert result.blocking == (MissingLots("s", 30_000_000, D("3000.00")),)
    assert result.holdings == ()


def test_a_choice_made_on_time_is_used_and_not_late() -> None:
    choice = (Pick("b2", BTC // 2),)
    result = run(
        [
            *TWO_BUYS,
            sell("s", date(2025, 3, 1), BTC // 2, "30000.00", picks=choice, identified_on=date(2025, 3, 1)),
        ]
    )
    assert [(a.lot, a.basis) for a in result.allocations] == [("b2", D("15000.00"))]
    assert result.late == ()


def test_a_late_choice_that_differs_from_fifo_is_used_and_warned_adr0021() -> None:
    choice = (Pick("b2", BTC // 2),)
    result = run(
        [
            *TWO_BUYS,
            sell("s", date(2025, 3, 1), BTC // 2, "30000.00", picks=choice, identified_on=date(2025, 3, 2)),
        ]
    )
    assert [a.lot for a in result.allocations] == ["b2"]  # the user's choice, not FIFO's
    assert result.late == (LateIdentification("s", date(2025, 3, 2), (Pick("b1", BTC // 2),)),)


def test_a_late_choice_that_matches_fifo_is_not_warned_adr0021() -> None:
    choice = (Pick("b1", 30_000_000), Pick("b1", 20_000_000))  # FIFO's lots, split in two picks
    result = run(
        [
            *TWO_BUYS,
            sell("s", date(2025, 3, 1), BTC // 2, "30000.00", picks=choice, identified_on=date(2026, 1, 1)),
        ]
    )
    assert result.late == ()
    assert sum(a.basis for a in result.allocations) == D("5000.00")


def test_a_later_disposal_sees_what_an_earlier_choice_left() -> None:
    result = run(
        [
            *TWO_BUYS,
            sell(
                "s1",
                date(2025, 3, 1),
                BTC // 2,
                "1.00",
                picks=(Pick("b2", BTC // 2),),
                identified_on=date(2025, 3, 1),
            ),
            sell("s2", date(2025, 3, 2), BTC, "2.00"),
        ]
    )
    assert [(a.disposal, a.lot, a.sats) for a in result.allocations] == [
        ("s1", "b2", BTC // 2),
        ("s2", "b1", BTC),
    ]
    assert result.holdings == () and result.blocking == ()


@pytest.mark.parametrize(
    ("disposal", "message"),
    [
        (sell("s", date(2025, 1, 1), 10, "1.00", picks=(Pick("b2", 10),)), "date it was chosen"),
        (sell("s", date(2025, 1, 1), 10, "1.00", identified_on=date(2025, 1, 1)), "no identification date"),
        (
            sell(
                "s", date(2025, 1, 1), 10, "1.00", picks=(Pick("nope", 10),), identified_on=date(2025, 1, 1)
            ),
            "isn't held",
        ),
        (
            sell("s", date(2025, 1, 1), 10, "1.00", picks=(Pick("w", 10),), identified_on=date(2025, 1, 1)),
            "isn't held",
        ),
        (
            sell(
                "s", date(2025, 1, 1), BTC, "1.00", picks=(Pick("b2", BTC),), identified_on=date(2025, 1, 1)
            ),
            "fewer sats",
        ),
        (
            sell("s", date(2025, 1, 1), 10, "1.00", picks=(Pick("b2", 9),), identified_on=date(2025, 1, 1)),
            "add up",
        ),
        (
            sell("s", date(2025, 1, 1), 10, "1.00", picks=(Pick("b2", 0),), identified_on=date(2025, 1, 1)),
            "positive",
        ),
    ],
)
def test_an_invalid_choice_of_lots_is_refused(disposal: Disposal, message: str) -> None:
    events: list[Event] = [*TWO_BUYS, buy("w", date(2024, 7, 1), 10, "1.00", account="wallet"), disposal]
    with pytest.raises(EngineError, match=message):
        run(events)


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([buy("b", date(2024, 2, 1), 1, "1.00"), buy("c", date(2024, 1, 1), 1, "1.00")], "out of date order"),
        ([buy("b", date(2024, 1, 1), 1, "1.00"), buy("b", date(2024, 1, 2), 1, "1.00")], "twice"),
        ([buy("b", date(2024, 1, 1), 0, "1.00")], "positive"),
        ([buy("b", date(2024, 1, 1), True, "1.00")], "positive"),  # a bool is not a count of sats
        ([buy("b", date(2024, 1, 1), 21_000_000 * BTC + 1, "1.00")], "supply"),
        ([buy("b", date(2024, 1, 1), 1, "1.001")], "two fractional digits"),
        ([buy("b", date(2024, 1, 1), 1, "-1.00")], "negative"),
        ([Acquisition("b", "exch", date(2024, 1, 1), "buy", 1, 1.5)], "finite Decimal"),  # type: ignore[arg-type]
        ([buy("b", date(2024, 1, 1), 1, "1.00", kind="gift_in")], "unknown kind"),
        ([Disposal("s", "exch", date(2024, 1, 1), "gift_out", 1, D("1.00"))], "unknown kind"),  # type: ignore[arg-type]
        ([buy("", date(2024, 1, 1), 1, "1.00")], "names its id"),
        ([buy("b", date(2024, 1, 1), 1, "1.00", account="")], "names its id"),
        ([Acquisition("b", "exch", "2024-01-01", "buy", 1, D("1.00"))], "must be a date"),  # type: ignore[arg-type]
        (["not an event"], "Acquisition or a Disposal"),
    ],
)
def test_invalid_events_are_refused(events: list[object], message: str) -> None:
    with pytest.raises(EngineError, match=message):
        run(events)  # type: ignore[arg-type]


def test_an_unknown_standing_method_is_refused() -> None:
    with pytest.raises(EngineError, match="standing method"):
        run([], {"exch": "lifo"})  # type: ignore[dict-item]


def test_fifo_is_also_the_explicit_standing_method() -> None:
    events: list[Event] = [*TWO_BUYS, sell("s", date(2025, 3, 1), BTC, "1.00")]
    assert run(events, {"exch": "fifo"}) == run(events)


# Property tests: any valid history conserves sats, basis and proceeds to the cent (T-502).

_day = st.integers(min_value=0, max_value=1500)
_event = st.one_of(
    st.tuples(
        st.just("a"), st.sampled_from(["x", "y"]), _day, st.integers(1, 3 * BTC), st.integers(0, 10**9)
    ),
    st.tuples(
        st.just("d"), st.sampled_from(["x", "y"]), _day, st.integers(1, 3 * BTC), st.integers(0, 10**9)
    ),
)


def _history(raw: list[tuple[str, str, int, int, int]]) -> list[Acquisition | Disposal]:
    events: list[Acquisition | Disposal] = []
    for i, (kind, account, day, sats, cents) in enumerate(sorted(raw, key=lambda r: r[2])):
        on = date.fromordinal(date(2020, 1, 1).toordinal() + day)
        amount = D(cents).scaleb(-2)
        if kind == "a":
            events.append(Acquisition(f"e{i}", account, on, "buy", sats, amount))
        else:
            events.append(Disposal(f"e{i}", account, on, "sell", sats, amount))
    return events


@settings(max_examples=300)
@given(st.lists(_event, max_size=25))
def test_any_history_conserves_sats_basis_and_proceeds(raw: list[tuple[str, str, int, int, int]]) -> None:
    events = _history(raw)
    result = run(events)
    for account in ("x", "y"):
        bought = [e for e in events if isinstance(e, Acquisition) and e.account == account]
        used = [a for a in result.allocations if a.lot in {b.id for b in bought}]
        held = [h for h in result.holdings if h.account == account]
        assert sum(b.sats for b in bought) == sum(a.sats for a in used) + sum(h.sats for h in held)
        assert sum(b.basis for b in bought) == sum(a.basis for a in used) + sum(h.basis for h in held)
    for d in (e for e in events if isinstance(e, Disposal)):
        parts = [a for a in result.allocations if a.disposal == d.id]
        missing = [m for m in result.blocking if m.disposal == d.id]
        assert sum(a.sats for a in parts) + sum(m.sats for m in missing) == d.sats
        assert sum(a.proceeds for a in parts) + sum(m.proceeds for m in missing) == d.proceeds
        assert all(a.basis >= 0 and a.proceeds >= 0 for a in parts)
    values = [v for a in result.allocations for v in (a.basis, a.proceeds)] + [
        h.basis for h in result.holdings
    ]
    assert all(type(v) is Decimal and v == v.quantize(D("0.01")) for v in values)
    assert run(events) == result  # deterministic

"""The lot engine: acquisitions, disposals per account, identification, holding periods, exact splits
(PLAN §7; ADRs 0008, 0009, 0021; T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.tax.engine import (
    Acquisition,
    Allocation,
    Disposal,
    EngineError,
    Event,
    LateIdentification,
    Lot,
    MissingLots,
    Pick,
    Result,
    long_term,
    run,
)

BTC = 100_000_000
D = Decimal


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def buy(id: str, on: date, sats: int, basis: str, **kw: str) -> Acquisition:
    """A lot; `account` defaults to "exch" and `kind` to "buy"."""
    return Acquisition(id, kw.get("account", "exch"), on, kw.get("kind", "buy"), sats, D(basis))  # type: ignore[arg-type]


def sell(id: str, on: date, sats: int, proceeds: str, **kw: object) -> Disposal:
    """A sale at noon UTC on `on` unless `at` is given; `account` defaults to "exch"."""
    moment = kw.pop("at", at(on))
    account = kw.pop("account", "exch")
    return Disposal(id, account, on, moment, "sell", sats, D(proceeds), **kw)  # type: ignore[arg-type]


TWO_BUYS: list[Event] = [
    buy("b1", date(2024, 1, 10), BTC, "10000.00"),
    buy("b2", date(2024, 6, 1), BTC // 2, "15000.00"),
]
SALE_DAY = date(2025, 3, 1)


def test_fifo_uses_the_oldest_lot_first_and_splits_basis_and_proceeds_by_sats() -> None:
    result = run([*TWO_BUYS, sell("s1", SALE_DAY, 120_000_000, "60000.00")])

    assert result.allocations == (
        # 100M of the 120M sold: 5/6 of the proceeds; all of b1's basis; held 14 months.
        Allocation("s1", "b1", BTC, D("10000.00"), D("50000.00"), date(2024, 1, 10), SALE_DAY, True),
        # 20M of b2's 50M: 2/5 of its basis; the rest of the proceeds; held 9 months.
        Allocation("s1", "b2", 20_000_000, D("6000.00"), D("10000.00"), date(2024, 6, 1), SALE_DAY, False),
    )
    assert [a.gain for a in result.allocations] == [D("40000.00"), D("4000.00")]
    assert result.holdings == (Lot("b2", "exch", date(2024, 6, 1), 30_000_000, D("9000.00"), False),)
    assert result.late == () and result.blocking == ()


def test_a_loss_is_a_negative_gain() -> None:
    (a,) = run(
        [buy("b", date(2024, 1, 1), 10, "100.00"), sell("s", date(2024, 2, 1), 10, "40.00")]
    ).allocations
    assert a.gain == D("-60.00")


def test_splits_take_a_share_of_what_remains_so_no_cent_is_lost() -> None:
    # A lot of 3 sats with $0.10 basis, sold one sat at a time: 0.0333 -> 0.03, 0.035 -> 0.04 (half
    # to even), then the remaining 0.03. Proportional rounding of each would give 0.03 * 3 = 0.09.
    events: list[Event] = [buy("b", date(2024, 1, 1), 3, "0.10")]
    events += [sell(f"s{i}", date(2024, 2, i), 1, "1.00") for i in (1, 2, 3)]
    result = run(events)
    assert [a.basis for a in result.allocations] == [D("0.03"), D("0.04"), D("0.03")]
    assert result.holdings == ()


def test_proceeds_split_across_lots_add_up_to_the_disposal() -> None:
    events: list[Event] = [buy(f"b{i}", date(2024, 1, i), 1, "1.00") for i in (1, 2, 3)]
    result = run([*events, sell("s", date(2024, 2, 1), 3, "0.10")])
    assert [a.proceeds for a in result.allocations] == [D("0.03"), D("0.04"), D("0.03")]


def test_the_figures_dont_depend_on_the_callers_decimal_context_t502() -> None:
    # Under a 3-digit context, $1000.00 - $333.33 would round to $667, and $0.33 would appear from
    # nowhere. The engine runs in its own fixed context, and `gain` too.
    events: list[Event] = [buy("b", date(2024, 1, 1), 3, "1000.00")]
    events += [sell(f"s{i}", date(2024, 2, i), 1, "1234.56") for i in (1, 2, 3)]
    expected = run(events)
    with localcontext(Context(prec=3)):
        result = run(events)
        gains = [a.gain for a in result.allocations]
    assert result == expected
    assert [a.basis for a in result.allocations] == [D("333.33"), D("333.34"), D("333.33")]
    assert gains == [D("901.23"), D("901.22"), D("901.23")]


@pytest.mark.parametrize(
    ("acquired", "last_short", "first_long"),
    [
        (date(2024, 3, 15), date(2025, 3, 15), date(2025, 3, 16)),
        (date(2024, 2, 29), date(2025, 2, 28), date(2025, 3, 1)),  # leap day: anniversary 28 Feb
        (date(2023, 2, 28), date(2024, 2, 29), date(2024, 3, 1)),  # last day of Feb: Rev. Rul. 66-6
        (date(2024, 2, 28), date(2025, 2, 28), date(2025, 3, 1)),  # not the last day in a leap year
        (date(2024, 4, 30), date(2025, 4, 30), date(2025, 5, 1)),
        (date(2024, 1, 30), date(2025, 1, 30), date(2025, 1, 31)),
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
    assert result.holdings == (Lot("b", "wallet", date(2024, 1, 1), BTC, D("100.00"), False),)


def test_holdings_are_in_acquisition_order_across_accounts() -> None:
    events: list[Event] = [
        buy("x1", date(2024, 1, 1), 1, "1.00", account="x"),
        buy("y2", date(2024, 1, 2), 1, "1.00", account="y"),
        buy("x3", date(2024, 1, 3), 1, "1.00", account="x"),
    ]
    assert [h.id for h in run(events).holdings] == ["x1", "y2", "x3"]


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


def test_a_shortfall_across_several_lots_blocks_on_the_rest() -> None:
    events: list[Event] = [
        buy("b1", date(2024, 1, 1), 1, "1.00"),
        buy("b2", date(2024, 1, 2), 2, "2.00"),
        sell("s", date(2024, 2, 1), 6, "1.00"),
    ]
    result = run(events)
    # 1 of 6 sats: 0.1666 -> 0.17; 2 of the remaining 5: 0.332 -> 0.33; the 3 missing get 0.50.
    assert [(a.lot, a.proceeds) for a in result.allocations] == [("b1", D("0.17")), ("b2", D("0.33"))]
    assert result.blocking == (MissingLots("s", 3, D("0.50")),)


def test_a_choice_made_before_or_at_the_sale_is_used_and_not_late() -> None:
    for chosen in (at(SALE_DAY), at(date(2025, 2, 1))):
        result = run(
            [
                *TWO_BUYS,
                sell(
                    "s", SALE_DAY, BTC // 2, "30000.00", picks=(Pick("b2", BTC // 2),), identified_at=chosen
                ),
            ]
        )
        assert [(a.lot, a.basis) for a in result.allocations] == [("b2", D("15000.00"))]
        assert result.late == ()


def test_a_choice_made_later_on_the_sale_day_is_late_t508() -> None:
    # Treas. Reg. §1.1012-1(j): no later than the date *and time* of the sale.
    sale = sell(
        "s",
        SALE_DAY,
        BTC // 2,
        "30000.00",
        at=at(SALE_DAY, 9),
        picks=(Pick("b2", BTC // 2),),
        identified_at=at(SALE_DAY, 18),
    )
    result = run([*TWO_BUYS, sale])
    assert [a.lot for a in result.allocations] == ["b2"]
    assert [late.disposal for late in result.late] == ["s"]


def test_a_late_choice_that_differs_from_fifo_is_used_and_warned_with_fifos_figures_adr0021() -> None:
    chosen = at(date(2025, 3, 2))
    result = run(
        [
            *TWO_BUYS,
            sell("s", SALE_DAY, BTC // 2, "30000.00", picks=(Pick("b2", BTC // 2),), identified_at=chosen),
        ]
    )
    assert [a.lot for a in result.allocations] == ["b2"]  # the user's choice, not FIFO's
    # FIFO would have used half of b1: basis $5000.00, all the proceeds, held over a year (ADR 0008 §4).
    fifo = Allocation("s", "b1", BTC // 2, D("5000.00"), D("30000.00"), date(2024, 1, 10), SALE_DAY, True)
    assert result.late == (LateIdentification("s", chosen, (fifo,)),)
    assert result.holdings[0] == Lot("b1", "exch", date(2024, 1, 10), BTC, D("10000.00"), False)  # untouched


def test_a_late_choice_that_matches_fifo_is_not_warned_and_repeats_are_merged_adr0021() -> None:
    picks = (Pick("b1", 30_000_000), Pick("b1", 20_000_000))  # FIFO's lots, split in two picks
    result = run(
        [
            *TWO_BUYS,
            sell("s", SALE_DAY, BTC // 2, "30000.00", picks=picks, identified_at=at(date(2026, 1, 1))),
        ]
    )
    assert result.late == ()
    assert [(a.lot, a.sats, a.basis) for a in result.allocations] == [("b1", BTC // 2, D("5000.00"))]


def test_a_late_choice_of_fifos_lots_in_another_order_is_not_warned() -> None:
    picks = (Pick("b2", 20_000_000), Pick("b1", BTC))
    result = run(
        [
            *TWO_BUYS,
            sell("s", SALE_DAY, 120_000_000, "60000.00", picks=picks, identified_at=at(date(2026, 1, 1))),
        ]
    )
    assert result.late == ()
    assert [a.lot for a in result.allocations] == ["b2", "b1"]  # the user's order


def test_a_later_disposal_sees_what_an_earlier_choice_left() -> None:
    first = sell("s1", SALE_DAY, BTC // 2, "1.00", picks=(Pick("b2", BTC // 2),), identified_at=at(SALE_DAY))
    result = run([*TWO_BUYS, first, sell("s2", date(2025, 3, 2), BTC, "2.00")])
    assert [(a.disposal, a.lot, a.sats) for a in result.allocations] == [
        ("s1", "b2", BTC // 2),
        ("s2", "b1", BTC),
    ]
    assert result.holdings == () and result.blocking == ()


ON_TIME = at(date(2025, 1, 1))


@pytest.mark.parametrize(
    ("kw", "sats", "message"),
    [
        ({"picks": (Pick("b2", 10),)}, 10, "time it was chosen"),
        ({"identified_at": ON_TIME}, 10, "no identification time"),
        ({"picks": (Pick("nope", 10),), "identified_at": ON_TIME}, 10, "isn't held"),
        ({"picks": (Pick("w", 10),), "identified_at": ON_TIME}, 10, "isn't held"),
        ({"picks": (Pick("b2", BTC),), "identified_at": ON_TIME}, BTC, "fewer sats"),
        ({"picks": (Pick("b2", 9),), "identified_at": ON_TIME}, 10, "add up"),
        ({"picks": (Pick("b2", 0),), "identified_at": ON_TIME}, 10, "positive"),
        ({"picks": (("b2", 10),), "identified_at": ON_TIME}, 10, "tuple of Picks"),
        ({"picks": (Pick("b2", 10),), "identified_at": datetime(2025, 1, 1)}, 10, "timezone-aware UTC"),
        ({"picks": (Pick("b2", 10),), "identified_at": date(2025, 1, 1)}, 10, "timezone-aware UTC"),
        ({"at": datetime(2025, 1, 1, 12)}, 10, "timezone-aware UTC"),
        ({"at": datetime(2025, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5)))}, 10, "timezone-aware UTC"),
    ],
)
def test_an_invalid_disposal_is_refused(kw: dict[str, object], sats: int, message: str) -> None:
    events = [
        *TWO_BUYS,
        buy("w", date(2024, 7, 1), 10, "1.00", account="wallet"),
        sell("s", date(2025, 1, 1), sats, "1.00", **kw),
    ]
    with pytest.raises(EngineError, match=message):
        run(events)


# A disposal kind the engine doesn't know (movements come in a later slice).
WITHDRAWAL = Disposal("s", "exch", date(2024, 1, 1), at(date(2024, 1, 1)), "withdrawal", 1, D("1.00"))  # type: ignore[arg-type]


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
        ([buy("b", date(2024, 1, 1), 1, "1e100")], "quadrillion"),
        ([Acquisition("b", "exch", date(2024, 1, 1), "buy", 1, 1.5)], "finite Decimal"),  # type: ignore[arg-type]
        ([buy("b", date(2024, 1, 1), 1, "1.00", kind="opening_allocation_2025")], "unknown kind"),
        ([WITHDRAWAL], "unknown kind"),
        ([buy("", date(2024, 1, 1), 1, "1.00")], "names its id"),
        ([buy("b", date(2024, 1, 1), 1, "1.00", account="")], "names its id"),
        ([Acquisition("b", "exch", "2024-01-01", "buy", 1, D("1.00"))], "must be a date"),  # type: ignore[arg-type]
        ([Acquisition("b", "exch", at(date(2024, 1, 1)), "buy", 1, D("1.00"))], "must be a date"),
        (["not an event"], "an Acquisition, a Disposal or a Transfer"),
    ],
)
def test_invalid_events_are_refused(events: list[object], message: str) -> None:
    with pytest.raises(EngineError, match=message):
        run(events)  # type: ignore[arg-type]


def test_an_unknown_standing_method_is_refused() -> None:
    with pytest.raises(EngineError, match="standing method"):
        run([], {"exch": "lifo"})  # type: ignore[dict-item]


def test_fifo_is_also_the_explicit_standing_method() -> None:
    events = [*TWO_BUYS, sell("s", SALE_DAY, BTC, "1.00")]
    assert run(events, {"exch": "fifo"}) == run(events)


# Property tests: any valid history, with automatic disposals and user choices made before or after
# the sale, conserves sats, basis and proceeds to the cent (T-502), and is late exactly when ADR 0021
# says so.


def _fifo(held: list[Lot], sats: int) -> dict[str, int]:
    picks: dict[str, int] = {}
    for h in held:
        take = min(h.sats, sats)
        if take:
            picks[h.id] = take
            sats -= take
    return picks


def _choice(  # noqa: PLR0913 - the draw, the event's id, account and day, and both runs' lots
    data: st.DataObject, id: str, account: str, on: date, held: list[Lot], *, replayed: list[Lot]
) -> tuple[Disposal, bool, Disposal]:
    """A user's choice: newest lots first (so it often differs from FIFO), some partly, made up to two
    days before or after the sale; whether ADR 0021 makes it late, judged against the replay's lots
    (`replayed`, #238); and the event as the replay has it: by FIFO if late, or if it names lots the
    replay doesn't hold in full."""
    picks = [
        Pick(h.id, data.draw(st.integers(1, h.sats))) for h in reversed(held) if data.draw(st.booleans())
    ]
    picks = picks or [Pick(held[-1].id, held[-1].sats)]
    chosen = at(on) + timedelta(hours=data.draw(st.integers(-48, 48)))
    total = sum(p.sats for p in picks)
    late = chosen > at(on) and {p.lot: p.sats for p in picks} != _fifo(replayed, total)
    amount = D(data.draw(st.integers(0, 10**9))).scaleb(-2)
    real = Disposal(id, account, on, at(on), "sell", total, amount, tuple(picks), chosen)
    room = {h.id: h.sats for h in replayed}
    follows = chosen <= at(on) and all(room.get(p.lot, 0) >= p.sats for p in picks)
    replay = real if follows else Disposal(id, account, on, at(on), "sell", total, amount)
    return real, late, replay


def _assert_conserved(events: list[Event], result: Result) -> None:
    for account in ("x", "y"):
        bought = {e.id: e for e in events if isinstance(e, Acquisition) and e.account == account}
        used = [a for a in result.allocations if a.lot in bought]
        held = [h for h in result.holdings if h.account == account]
        bases = [b.basis for b in bought.values() if b.basis is not None]  # buys: always known
        assert len(bases) == len(bought)
        assert sum(b.sats for b in bought.values()) == sum(a.sats for a in used) + sum(h.sats for h in held)
        assert sum(bases) == sum(a.basis for a in used) + sum(h.basis for h in held)
    disposals = {e.id: e for e in events if isinstance(e, Disposal)}
    for d in disposals.values():
        parts = [a for a in result.allocations if a.disposal == d.id]
        missing = [m for m in result.blocking if isinstance(m, MissingLots) and m.disposal == d.id]
        assert sum(a.sats for a in parts) + sum(m.sats for m in missing) == d.sats
        assert sum(a.proceeds for a in parts) + sum(m.proceeds for m in missing) == d.proceeds
        assert all(a.basis >= 0 for a in parts)  # proceeds can be negative (ADR 0009)
    for late in result.late:
        figures = [a for a in late.standing if isinstance(a, Allocation)]  # sales only here: no gifts
        assert len(figures) == len(late.standing)
        assert sum(a.sats for a in figures) == disposals[late.disposal].sats
        assert sum(a.proceeds for a in figures) == disposals[late.disposal].proceeds
    values = [v for a in result.allocations for v in (a.basis, a.proceeds)] + [
        h.basis for h in result.holdings
    ]
    assert all(type(v) is Decimal and v == v.quantize(D("0.01")) for v in values)


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_any_history_conserves_sats_basis_and_proceeds(data: st.DataObject) -> None:
    events: list[Event] = []
    replay: list[Event] = []  # the same history with the late choices disregarded (#238)
    expect_late: set[str] = set()
    start = date(2020, 1, 1).toordinal()
    for i in range(data.draw(st.integers(0, 20))):
        account = data.draw(st.sampled_from(["x", "y"]))
        on = date.fromordinal(start + 3 * i)
        kind = data.draw(st.sampled_from(["buy", "auto", "choice"]))
        held = [h for h in run(events).holdings if h.account == account]
        if kind == "choice" and held:
            replayed = [h for h in run(replay).holdings if h.account == account]
            disposal, late, as_replayed = _choice(data, f"e{i}", account, on, held, replayed=replayed)
            events.append(disposal)
            replay.append(as_replayed)
            if late:
                expect_late.add(disposal.id)
            continue
        sats = data.draw(st.integers(1, 3 * BTC))
        amount = D(data.draw(st.integers(0, 10**9))).scaleb(-2)
        if kind == "buy":
            events.append(Acquisition(f"e{i}", account, on, "buy", sats, amount))
        else:
            events.append(Disposal(f"e{i}", account, on, at(on), "sell", sats, amount))
        replay.append(events[-1])
    result = run(events)
    _assert_conserved(events, result)
    assert {late.disposal for late in result.late} == expect_late
    # the warnings' figures are the replay's own allocations for that disposal (#238): no transfers
    # here, so the replay never stops
    replay_allocations = run(replay).allocations
    assert result.replay_stopped is None
    for warning in result.late:
        assert warning.replayed
        assert warning.standing == tuple(a for a in replay_allocations if a.disposal == warning.disposal)
    assert run(events) == result  # deterministic

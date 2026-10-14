"""The lot engine's transfers: deposits, withdrawals and self-transfers move lots between accounts, and
fees (PLAN §7; ADRs 0008, 0009, 0043; T-501, T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.tax.engine import (
    Acquisition,
    Disposal,
    EngineError,
    Event,
    LateIdentification,
    Lot,
    MissingLots,
    Moved,
    Pick,
    Transfer,
    UnknownBasis,
    run,
)

BTC = 100_000_000
D = Decimal
DAY = date(2025, 1, 1)


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def move(id: str, src: str, to: str, sats: int, **kw: Any) -> Transfer:
    """A transfer on DAY at noon UTC; `kind` defaults to a deposit."""
    on = kw.pop("on", DAY)
    return Transfer(id, src, to, on, kw.pop("at", at(on)), kw.pop("kind", "deposit"), sats, **kw)


BUYS: list[Event] = [
    Acquisition("b1", "w", date(2024, 1, 10), "buy", BTC, D("10000.00")),
    Acquisition("b2", "w", date(2024, 6, 1), "buy", BTC // 2, D("15000.00")),
]


def test_a_deposit_moves_lots_with_their_basis_and_dates_and_carries_the_fee_adr0043() -> None:
    # 1.2 BTC leave the wallet, 20,000 sats of it the network fee. FIFO takes all of b1 and 0.2 of b2; the
    # arriving sats take the lots first, so the fee is b2's last 20,000 sats. No fee is a disposal: b2's
    # moved part keeps the basis of all 0.2 BTC (6000.00) on the 19,980,000 sats that arrive.
    result = run([*BUYS, move("d", "w", "x", 120_000_000, fee_sats=20_000)])
    assert result.allocations == ()
    assert result.moves == (
        Moved("d", "b1", "b1@d", "x", BTC, D("10000.00"), date(2024, 1, 10)),
        Moved("d", "b2", "b2@d", "x", 20_000_000 - 20_000, D("6000.00"), date(2024, 6, 1), 20_000),
    )
    assert result.holdings == (
        Lot("b2", "w", date(2024, 6, 1), 30_000_000, D("9000.00"), False),
        Lot("b1@d", "x", date(2024, 1, 10), BTC, D("10000.00"), False),
        Lot("b2@d", "x", date(2024, 6, 1), 20_000_000 - 20_000, D("6000.00"), False),
    )


def test_an_exchange_withdrawal_fee_carries_its_basis_to_the_withdrawn_coins_adr0043() -> None:
    # ADR 0043: an exchange's BTC withdrawal fee is no disposal. The withdrawn sats take e first; the
    # fee's 50,000 sats are e's too, so their basis stays on e's moved part: 99,950,000 sats, 40000.00.
    events: list[Event] = [
        Acquisition("e", "x", date(2024, 1, 1), "buy", BTC, D("40000.00")),
        move("wd", "x", "w", BTC, kind="withdrawal", fee_sats=50_000),
    ]
    result = run(events)
    assert result.allocations == ()
    assert result.holdings == (Lot("e@wd", "w", date(2024, 1, 1), BTC - 50_000, D("40000.00"), False),)


def test_a_withdrawal_fee_is_never_a_disposal_adr0043() -> None:
    assert run([*BUYS, move("wd", "w", "x", 10, kind="withdrawal", fee_sats=1)]).allocations == ()


def test_moved_lots_keep_their_holding_period() -> None:
    events: list[Event] = [*BUYS, move("d", "w", "x", BTC)]
    sale = Disposal("s", "x", date(2025, 2, 1), at(date(2025, 2, 1)), "sell", BTC, D("90000.00"))
    (a,) = run([*events, sale]).allocations
    assert (a.lot, a.acquired, a.long_term, a.gain) == ("b1@d", date(2024, 1, 10), True, D("80000.00"))


def test_a_moved_lot_takes_its_place_in_fifo_by_acquisition_date() -> None:
    # The exchange already holds a lot bought 1 March 2024; the deposited lot was bought 10 January, so
    # FIFO on the exchange sells the deposited lot first (Treas. Reg. §1.1012-1(c)(1): earliest acquired).
    events: list[Event] = [
        BUYS[0],
        Acquisition("e", "x", date(2024, 3, 1), "buy", BTC, D("30000.00")),
        move("d", "w", "x", BTC),
        Disposal("s", "x", date(2025, 2, 1), at(date(2025, 2, 1)), "sell", BTC, D("90000.00")),
    ]
    (a,) = run(events).allocations
    assert a.lot == "b1@d"


def test_fifo_finds_a_lot_moved_in_ahead_of_its_cursor() -> None:
    # The exchange's first lot is used up (its FIFO cursor has moved past it); an older lot then
    # arrives at the front: FIFO must still take it first.
    events: list[Event] = [
        BUYS[0],
        Acquisition("e1", "x", date(2024, 3, 1), "buy", 10, D("1.00")),
        Acquisition("e2", "x", date(2024, 4, 1), "buy", 10, D("2.00")),
        Disposal("s1", "x", date(2024, 12, 1), at(date(2024, 12, 1)), "sell", 10, D("5.00")),
        # A second sale moves the cursor past e1 (now used up) to e2.
        Disposal("s2", "x", date(2024, 12, 2), at(date(2024, 12, 2)), "sell", 5, D("5.00")),
        move("d", "w", "x", 10),
        Disposal("s3", "x", date(2025, 2, 1), at(date(2025, 2, 1)), "sell", 10, D("5.00")),
    ]
    assert [(a.disposal, a.lot) for a in run(events).allocations] == [
        ("s1", "e1"),
        ("s2", "e2"),
        ("s3", "b1@d"),
    ]


def test_a_withdrawal_from_an_account_without_lots_uses_the_users_basis_and_date_adr0009() -> None:
    wd = move(
        "wd", "x", "w", BTC, kind="withdrawal", missing_basis=D("5000.00"), missing_acquired=date(2023, 5, 1)
    )
    result = run([wd])
    assert result.blocking == ()
    assert result.holdings == (Lot("wd@@unrecorded@wd", "w", date(2023, 5, 1), BTC, D("5000.00"), False),)


def test_a_withdrawal_without_lots_or_a_basis_is_unknown_basis_t509() -> None:
    result = run([move("wd", "x", "w", BTC, kind="withdrawal")])
    assert result.blocking == (UnknownBasis("wd@@unrecorded"),)
    (lot,) = result.holdings
    assert (lot.account, lot.unknown_basis, lot.acquired) == ("w", True, DAY)


def test_a_withdrawal_short_of_lots_in_an_account_with_lots_is_missing_basis_adr0009() -> None:
    # ADR 0009: a lot is created on withdrawal only when no lots are known for the account. With some
    # lots, a shortfall points to a missing buy: it blocks, and no basis can paper over it.
    events: list[Event] = [
        Acquisition("e", "x", date(2024, 1, 1), "buy", 30, D("3.00")),
        move("wd", "x", "w", 100, kind="withdrawal"),
    ]
    result = run(events)
    assert result.created == ()
    assert result.blocking == (MissingLots("wd", 70, D("0.00")),)
    assert [(m.lot, m.sats) for m in result.moves] == [("e", 30)]
    papered = move("wd", "x", "w", 100, kind="withdrawal", missing_basis=D("7.00"), missing_acquired=DAY)
    with pytest.raises(EngineError, match="missing basis"):
        run([events[0], papered])


def test_a_created_lot_is_listed_and_named_by_the_engine() -> None:
    wd = move(
        "wd", "x", "w", BTC, kind="withdrawal", missing_basis=D("5000.00"), missing_acquired=date(2023, 5, 1)
    )
    assert run([wd]).created == (Lot("wd@@unrecorded", "x", date(2023, 5, 1), BTC, D("5000.00"), False),)


def test_a_deposit_the_wallet_cant_cover_is_missing_basis() -> None:
    events: list[Event] = [BUYS[0], move("d", "w", "x", BTC + 10)]
    result = run(events)
    assert result.blocking == (MissingLots("d", 10, D("0.00")),)
    assert [m.sats for m in result.moves] == [BTC]


def test_a_late_withdrawal_choice_that_differs_from_fifo_is_warned_with_fifos_lots_adr0008() -> None:
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 10, D("3.00")),
        move("wd", "x", "w", 10, kind="withdrawal", picks=(Pick("e2", 10),), identified_at=at(DAY, 18)),
    ]
    result = run(events)
    assert [m.lot for m in result.moves] == ["e2"]  # the user's choice
    assert result.late == (
        LateIdentification(
            "wd", at(DAY, 18), (Moved("wd", "e1", "e1@wd", "w", 10, D("1.00"), date(2024, 1, 1)),)
        ),
    )


def test_a_fee_that_uses_up_whole_lots_carries_their_basis_to_the_oldest_moved_part_adr0043() -> None:
    # Three 1-sat lots, 2 sats of fee: one sat arrives, b1's (the oldest); the fee is the last sats, all
    # of b2 and b3, so their basis (2.00) goes to b1's moved part, which records them.
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    result = run([*events, move("d", "w", "x", 3, fee_sats=2)])
    assert result.moves == (Moved("d", "b1", "b1@d", "x", 1, D("3.00"), date(2024, 1, 1), 2, ("b2", "b3")),)
    assert result.holdings == (Lot("b1@d", "x", date(2024, 1, 1), 1, D("3.00"), False),)


def test_the_fee_takes_the_last_sats_in_fifo_order_adr0043() -> None:
    # Picks of 1 and 1000 sats, a fee of 1000: the one arriving sat is the older lot's, and the fee is all
    # of the newer one, whose 1000.00 of basis joins the older lot's moved part.
    events: list[Event] = [
        Acquisition("small", "w", date(2024, 1, 1), "buy", 1, D("1.00")),
        Acquisition("big", "w", date(2024, 1, 2), "buy", 1000, D("1000.00")),
        move("d", "w", "x", 1001, fee_sats=1000),
    ]
    (m,) = run(events).moves
    assert (m.lot, m.sats, m.basis, m.carried_fee, m.carried_from) == (
        "small",
        1,
        D("1001.00"),
        1000,
        ("big",),
    )


def test_a_gift_lot_moves_with_its_dual_basis() -> None:
    gift = Acquisition(
        "g", "w", date(2024, 3, 1), "gift_in", BTC, D("10000.00"), D("6000.00"), date(2020, 5, 1)
    )
    (lot,) = run([gift, move("d", "w", "x", BTC)]).holdings
    assert (lot.account, lot.basis, lot.loss_basis, lot.loss_from, lot.acquired) == (
        "x",
        D("10000.00"),
        D("6000.00"),
        date(2024, 3, 1),
        date(2020, 5, 1),
    )


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"kind": "swap"}, "unknown kind"),
        ({"fee_sats": BTC}, "below the sats"),
        ({"fee_sats": -1}, "below the sats"),
        ({"fee_sats": True}, "below the sats"),
        ({"missing_basis": D("1.00")}, "only a withdrawal"),
        (
            {"kind": "withdrawal", "missing_basis": D("1.00"), "missing_acquired": date(2026, 1, 1)},
            "from genesis",
        ),
    ],
)
def test_an_invalid_transfer_is_refused(kw: dict[str, Any], message: str) -> None:
    src = "x" if kw.get("kind") == "withdrawal" else "w"
    with pytest.raises(EngineError, match=message):
        run([*BUYS, move("t", src, "y", BTC, **kw)])


def test_a_transfer_moves_to_another_account() -> None:
    with pytest.raises(EngineError, match="another account"):
        run([*BUYS, move("t", "w", "w", BTC)])


# Property: any mix of buys, deposits, withdrawals and sales conserves sats (fees leave the books, their
# basis stays with the coins) and basis (T-502, ADR 0043).


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_any_mix_of_transfers_conserves_sats_and_basis(data: st.DataObject) -> None:
    events: list[Event] = []
    start = date(2022, 1, 1).toordinal()
    for i in range(data.draw(st.integers(0, 16))):
        on = date.fromordinal(start + 3 * i)
        sats = data.draw(st.integers(2, 2 * BTC))
        cents = D(data.draw(st.integers(0, 10**8))).scaleb(-2)
        kind = data.draw(st.sampled_from(["buy", "deposit", "withdrawal", "sell"]))
        src, dst = data.draw(st.sampled_from([("w", "x"), ("x", "w")]))
        if kind == "buy":
            events.append(Acquisition(f"e{i}", src, on, "buy", sats, cents))
        elif kind == "sell":
            events.append(Disposal(f"e{i}", src, on, at(on), "sell", sats, cents))
        else:
            fee = data.draw(st.integers(0, sats - 1))
            events.append(move(f"e{i}", src, dst, sats, on=on, kind=kind, fee_sats=fee))
    result = run(events)
    bought = [e for e in events if isinstance(e, Acquisition)]
    created = sum(lot.sats for lot in result.created)  # lots created for withdrawals, ADR 0009
    sold = sum(a.sats for a in result.allocations)
    held = sum(h.sats for h in result.holdings)
    carried = sum(m.carried_fee for m in result.moves)  # carried fees leave the books, their basis stays
    assert sum(b.sats for b in bought) + created == sold + held + carried
    used = sum((a.lot_basis if a.lot_basis is not None else a.basis) for a in result.allocations)
    assert sum(b.basis or D("0.00") for b in bought) == used + sum(h.basis for h in result.holdings)
    assert all(m.sats > 0 for m in result.moves)
    # A created lot is used up whole by its own withdrawal: none stays behind as sats nobody had (T-509).
    assert not [h for h in result.holdings if h.id.endswith("@@unrecorded")]
    for lot in result.created:
        tid = lot.id.removesuffix("@@unrecorded")
        taken = sum(m.sats + m.carried_fee for m in result.moves if m.lot == lot.id and m.transfer == tid)
        taken += sum(a.sats for a in result.allocations if a.lot == lot.id and a.disposal == tid)
        assert taken == lot.sats
    sales = {e.id for e in events if isinstance(e, Disposal)}
    assert {a.disposal for a in result.allocations} <= sales  # a fee never makes an allocation (ADR 0043)
    assert run(events) == result  # deterministic


@pytest.mark.parametrize("bad", ["a@b", "x@@unrecorded", "@"])
def test_event_ids_cant_use_the_engines_separator(bad: str) -> None:
    with pytest.raises(EngineError, match="can't contain"):
        run([Acquisition(bad, "w", DAY, "buy", 1, D("1.00"))])


def test_outpoint_style_ids_are_allowed() -> None:
    # Callers may name events by outpoint (txid:vout): only "@" is the engine's.
    (lot,) = run([Acquisition("ab" * 32 + ":0", "w", DAY, "buy", 1, D("1.00"))]).holdings
    assert lot.id.endswith(":0")


@pytest.mark.parametrize("kw", [{"missing_basis": D("1.00")}, {"missing_acquired": date(2024, 1, 1)}])
def test_unrecorded_sats_need_both_a_basis_and_an_original_date(kw: dict[str, Any]) -> None:
    with pytest.raises(EngineError, match="both"):
        run([move("wd", "x", "w", 10, kind="withdrawal", **kw)])


def test_an_account_without_lots_has_none_to_choose() -> None:
    wd = move("wd", "x", "w", 10, kind="withdrawal", picks=(Pick("e", 10),), identified_at=at(DAY))
    with pytest.raises(EngineError, match="none to choose"):
        run([wd])


def test_a_late_withdrawals_warning_shows_fifos_fee_arriving_with_its_lot_adr0008() -> None:
    # FIFO would have taken e1 (10 sats, 1.00): 8 sats arrive with all of its basis, the 2-sat fee carried.
    late_choice = move(
        "wd", "x", "w", 10, kind="withdrawal", fee_sats=2, picks=(Pick("e2", 10),), identified_at=at(DAY, 18)
    )
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 10, D("3.00")),
        late_choice,
    ]
    (late,) = run(events).late
    assert late.standing == (Moved("wd", "e1", "e1@wd", "w", 8, D("1.00"), date(2024, 1, 1), 2),)


def test_a_moved_gift_keeps_its_fifo_place_by_the_day_it_was_received_241() -> None:
    # The exchange holds a buy from 1 January 2024; a gift received 1 March 2024 (donor's date 2020) is
    # deposited. FIFO goes by the day each lot reached the user, as in the wallet, so the buy is sold
    # first, not the gift with its tacked 2020 date.
    gift = Acquisition("g", "w", date(2024, 3, 1), "gift_in", 10, D("1.00"), D("2.00"), date(2020, 5, 1))
    events: list[Event] = [
        Acquisition("e", "x", date(2024, 1, 1), "buy", 10, D("5.00")),
        gift,
        move("d", "w", "x", 10, on=date(2024, 4, 1)),
        Disposal("s", "x", date(2024, 5, 1), at(date(2024, 5, 1)), "sell", 10, D("6.00")),
    ]
    (a,) = run(events).allocations
    assert a.lot == "e"


def test_a_short_deposits_known_sats_arrive_first_adr0043() -> None:
    # 10 sats leave, the wallet holds 5. The arriving sats take the lots first: with a 4-sat fee 6 sats
    # arrive, so all 5 known sats arrive and the fee falls on the missing sats; with an 8-sat fee 2
    # arrive and 3 of b's sats are fee (ADR 0043). The rest of the transfer is missing basis.
    b = Acquisition("b", "w", date(2024, 1, 1), "buy", 5, D("5.00"))
    for fee, arrive in ((4, 5), (8, 2)):
        result = run([b, move("d", "w", "x", 10, fee_sats=fee)])
        assert result.moves == (Moved("d", "b", "b@d", "x", arrive, D("5.00"), date(2024, 1, 1), 5 - arrive),)
        assert result.blocking == (MissingLots("d", 5, D("0.00")),)


def test_a_chosen_set_of_lots_leaves_no_sats_unrecorded() -> None:
    wd = move(
        "wd",
        "w",
        "x",
        10,
        kind="withdrawal",
        picks=(Pick("b1", 10),),
        identified_at=at(DAY),
        missing_basis=D("1.00"),
        missing_acquired=DAY,
    )
    with pytest.raises(EngineError, match="leaves no sats unrecorded"):
        run([*BUYS, wd])


def test_a_late_deposits_warning_shows_the_fee_basis_arriving() -> None:
    # FIFO would have taken b1 (10 sats, 1.00) with a 2-sat carried fee: 8 sats arrive with all 1.00.
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("b2", "w", date(2024, 2, 1), "buy", 10, D("3.00")),
        move("d", "w", "x", 10, fee_sats=2, picks=(Pick("b2", 10),), identified_at=at(DAY, 18)),
    ]
    (late,) = run(events).late
    assert late.standing == (Moved("d", "b1", "b1@d", "x", 8, D("1.00"), date(2024, 1, 1), 2),)


def test_a_late_alternative_carries_dust_basis_as_the_real_run_would() -> None:
    # FIFO would take three 1-sat lots (1.00 each) with a 2-sat fee: b2 and b3 are all fee, and their
    # basis goes to b1's sat, exactly as when the transfer itself runs.
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    events.append(Acquisition("b9", "w", date(2024, 2, 1), "buy", 3, D("9.00")))
    late_choice = move("d", "w", "x", 3, fee_sats=2, picks=(Pick("b9", 3),), identified_at=at(DAY, 18))
    (late,) = run([*events, late_choice]).late
    assert late.standing == (Moved("d", "b1", "b1@d", "x", 1, D("3.00"), date(2024, 1, 1), 2, ("b2", "b3")),)
    real = run([*events, move("d", "w", "x", 3, fee_sats=2)])  # FIFO itself: the same figures
    assert real.moves == late.standing


def test_a_fee_over_many_lots_takes_the_last_ones_adr0043() -> None:
    # 100 equal 10-sat lots, a 199-sat fee: 801 sats arrive, the first 80 lots in full and 1 sat of the
    # 81st (its other 9 are fee); the last 19 lots are all fee, and their basis joins the oldest part.
    events: list[Event] = [
        Acquisition(f"b{i:03}", "w", date(2024, 1, 1) + timedelta(days=i), "buy", 10, D("1.00"))
        for i in range(100)
    ]
    result = run([*events, move("d", "w", "x", 1000, fee_sats=199)])
    assert [m.carried_fee for m in result.moves] == [190] + [0] * 79 + [9]
    assert result.moves[0].basis == D("20.00") and len(result.moves[0].carried_from) == 19


def test_a_late_choice_of_fifos_lots_in_another_order_moves_the_same_sats() -> None:
    # Two 2-sat lots and a 1-sat fee: the fee falls on the same lot whichever order the
    # user names them in, so a late choice of FIFO's own lots needs no warning.
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 2, D("2.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 2, D("4.00")),
    ]
    fifo = run([*events, move("wd", "x", "w", 4, kind="withdrawal", fee_sats=1)])
    picked = move(
        "wd",
        "x",
        "w",
        4,
        kind="withdrawal",
        fee_sats=1,
        picks=(Pick("e2", 2), Pick("e1", 2)),
        identified_at=at(DAY, 18),
    )
    result = run([*events, picked])
    assert result.late == ()
    assert result.allocations == fifo.allocations and result.moves == fifo.moves


def test_a_second_withdrawal_from_an_account_without_recorded_buys_also_takes_a_basis_adr0009() -> None:
    first = move(
        "wd1", "x", "w", 10, kind="withdrawal", missing_basis=D("1.00"), missing_acquired=date(2023, 1, 1)
    )
    second = move(
        "wd2",
        "x",
        "w",
        20,
        kind="withdrawal",
        on=date(2025, 2, 1),
        missing_basis=D("3.00"),
        missing_acquired=date(2023, 6, 1),
    )
    result = run([first, second])
    assert [lot.id for lot in result.created] == ["wd1@@unrecorded", "wd2@@unrecorded"]
    assert result.blocking == ()


def test_a_fee_lot_of_a_gift_passes_its_lower_basis_adr0043() -> None:
    # The buy's sat arrives; the gift's sat is all fee, and the lower of its two bases (5.00) joins the
    # buy's moved part, the conservative choice
    events: list[Event] = [
        Acquisition("b", "w", date(2024, 1, 1), "buy", 1, D("1.00")),
        Acquisition("g", "w", date(2024, 1, 2), "gift_in", 1, D("10.00"), D("5.00"), date(2020, 1, 1)),
        move("d", "w", "x", 2, fee_sats=1),
    ]
    (lot,) = run(events).holdings
    assert (lot.id, lot.basis, lot.loss_basis) == ("b@d", D("6.00"), None)


def test_a_fee_joins_a_gifts_moved_part_on_both_its_bases_adr0043() -> None:
    # The gift's sat arrives (it is the older lot); the buy's sat is all fee, and its 1.00 joins the
    # gift's moved part on both bases: 11.00 and 6.00
    gift = Acquisition("g", "w", date(2024, 1, 1), "gift_in", 1, D("10.00"), D("5.00"), date(2020, 1, 1))
    events: list[Event] = [gift, Acquisition("b", "w", date(2024, 1, 2), "buy", 1, D("1.00"))]
    (lot,) = run([*events, move("d", "w", "x", 2, fee_sats=1)]).holdings
    assert (lot.id, lot.basis, lot.loss_basis) == ("g@d", D("11.00"), D("6.00"))


def test_a_withdrawal_never_creates_sats_beside_lots_the_account_holds_t509() -> None:
    # Exchange A has no lots: a withdrawal to exchange B creates a lot. B now holds those sats (not
    # recorded by the user, but held): a withdrawal from B uses them, and never creates more beside them.
    first = move(
        "wd1", "a", "b", 10, kind="withdrawal", missing_basis=D("1.00"), missing_acquired=date(2023, 1, 1)
    )
    second = move("wd2", "b", "w", 10, kind="withdrawal", on=date(2025, 2, 1))
    result = run([first, second])
    assert [lot.id for lot in result.created] == ["wd1@@unrecorded"]
    assert [(h.id, h.account, h.sats) for h in result.holdings] == [("wd1@@unrecorded@wd1@wd2", "w", 10)]
    papered = move(
        "wd2",
        "b",
        "w",
        10,
        kind="withdrawal",
        on=date(2025, 2, 1),
        missing_basis=D("3.00"),
        missing_acquired=date(2023, 6, 1),
    )
    with pytest.raises(EngineError, match="missing basis"):
        run([first, papered])


def test_a_created_lot_stays_unrecorded_however_far_it_moves() -> None:
    first = move(
        "wd1", "a", "b", 10, kind="withdrawal", missing_basis=D("1.00"), missing_acquired=date(2023, 1, 1)
    )
    # the hops' kind doesn't matter here (self-transfers are refused until UTXO tracing, #243)
    hop = move("t1", "b", "c", 10, kind="deposit", on=date(2025, 1, 2))
    away = move("t2", "c", "d", 10, kind="deposit", on=date(2025, 1, 3))
    # c and d only ever held the created lot, now gone on: a withdrawal from c can take a basis again.
    again = move(
        "wd2",
        "c",
        "w",
        5,
        kind="withdrawal",
        on=date(2025, 2, 1),
        missing_basis=D("2.00"),
        missing_acquired=date(2023, 6, 1),
    )
    assert [lot.id for lot in run([first, hop, away, again]).created] == [
        "wd1@@unrecorded",
        "wd2@@unrecorded",
    ]


def test_a_late_alternative_carries_only_a_partial_dust_parts_share() -> None:
    # b1 and b2 hold 10 sats each (1.00 each). FIFO moving 11 sats with a 10-sat fee picks b1 10, b2 1;
    # one sat arrives, b1's, so b1's other 9 and b2's sat are fee: b2's part carries 1/10 of its basis.
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("b2", "w", date(2024, 1, 2), "buy", 10, D("1.00")),
        Acquisition("b9", "w", date(2024, 2, 1), "buy", 11, D("9.00")),
    ]
    late_choice = move("d", "w", "x", 11, fee_sats=10, picks=(Pick("b9", 11),), identified_at=at(DAY, 18))
    (late,) = run([*events, late_choice]).late
    real = run([*events, move("d", "w", "x", 11, fee_sats=10)])
    assert late.standing == real.moves
    assert real.moves == (Moved("d", "b1", "b1@d", "x", 1, D("1.10"), date(2024, 1, 1), 10, ("b2",)),)


def test_dust_goes_to_the_oldest_moved_part_adr0043() -> None:
    # Lots of 1, 2 and 3 sats, a 4-sat fee (of 6): the 2 arriving sats are dust's and one of buy's; the
    # gift is all fee, and the lower of its bases (1.00) joins the oldest moved part, dust's.
    events: list[Event] = [
        Acquisition("dust", "w", date(2024, 1, 1), "buy", 1, D("0.50")),
        Acquisition("buy", "w", date(2024, 1, 2), "buy", 2, D("2.00")),
        Acquisition("g", "w", date(2024, 1, 3), "gift_in", 3, D("3.00"), D("1.00"), date(2020, 1, 1)),
    ]
    result = run([*events, move("d", "w", "x", 6, fee_sats=4)])
    assert [(m.lot, m.sats, m.basis, m.carried_fee, m.carried_from) for m in result.moves] == [
        ("dust", 1, D("1.50"), 3, ("g",)),
        ("buy", 1, D("2.00"), 1, ()),
    ]


def test_a_moved_inherited_lot_stays_long_term_irc1223() -> None:
    events: list[Event] = [
        Acquisition("i", "w", date(2025, 5, 1), "inherit", 10, D("10.00")),
        move("d", "w", "x", 10, on=date(2025, 5, 2)),
        Disposal("s", "x", date(2025, 5, 3), at(date(2025, 5, 3)), "sell", 10, D("12.00")),
    ]
    (a,) = run(events).allocations
    assert a.long_term


def test_a_late_alternative_moves_dust_basis_onto_a_gifts_part_adr0008() -> None:
    # FIFO would take a 1-sat gift and a 1-sat buy for a 2-sat deposit with a 1-sat fee: the gift's sat
    # arrives and the buy's is all fee, its 1.00 joining the gift's part. The user's later choice stands.
    events: list[Event] = [
        Acquisition("g", "w", date(2024, 1, 1), "gift_in", 1, D("10.00"), D("5.00"), date(2020, 1, 1)),
        Acquisition("b", "w", date(2024, 1, 2), "buy", 1, D("1.00")),
        Acquisition("later", "w", date(2024, 2, 1), "buy", 2, D("2.00")),
    ]
    choice = move("d", "w", "x", 2, fee_sats=1, picks=(Pick("later", 2),), identified_at=at(DAY, 18))
    result = run([*events, choice])
    assert [m.lot for m in result.moves] == ["later"]
    (late,) = result.late
    assert late.standing == (Moved("d", "g", "g@d", "x", 1, D("11.00"), date(2020, 1, 1), 1, ("b",)),)


@pytest.mark.parametrize("picks", [None, (Pick("b1", 5),)])
def test_a_self_transfer_comes_only_as_a_wallet_tx(picks: tuple[Pick, ...] | None) -> None:
    # #243, the owner's decision: between the user's own wallets the spent outputs identify the lots, so
    # the account's FIFO order (or a choice of lots) must not stand in for them (ADR 0041)
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        move("t1", "w", "v", 5, kind="self_transfer", picks=picks),
    ]
    with pytest.raises(
        EngineError, match=r"transfer 't1': a self-transfer's lots follow the outputs .* WalletTx"
    ):
        run(events)


EXCHANGE: list[Event] = [
    Acquisition("e1", "x", date(2024, 1, 1), "buy", 10, D("1.00")),
    Acquisition("e2", "x", date(2024, 2, 1), "buy", 10, D("3.00")),
]


def test_a_choice_of_only_the_arriving_sats_takes_the_fee_by_the_standing_method_adr0043() -> None:
    # 10 sats leave e's exchange, 2 of them the fee; the user names 8 of e2 (on time). The fee takes the
    # standing method's next sats besides those, FIFO: 2 of e1. All of e1's part is fee, so its 0.20 of
    # basis joins e2's moved part: 8 sats with 2.40 + 0.20.
    wd = move(
        "wd", "x", "w", 10, kind="withdrawal", fee_sats=2, picks=(Pick("e2", 8),), identified_at=at(DAY, 9)
    )
    result = run([*EXCHANGE, wd])
    assert result.moves == (Moved("wd", "e2", "e2@wd", "w", 8, D("2.60"), date(2024, 2, 1), 2, ("e1",)),)
    assert result.late == ()


def test_a_late_choice_of_only_the_arriving_sats_is_judged_with_its_fee_lots_adr0043() -> None:
    # Late, the same choice is judged as a whole (e2 8, e1 2) against FIFO's (e1 10): it differs, and the
    # warning shows FIFO's: 8 of e1 arriving with all of its 1.00
    wd = move(
        "wd", "x", "w", 10, kind="withdrawal", fee_sats=2, picks=(Pick("e2", 8),), identified_at=at(DAY, 18)
    )
    (late,) = run([*EXCHANGE, wd]).late
    assert late.standing == (Moved("wd", "e1", "e1@wd", "w", 8, D("1.00"), date(2024, 1, 1), 2),)


def test_a_choice_of_the_arriving_sats_needs_sats_left_for_the_fee() -> None:
    lone = Acquisition("e", "x", date(2024, 1, 1), "buy", 8, D("1.00"))
    wd = move(
        "wd", "x", "w", 10, kind="withdrawal", fee_sats=2, picks=(Pick("e", 8),), identified_at=at(DAY, 9)
    )
    with pytest.raises(EngineError, match="too few sats for the fee"):
        run([lone, wd])


def test_an_unknown_basis_fee_lot_passes_none_and_stays_blocking_adr0043() -> None:
    # The buy's sat arrives; the unknown gift's sat is all fee: it passes 0.00, and still blocks
    events: list[Event] = [
        Acquisition("b", "w", date(2024, 1, 1), "buy", 1, D("1.00")),
        Acquisition("g", "w", date(2024, 1, 2), "gift_in", 1, None, D("2.00"), date(2020, 1, 1)),
        move("d", "w", "x", 2, fee_sats=1),
    ]
    result = run(events)
    assert result.moves == (Moved("d", "b", "b@d", "x", 1, D("1.00"), date(2024, 1, 1), 1, ("g",)),)
    assert UnknownBasis("g") in result.blocking


def test_carried_basis_goes_to_the_oldest_part_even_of_unknown_basis_adr0043() -> None:
    # The unknown gift is the oldest moved part, so it takes the all-fee lot's 1.00 (ADR 0043's oldest
    # moved lot; it stays blocking)
    events: list[Event] = [
        Acquisition("g", "w", date(2024, 1, 1), "gift_in", 1, None, D("2.00"), date(2020, 1, 1)),
        Acquisition("b", "w", date(2024, 1, 2), "buy", 1, D("1.00")),
        Acquisition("c", "w", date(2024, 1, 3), "buy", 1, D("1.00")),
        move("d", "w", "x", 3, fee_sats=1),
    ]
    moved = {m.lot: (m.basis, m.carried_from) for m in run(events).moves}
    assert moved == {"g": (D("1.00"), ("c",)), "b": (D("1.00"), ())}


HALVES: list[Event] = [
    Acquisition("e1", "x", date(2024, 1, 1), "buy", 5, D("1.00")),
    Acquisition("e2", "x", date(2024, 2, 1), "buy", 5, D("3.00")),
]


def test_a_late_choice_with_fifos_lots_but_other_arriving_sats_is_late_adr0043() -> None:
    # Naming arriving e2 5 and e1 3 makes the fee e1's last 2 sats: the same lots as FIFO in all (e1 5,
    # e2 5), but FIFO has e1's 5 arrive and 2 of e2 pay the fee. Different sats arrive, so it is late,
    # and the warning shows FIFO's moves.
    wd = move(
        "wd",
        "x",
        "w",
        10,
        kind="withdrawal",
        fee_sats=2,
        picks=(Pick("e2", 5), Pick("e1", 3)),
        identified_at=at(DAY, 18),
    )
    result = run([*HALVES, wd])
    assert {m.lot: (m.sats, m.carried_fee) for m in result.moves} == {"e1": (3, 2), "e2": (5, 0)}
    (late,) = result.late
    assert late.standing == (
        Moved("wd", "e1", "e1@wd", "w", 5, D("1.00"), date(2024, 1, 1)),
        Moved("wd", "e2", "e2@wd", "w", 3, D("3.00"), date(2024, 2, 1), 2),
    )


def test_a_late_choice_naming_fifos_arriving_sats_is_not_late_adr0043() -> None:
    wd = move(
        "wd",
        "x",
        "w",
        10,
        kind="withdrawal",
        fee_sats=2,
        picks=(Pick("e1", 5), Pick("e2", 3)),
        identified_at=at(DAY, 18),
    )
    assert run([*HALVES, wd]).late == ()


def test_the_replay_follows_an_on_time_choices_fee_lots_or_falls_back_whole_adr0043() -> None:
    # A late sale takes 3 of e2, so the real run holds e1 5, e2 2, e3 5 and the replay (FIFO) e1 2, e2 5,
    # e3 5. The withdrawal names its arriving e3 5 on time; its 3-sat fee takes e1 3 in the real run.
    # The replay must follow that whole identification, and holds only 2 of e1, so it falls back, whole,
    # to FIFO: e1 2 and e2 3 arrive in w. A late sale of the user's e3@wd then differs from the replay's.
    d2, d3 = date(2025, 1, 2), date(2025, 1, 3)
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 5, D("1.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 5, D("2.00")),
        Acquisition("e3", "x", date(2024, 3, 1), "buy", 5, D("3.00")),
        Disposal(
            "s1", "x", DAY, at(DAY), "sell", 3, D("3.00"), picks=(Pick("e2", 3),), identified_at=at(DAY, 18)
        ),
        move(
            "wd",
            "x",
            "w",
            8,
            on=d2,
            kind="withdrawal",
            fee_sats=3,
            picks=(Pick("e3", 5),),
            identified_at=at(d2, 9),
        ),
        Disposal(
            "s2", "w", d3, at(d3), "sell", 5, D("9.00"), picks=(Pick("e3@wd", 5),), identified_at=at(d3, 18)
        ),
    ]
    result = run(events)
    assert result.replay_stopped is None
    late = {x.disposal: x for x in result.late}
    assert {a.lot: a.sats for a in late["s2"].standing} == {"e1@wd": 2, "e2@wd": 3}
    assert late["s2"].replayed


def test_the_fee_can_take_the_rest_of_a_lot_the_choice_names_adr0043() -> None:
    # Naming 8 of e1's 10 sats: the fee's FIFO sats besides them are e1's last 2, so e1 10 in all, as
    # FIFO's, with the same 8 arriving: not late, and the moved part keeps all of e1's 1.00
    wd = move(
        "wd", "x", "w", 10, kind="withdrawal", fee_sats=2, picks=(Pick("e1", 8),), identified_at=at(DAY, 18)
    )
    result = run([*EXCHANGE, wd])
    assert result.moves == (Moved("wd", "e1", "e1@wd", "w", 8, D("1.00"), date(2024, 1, 1), 2),)
    assert result.late == ()

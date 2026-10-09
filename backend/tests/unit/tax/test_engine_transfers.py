"""The lot engine's transfers: deposits, withdrawals and self-transfers move lots between accounts, and
fees by role (PLAN §7; ADRs 0008, 0009; T-501, T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from coinacct.tax.engine import (
    Acquisition,
    Allocation,
    Disposal,
    EngineError,
    Event,
    FeeTreatment,
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


def test_a_deposit_moves_lots_with_their_basis_and_dates_and_carries_the_fee_adr0009() -> None:
    # 1.2 BTC leave the wallet, 20,000 sats of it the network fee. FIFO takes all of b1 and 0.2 of b2.
    # The fee splits by sats: 16,666 and 3,333, the one sat left to b1. With the default treatment
    # the fee is no disposal: each lot's basis stays with the sats that arrive.
    result = run([*BUYS, move("d", "w", "x", 120_000_000, fee_sats=20_000)])
    assert result.allocations == ()
    assert result.moves == (
        Moved("d", "b1", "b1@d", "x", BTC - 16_667, D("10000.00"), date(2024, 1, 10), 16_667),
        Moved("d", "b2", "b2@d", "x", 20_000_000 - 3_333, D("6000.00"), date(2024, 6, 1), 3_333),
    )
    assert result.holdings == (
        Lot("b2", "w", date(2024, 6, 1), 30_000_000, D("9000.00"), False),
        Lot("b1@d", "x", date(2024, 1, 10), BTC - 16_667, D("10000.00"), False),
        Lot("b2@d", "x", date(2024, 6, 1), 20_000_000 - 3_333, D("6000.00"), False),
    )


def test_a_deposit_fee_can_be_a_small_disposal_by_setting_adr0009() -> None:
    # The same fee as a disposal at its $12.00 FMV: b1's 16,667 sats have basis 1.6667 -> 1.67 and
    # proceeds 12 * 16,667 / 20,000 = 10.0002 -> 10.00; b2's 3,333 sats have basis 0.9999 -> 1.00 and the
    # rest of the proceeds, 2.00. What arrives keeps the rest of the basis: 9998.33 and 5999.00.
    deposit = move("d", "w", "x", 120_000_000, fee_sats=20_000, fee_value=D("12.00"))
    result = run([*BUYS, deposit], fee_treatment="dispose")
    assert [(a.disposal, a.lot, a.sats, a.basis, a.proceeds) for a in result.allocations] == [
        ("d", "b1", 16_667, D("1.67"), D("10.00")),
        ("d", "b2", 3_333, D("1.00"), D("2.00")),
    ]
    assert [(m.lot, m.sats, m.basis) for m in result.moves] == [
        ("b1", BTC - 16_667, D("9998.33")),
        ("b2", 20_000_000 - 3_333, D("5999.00")),
    ]


def test_an_exchange_withdrawal_fee_is_always_a_small_disposal_adr0009() -> None:
    events: list[Event] = [
        Acquisition("e", "x", date(2024, 1, 1), "buy", BTC, D("40000.00")),
        move("wd", "x", "w", BTC, kind="withdrawal", fee_sats=50_000, fee_value=D("30.00")),
    ]
    result = run(events)  # the default "carry" is for network fees, never for this
    (fee,) = result.allocations
    # 50,000 of 100M sats: basis 40000 * 0.0005 = 20.00; proceeds 30.00; a short-term gain of 10.00.
    assert (fee.lot, fee.sats, fee.basis, fee.proceeds, fee.gain, fee.long_term) == (
        "e",
        50_000,
        D("20.00"),
        D("30.00"),
        D("10.00"),
        False,
    )
    assert result.holdings == (Lot("e@wd", "w", date(2024, 1, 1), BTC - 50_000, D("39980.00"), False),)


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


def test_a_fee_that_takes_whole_dust_parts_carries_their_basis_to_the_last_moved_part() -> None:
    # Three 1-sat lots, 2 sats of fee: the smallest parts (here all one sat; the first ones) pay it, so
    # b1 and b2 are all fee and their basis (2.00) goes to b3's sat, which records them.
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    result = run([*events, move("d", "w", "x", 3, fee_sats=2)])
    assert result.moves == (Moved("d", "b3", "b3@d", "x", 1, D("3.00"), date(2024, 1, 3), 2, ("b1", "b2")),)
    assert result.holdings == (Lot("b3@d", "x", date(2024, 1, 3), 1, D("3.00"), False),)


def test_leftover_fee_falls_on_the_smallest_part_not_a_large_one() -> None:
    # Picks of 1 and 1000 sats, a fee of 1000: the floor shares are 0 and 999; one sat is left, and
    # only the 1-sat part has room for it, so the 1000-sat part keeps a sat and its own basis.
    events: list[Event] = [
        Acquisition("small", "w", date(2024, 1, 1), "buy", 1, D("1.00")),
        Acquisition("big", "w", date(2024, 1, 2), "buy", 1000, D("1000.00")),
        move("d", "w", "x", 1001, fee_sats=1000),
    ]
    (m,) = run(events).moves
    assert (m.lot, m.sats, m.basis, m.carried_from) == ("big", 1, D("1001.00"), ("small",))


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
        ({"fee_value": D("1.00")}, "needs fee sats"),
        ({"fee_sats": 1, "fee_value": D("-1.00")}, "negative"),
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


def test_an_unknown_fee_treatment_is_refused() -> None:
    with pytest.raises(EngineError, match="fee treatment"):
        run([], fee_treatment="ignore")  # type: ignore[arg-type]


# Property: any mix of buys, deposits, withdrawals and sales conserves sats (fees leave as disposals or
# carried basis) and basis, under both fee treatments (T-502).


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_any_mix_of_transfers_conserves_sats_and_basis(data: st.DataObject) -> None:
    treatment: FeeTreatment = data.draw(st.sampled_from(["carry", "dispose"]))
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
            value = D(data.draw(st.integers(0, 10**4))).scaleb(-2) if fee else None
            events.append(move(f"e{i}", src, dst, sats, on=on, kind=kind, fee_sats=fee, fee_value=value))
    try:
        result = run(events, fee_treatment=treatment)
    except EngineError as e:  # the one documented refusal: dust of an unknown-basis lot under carry
        assume("can't carry the basis" not in str(e))
        raise
    bought = [e for e in events if isinstance(e, Acquisition)]
    created = sum(lot.sats for lot in result.created)  # lots created for withdrawals, ADR 0009
    sold = sum(a.sats for a in result.allocations)
    held = sum(h.sats for h in result.holdings)
    carried = sum(m.carried_fee for m in result.moves)  # carried fees leave the books, their basis stays
    assert sum(b.sats for b in bought) + created == sold + held + carried
    used = sum((a.lot_basis if a.lot_basis is not None else a.basis) for a in result.allocations)
    assert sum(b.basis or D("0.00") for b in bought) == used + sum(h.basis for h in result.holdings)
    if treatment == "dispose":
        assert carried == 0
    assert all(m.sats > 0 for m in result.moves)
    # A created lot is used up whole by its own withdrawal: none stays behind as sats nobody had (T-509).
    assert not [h for h in result.holdings if h.id.endswith("@@unrecorded")]
    for lot in result.created:
        tid = lot.id.removesuffix("@@unrecorded")
        taken = sum(m.sats + m.carried_fee for m in result.moves if m.lot == lot.id and m.transfer == tid)
        taken += sum(a.sats for a in result.allocations if a.lot == lot.id and a.disposal == tid)
        assert taken == lot.sats
    assert run(events, fee_treatment=treatment) == result  # deterministic


@pytest.mark.parametrize("bad", ["a@b", "x@@unrecorded", "@"])
def test_event_ids_cant_use_the_engines_separator(bad: str) -> None:
    with pytest.raises(EngineError, match="can't contain"):
        run([Acquisition(bad, "w", DAY, "buy", 1, D("1.00"))])


def test_outpoint_style_ids_are_allowed() -> None:
    # Callers may name events by outpoint (txid:vout): only "@" is the engine's.
    (lot,) = run([Acquisition("ab" * 32 + ":0", "w", DAY, "buy", 1, D("1.00"))]).holdings
    assert lot.id.endswith(":0")


def test_a_fee_that_is_disposed_needs_its_value() -> None:
    with pytest.raises(EngineError, match="needs its value"):
        run([*BUYS, move("wd", "w", "x", 10, kind="withdrawal", fee_sats=1)])
    with pytest.raises(EngineError, match="needs its value"):
        run([*BUYS, move("d", "w", "x", 10, fee_sats=1)], fee_treatment="dispose")
    assert run([*BUYS, move("d", "w", "x", 10, fee_sats=1)]).allocations == ()  # carried: no value needed
    explicit = run([*BUYS, move("wd", "w", "x", 10, kind="withdrawal", fee_sats=1, fee_value=D("0.00"))])
    assert [a.proceeds for a in explicit.allocations] == [D("0.00")]  # an explicit 0.00 is allowed


@pytest.mark.parametrize("kw", [{"missing_basis": D("1.00")}, {"missing_acquired": date(2024, 1, 1)}])
def test_unrecorded_sats_need_both_a_basis_and_an_original_date(kw: dict[str, Any]) -> None:
    with pytest.raises(EngineError, match="both"):
        run([move("wd", "x", "w", 10, kind="withdrawal", **kw)])


def test_an_account_without_lots_has_none_to_choose() -> None:
    wd = move("wd", "x", "w", 10, kind="withdrawal", picks=(Pick("e", 10),), identified_at=at(DAY))
    with pytest.raises(EngineError, match="none to choose"):
        run([wd])


def test_a_late_withdrawals_warning_shows_fifos_fee_disposal_too_adr0008() -> None:
    # FIFO would have taken e1 (10 sats, 1.00): its 2-sat fee is a disposal (basis 0.20, proceeds 0.50)
    # and 8 sats would arrive with the rest of its basis, 0.80.
    late_choice = move(
        "wd",
        "x",
        "w",
        10,
        kind="withdrawal",
        fee_sats=2,
        fee_value=D("0.50"),
        picks=(Pick("e2", 10),),
        identified_at=at(DAY, 18),
    )
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 10, D("3.00")),
        late_choice,
    ]
    (late,) = run(events).late
    fee, moved = late.standing
    assert isinstance(fee, Allocation) and isinstance(moved, Moved)
    assert (fee.lot, fee.sats, fee.basis, fee.proceeds) == ("e1", 2, D("0.20"), D("0.50"))
    assert (moved.lot, moved.sats, moved.basis) == ("e1", 8, D("0.80"))


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


def test_a_short_deposits_fee_value_follows_the_sats_it_covers() -> None:
    # 10 sats leave, the wallet holds 5. A 4-sat fee fits in what is covered (at most covered - 1 = 4):
    # all of its 1.00 is charged. An 8-sat fee doesn't: 4 sats are charged with half the value (0.50),
    # and the other 0.50 goes with the missing basis.
    b = Acquisition("b", "w", date(2024, 1, 1), "buy", 5, D("5.00"))
    fits = run([b, move("d", "w", "x", 10, fee_sats=4, fee_value=D("1.00"))], fee_treatment="dispose")
    assert [(a.sats, a.proceeds) for a in fits.allocations] == [(4, D("1.00"))]
    assert fits.blocking == (MissingLots("d", 5, D("0.00")),)
    short = run([b, move("d", "w", "x", 10, fee_sats=8, fee_value=D("1.00"))], fee_treatment="dispose")
    assert [(a.sats, a.proceeds) for a in short.allocations] == [(4, D("0.50"))]
    assert short.blocking == (MissingLots("d", 5, D("0.50")),)


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


def test_a_late_deposits_warning_under_carry_shows_the_fee_basis_arriving() -> None:
    # FIFO would have taken b1 (10 sats, 1.00) with a 2-sat carried fee: 8 sats arrive with all 1.00.
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("b2", "w", date(2024, 2, 1), "buy", 10, D("3.00")),
        move("d", "w", "x", 10, fee_sats=2, picks=(Pick("b2", 10),), identified_at=at(DAY, 18)),
    ]
    (late,) = run(events).late
    assert late.standing == (Moved("d", "b1", "b1@d", "x", 8, D("1.00"), date(2024, 1, 1), 2),)


def test_a_late_alternative_carries_dust_basis_as_the_real_run_would() -> None:
    # FIFO would take three 1-sat lots (1.00 each) with a 2-sat fee: b1 and b2 are all fee, and their
    # basis goes to b3's sat, exactly as when the transfer itself runs.
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    events.append(Acquisition("b9", "w", date(2024, 2, 1), "buy", 3, D("9.00")))
    late_choice = move("d", "w", "x", 3, fee_sats=2, picks=(Pick("b9", 3),), identified_at=at(DAY, 18))
    (late,) = run([*events, late_choice]).late
    assert late.standing == (Moved("d", "b3", "b3@d", "x", 1, D("3.00"), date(2024, 1, 3), 2, ("b1", "b2")),)
    real = run([*events, move("d", "w", "x", 3, fee_sats=2)])  # FIFO itself: the same figures
    assert real.moves == late.standing


def test_fee_remainders_go_one_sat_per_pick_by_largest_remainder() -> None:
    # 100 equal 10-sat lots, a 199-sat fee: 1.99 each, so 1 by floor and the 99 left one each, in FIFO
    # order (equal remainders): 99 lots pay 2 sats and the last pays 1, never 100 on one lot.
    events: list[Event] = [
        Acquisition(f"b{i:03}", "w", date(2024, 1, 1) + timedelta(days=i), "buy", 10, D("1.00"))
        for i in range(100)
    ]
    result = run([*events, move("d", "w", "x", 1000, fee_sats=199)])
    assert [m.carried_fee for m in result.moves] == [2] * 99 + [1]


def test_a_late_choice_of_fifos_lots_in_another_order_gets_the_same_fee_roles() -> None:
    # Two 2-sat lots and a 1-sat fee: the fee falls on the same lot whichever order the
    # user names them in, so a late choice of FIFO's own lots needs no warning.
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 2, D("2.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 2, D("4.00")),
    ]
    fifo = run([*events, move("wd", "x", "w", 4, kind="withdrawal", fee_sats=1, fee_value=D("1.00"))])
    picked = move(
        "wd",
        "x",
        "w",
        4,
        kind="withdrawal",
        fee_sats=1,
        fee_value=D("1.00"),
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


def test_a_carried_fees_value_plays_no_part() -> None:
    b = Acquisition("b", "w", date(2024, 1, 1), "buy", 5, D("5.00"))
    priced = run([b, move("d", "w", "x", 10, fee_sats=8, fee_value=D("1.00"))])  # carry, short
    unpriced = run([b, move("d", "w", "x", 10, fee_sats=8)])
    assert priced == unpriced
    assert priced.blocking == (MissingLots("d", 5, D("0.00")),)


def test_dust_of_a_gift_lot_cant_carry_its_basis_into_another_lot() -> None:
    gift = Acquisition("g", "w", date(2024, 1, 1), "gift_in", 1, D("10.00"), D("5.00"), date(2020, 1, 1))
    events: list[Event] = [gift, Acquisition("b", "w", date(2024, 1, 2), "buy", 1, D("1.00"))]
    with pytest.raises(EngineError, match="can't carry the basis"):
        run([*events, move("d", "w", "x", 2, fee_sats=1)])
    # Under the disposal treatment the gift's sat is simply the fee disposal, with its dual basis.
    result = run([*events, move("d", "w", "x", 2, fee_sats=1, fee_value=D("0.50"))], fee_treatment="dispose")
    (fee,) = result.allocations
    assert (fee.lot, fee.rule, fee.basis) == ("g", "fmv_at_gift", D("5.00"))


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
    # the shares are [9, 1], so b2's 1-sat part is all fee and carries 1/10 of b2's basis (0.10).
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


def test_dust_goes_to_the_last_plain_moved_part() -> None:
    # Lots of 1, 2 and 3 sats, a 4-sat fee (of 6): the floor shares are 0, 1 and 2, the one sat left
    # can't be taken by the 2- or 3-sat parts without emptying them, so it falls on the 1-sat dust.
    # The dust's basis goes to the buy, the last plain part that moves, not to the gift.
    events: list[Event] = [
        Acquisition("dust", "w", date(2024, 1, 1), "buy", 1, D("0.50")),
        Acquisition("buy", "w", date(2024, 1, 2), "buy", 2, D("2.00")),
        Acquisition("g", "w", date(2024, 1, 3), "gift_in", 3, D("3.00"), D("1.00"), date(2020, 1, 1)),
    ]
    result = run([*events, move("d", "w", "x", 6, fee_sats=4)])
    assert [(m.lot, m.sats, m.basis, m.carried_fee, m.carried_from) for m in result.moves] == [
        ("buy", 1, D("2.50"), 2, ("dust",)),
        ("g", 1, D("3.00"), 2, ()),
    ]


def test_a_moved_inherited_lot_stays_long_term_irc1223() -> None:
    events: list[Event] = [
        Acquisition("i", "w", date(2025, 5, 1), "inherit", 10, D("10.00")),
        move("d", "w", "x", 10, on=date(2025, 5, 2)),
        Disposal("s", "x", date(2025, 5, 3), at(date(2025, 5, 3)), "sell", 10, D("12.00")),
    ]
    (a,) = run(events).allocations
    assert a.long_term


def test_a_late_choice_stands_when_fifos_alternative_would_hit_refused_dust_adr0008() -> None:
    # FIFO would take a 1-sat gift and a 1-sat buy for a 2-sat deposit with a 1-sat fee: the gift's sat
    # would be all-fee dust, which can't carry its basis. The user picked a later 2-sat buy, late: the
    # choice is used, and the warning shows the gift's dust as its own part, none of it arriving.
    events: list[Event] = [
        Acquisition("g", "w", date(2024, 1, 1), "gift_in", 1, D("10.00"), D("5.00"), date(2020, 1, 1)),
        Acquisition("b", "w", date(2024, 1, 2), "buy", 1, D("1.00")),
        Acquisition("later", "w", date(2024, 2, 1), "buy", 2, D("2.00")),
    ]
    choice = move("d", "w", "x", 2, fee_sats=1, picks=(Pick("later", 2),), identified_at=at(DAY, 18))
    result = run([*events, choice])
    assert [m.lot for m in result.moves] == ["later"]
    (late,) = result.late
    assert late.standing == (
        Moved("d", "b", "b@d", "x", 1, D("1.00"), date(2024, 1, 2), 0),
        Moved("d", "g", "g@d", "x", 0, D("10.00"), date(2020, 1, 1), 1),
    )


@pytest.mark.parametrize("picks", [None, (Pick("b1", 5),)])
def test_a_self_transfer_is_refused_until_utxo_tracing_exists(picks: tuple[Pick, ...] | None) -> None:
    # #243, the owner's decision: between the user's own wallets the spent outputs identify the lots, so
    # the account's FIFO order (or a choice of lots) must not stand in for them
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        move("t1", "w", "v", 5, kind="self_transfer", picks=picks),
    ]
    with pytest.raises(EngineError, match=r"transfer 't1': .* needs UTXO tracing \(not built yet, #243\)"):
        run(events)

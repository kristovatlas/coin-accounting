"""The lot engine's transfers: deposits, withdrawals and self-transfers move lots between accounts, and
fees by role (PLAN §7; ADRs 0008, 0009; T-501, T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    assert result.holdings == (Lot("wd:unrecorded@wd", "w", date(2023, 5, 1), BTC, D("5000.00"), False),)


def test_a_withdrawal_without_lots_or_a_basis_is_unknown_basis_t509() -> None:
    result = run([move("wd", "x", "w", BTC, kind="withdrawal")])
    assert result.blocking == (UnknownBasis("wd:unrecorded"),)
    (lot,) = result.holdings
    assert (lot.account, lot.unknown_basis, lot.acquired) == ("w", True, DAY)


def test_a_withdrawal_uses_the_lots_it_has_and_creates_only_the_rest() -> None:
    events: list[Event] = [
        Acquisition("e", "x", date(2024, 1, 1), "buy", 30, D("3.00")),
        move("wd", "x", "w", 100, kind="withdrawal", missing_basis=D("7.00")),
    ]
    assert [(m.lot, m.sats, m.basis) for m in run(events).moves] == [
        ("e", 30, D("3.00")),
        ("wd:unrecorded", 70, D("7.00")),
    ]


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
    # Three 1-sat lots, 2 sats of fee: two parts are all fee, so their basis (2.00) goes to the one sat
    # that arrives, which carries all three lots' basis.
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    result = run([*events, move("d", "w", "x", 3, fee_sats=2)])
    assert result.moves == (Moved("d", "b1", "b1@d", "x", 1, D("3.00"), date(2024, 1, 1), 2),)
    assert result.holdings == (Lot("b1@d", "x", date(2024, 1, 1), 1, D("3.00"), False),)


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
            value = D(data.draw(st.integers(0, 10**4))).scaleb(-2) if fee else D("0.00")
            events.append(move(f"e{i}", src, dst, sats, on=on, kind=kind, fee_sats=fee, fee_value=value))
    result = run(events, fee_treatment=treatment)
    bought = [e for e in events if isinstance(e, Acquisition)]
    # Lots created for withdrawals' unrecorded sats enter the books there (with unknown basis, 0.00, here):
    # they are used up whole by the withdrawal, partly as its fee disposal.
    created = sum(m.sats + m.carried_fee for m in result.moves if m.lot.endswith(":unrecorded")) + sum(
        a.sats for a in result.allocations if a.lot.endswith(":unrecorded")
    )
    sold = sum(a.sats for a in result.allocations)
    held = sum(h.sats for h in result.holdings)
    carried = sum(m.carried_fee for m in result.moves)  # carried fees leave the books, their basis stays
    assert sum(b.sats for b in bought) + created == sold + held + carried
    used = sum((a.lot_basis if a.lot_basis is not None else a.basis) for a in result.allocations)
    assert sum(b.basis or D("0.00") for b in bought) == used + sum(h.basis for h in result.holdings)
    if treatment == "dispose":
        assert carried == 0
    assert all(m.sats > 0 for m in result.moves)
    assert run(events, fee_treatment=treatment) == result  # deterministic

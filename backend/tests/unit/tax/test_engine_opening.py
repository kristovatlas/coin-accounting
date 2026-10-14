"""The 2025 opening allocation (ADR 0008 §6; ADR 0041 §5; Rev. Proc. 2024-28 §5.02): hand-worked figures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from coinacct.domain.money import share
from coinacct.tax.engine import (
    Acquisition,
    Coin,
    Disposal,
    EngineError,
    Event,
    LateOpening,
    Lot,
    OpeningAllocation,
    OpeningDiffers,
    OpeningLot,
    OpeningMismatch,
    Pick,
    Spent,
    Transfer,
    UnknownBasis,
    Untraced,
    Wallet,
    WalletTx,
    run,
)
from coinacct.tax.tracing import Output

D = Decimal
DAY = date(2025, 1, 1)
RECORDED = datetime(2024, 12, 20, tzinfo=UTC)  # made before 2025, as Rev. Proc. 2024-28 asks

# x held two lots before 2025: 600 sats from 2023 ($6) and 400 from 2024 ($8)
HELD: list[Event] = [
    Acquisition("a", "x", date(2023, 5, 1), "buy", 600, D("6.00")),
    Acquisition("b", "x", date(2024, 5, 1), "buy", 400, D("8.00")),
]


def opening(*lots: OpeningLot, **kw: Any) -> OpeningAllocation:
    return OpeningAllocation(
        kw.pop("id", "open"),
        kw.pop("account", "x"),
        kw.pop("on", DAY),
        lots,
        kw.pop("recorded_at", RECORDED),
        kw.pop("method", "the actual unit method, by the wallet's records"),
    )


def sell(id: str, on: date, sats: int, proceeds: str, hour: int = 12) -> Disposal:
    return Disposal(
        id, "x", on, datetime(on.year, on.month, on.day, hour, tzinfo=UTC), "sell", sats, D(proceeds)
    )


SPLIT = opening(OpeningLot(700, D("5.00"), date(2022, 1, 1)), OpeningLot(300, D("9.00"), date(2024, 6, 1)))


def test_the_allocation_replaces_the_accounts_lots_adr0008() -> None:
    result = run([*HELD, SPLIT])
    assert [(lot.id, lot.sats, lot.basis, lot.acquired) for lot in result.holdings if lot.sats] == [
        ("open@1", 700, D("5.00"), date(2022, 1, 1)),
        ("open@2", 300, D("9.00"), date(2024, 6, 1)),
    ]
    assert [(lot.id, lot.sats, lot.basis) for lot in result.replaced] == [
        ("a", 600, D("6.00")),
        ("b", 400, D("8.00")),
    ]
    assert result.blocking == ()


def test_a_sale_after_the_allocation_draws_on_its_lots_oldest_first() -> None:
    result = run([*HELD, SPLIT, sell("s", date(2025, 3, 1), 800, "80.00")])
    # FIFO: open@1's 700 ($5.00, held since 2022: long-term) and 100 of open@2 ($3.00 of $9.00, held
    # since 2024-06-01: short-term)
    assert [(a.lot, a.sats, a.basis, a.long_term) for a in result.allocations] == [
        ("open@1", 700, D("5.00"), True),
        ("open@2", 100, D("3.00"), False),
    ]


def test_events_before_2025_still_use_the_derived_lots() -> None:
    result = run(
        [
            *HELD,
            sell("s", date(2024, 12, 1), 100, "2.00"),
            opening(OpeningLot(900, D("12.00"), date(2023, 1, 1))),
        ]
    )
    assert [(a.lot, a.sats) for a in result.allocations] == [("a", 100)]
    assert [(lot.id, lot.sats) for lot in result.replaced] == [("a", 500), ("b", 400)]


def test_an_inherited_allocated_lot_is_long_term_whatever_its_date() -> None:
    inherited = opening(OpeningLot(1_000, D("10.00"), date(2024, 12, 1), always_long=True))
    (row,) = run([*HELD, inherited, sell("s", date(2025, 1, 5), 1_000, "20.00")]).allocations
    assert row.long_term


def test_allocating_fewer_sats_than_held_leaves_a_placeholder_t509() -> None:
    result = run([*HELD, opening(OpeningLot(900, D("12.00"), date(2023, 1, 1)))])
    (placeholder,) = result.placeholders
    assert (placeholder.id, placeholder.sats, placeholder.unknown_basis) == ("open@@placeholder", 100, True)
    assert UnknownBasis("open@@placeholder") in result.blocking


def test_allocating_more_sats_than_held_blocks_and_changes_nothing() -> None:
    result = run([*HELD, opening(OpeningLot(1_001, D("12.00"), date(2023, 1, 1)))])
    assert result.blocking == (OpeningMismatch("open", 1_000, 1_001),)
    assert {lot.id for lot in result.holdings} == {"a", "b"}
    assert result.replaced == ()


def test_other_accounts_keep_their_lots() -> None:
    other = Acquisition("c", "y", date(2024, 1, 1), "buy", 50, D("1.00"))
    result = run([HELD[0], other, HELD[1], SPLIT])
    assert {lot.id for lot in result.holdings} == {"c", "open@1", "open@2"}


def test_a_coin_traced_wallets_allocation_blocks_for_now_adr0041() -> None:
    wallet = Acquisition("w1", "w", date(2024, 1, 1), "buy", 50, D("1.00"))
    result = run(
        [wallet, opening(OpeningLot(50, D("1.00"), date(2024, 1, 1)), account="w")],
        wallets={"w": Wallet(None)},
    )
    assert [(b.event, b.reason) for b in result.blocking if isinstance(b, Untraced)] == [
        ("w", "no_tracking_start"),
        ("open", "opening_allocation"),
    ]
    assert {lot.id for lot in result.holdings} == {"w1"}


# Lateness (ADR 0041 §5): after the user's first sale, disposition or transfer from 2025-01-01, from any
# account, or after the 2025 return's due date if that comes first. A warning: the allocation is used.


def test_an_allocation_recorded_before_the_first_2025_sale_is_on_time() -> None:
    recorded = opening(
        OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=datetime(2025, 2, 1, tzinfo=UTC)
    )
    assert run([*HELD, recorded, sell("s", date(2025, 3, 1), 10, "1.00")]).opening_late == ()


def test_an_allocation_recorded_after_the_first_2025_sale_is_late_t508() -> None:
    late_at = datetime(2025, 3, 2, tzinfo=UTC)
    recorded = opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=late_at)
    result = run([*HELD, recorded, sell("s", date(2025, 3, 1), 10, "1.00")])
    assert result.opening_late == (LateOpening("open", late_at, datetime(2025, 3, 1, 12, tzinfo=UTC), "s"),)
    assert result.allocations[0].lot == "open@1"  # used all the same


def test_another_accounts_transfer_counts_as_the_first() -> None:
    late_at = datetime(2025, 2, 2, tzinfo=UTC)
    deposit = Transfer("d", "y", "z", date(2025, 2, 1), datetime(2025, 2, 1, 9, tzinfo=UTC), "deposit", 10)
    events: list[Event] = [
        HELD[0],
        Acquisition("c", "y", date(2024, 1, 1), "buy", 50, D("1.00")),
        HELD[1],
        opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=late_at),
        deposit,
    ]
    (warning,) = run(events).opening_late
    assert (warning.first, warning.deadline) == ("d", datetime(2025, 2, 1, 9, tzinfo=UTC))


def test_with_no_2025_sale_the_due_date_is_the_deadline() -> None:
    late_at = datetime(2026, 4, 16, tzinfo=UTC)
    (warning,) = run(
        [*HELD, opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=late_at)]
    ).opening_late
    assert (warning.first, warning.deadline) == (None, datetime(2026, 4, 15, tzinfo=UTC))


def test_a_sale_after_the_due_date_makes_the_due_date_the_deadline() -> None:
    late_at = datetime(2026, 5, 2, tzinfo=UTC)
    recorded = opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=late_at)
    (warning,) = run([*HELD, recorded, sell("s", date(2026, 5, 1), 10, "1.00")]).opening_late
    assert (warning.first, warning.deadline) == (None, datetime(2026, 4, 15, tzinfo=UTC))


def test_the_replay_follows_the_allocation() -> None:
    # a late choice before the allocation, and a sale after it: the replay follows both
    late = Disposal(
        "s0",
        "x",
        date(2024, 12, 1),
        datetime(2024, 12, 1, 12, tzinfo=UTC),
        "sell",
        100,
        D("2.00"),
        picks=(Pick("b", 100),),
        identified_at=datetime(2024, 12, 2, tzinfo=UTC),
    )
    events = [
        *HELD,
        late,
        opening(OpeningLot(900, D("12.00"), date(2023, 1, 1))),
        sell("s", date(2025, 3, 1), 10, "1.00"),
    ]
    result = run(events)
    assert [w.disposal for w in result.late] == ["s0"]
    assert result.replay_stopped is None


@pytest.mark.parametrize(
    ("event", "match"),
    [
        (opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), on=date(2025, 1, 2)), "dated 2025-01-01"),
        (opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), method=" "), "states the method"),
        (opening(), "a tuple of OpeningLots"),
        (opening(OpeningLot(0, D("12.00"), date(2023, 1, 1))), "sats must be a positive whole number"),
        (opening(OpeningLot(1_000, D("12.00"), date(2025, 1, 1))), "from genesis to 2024-12-31"),
        (opening(OpeningLot(1_000, D("-1.00"), date(2023, 1, 1))), "can't be negative"),
        (opening(OpeningLot(1_000, D("1.00"), date(2024, 12, 21))), "made before a lot it allocates"),
        (opening(OpeningLot(1_000, D("1.00"), date(2023, 1, 1), always_long=1)), "True or False"),  # type: ignore[arg-type]
        (
            opening(OpeningLot(1_000, D("1.00"), date(2023, 1, 1)), recorded_at=datetime(2024, 1, 1)),
            "recorded",
        ),
        (opening(OpeningLot(1_000, D("1.00"), date(2023, 1, 1)), id="o@1"), "can't contain"),
    ],
)
def test_invalid_allocations_raise(event: OpeningAllocation, match: str) -> None:
    with pytest.raises(EngineError, match=match):
        run([*HELD, event])


def test_one_allocation_per_account() -> None:
    first = opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)))
    with pytest.raises(EngineError, match="has one already"):
        run([*HELD, first, opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), id="again")])


def test_the_allocation_comes_before_every_other_2025_event() -> None:
    with pytest.raises(EngineError, match="comes before every other 2025 event"):
        run([*HELD, Acquisition("c", "x", DAY, "buy", 1, D("1.00")), SPLIT])


def test_a_lot_left_is_a_lot() -> None:
    (lot,) = [lot for lot in run([*HELD, SPLIT]).holdings if lot.id == "open@2"]
    assert lot == Lot("open@2", "x", date(2024, 6, 1), 300, D("9.00"), False)


def test_an_allocation_recorded_at_the_first_sales_moment_is_late() -> None:
    # Rev. Proc. 2024-28 §5.02(4): complete *before* the first sale; at its very moment is too late
    sale = sell("s", date(2025, 3, 1), 10, "1.00")
    recorded = opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=sale.at)
    (warning,) = run([*HELD, recorded, sale]).opening_late
    assert (warning.first, warning.deadline) == ("s", sale.at)


def test_an_allocation_recorded_on_the_due_date_is_late() -> None:
    recorded = opening(
        OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=datetime(2026, 4, 15, tzinfo=UTC)
    )
    assert len(run([*HELD, recorded]).opening_late) == 1


def test_a_wallets_first_transaction_counts_and_is_named_by_its_leaving_event() -> None:
    # a coin-traced wallet's sale is the first 2025 disposition: the warning names the sale, not the
    # wallet transaction; and a consolidation counts too (any spend)
    coin = Acquisition(
        "c", "w", date(2024, 1, 1), "buy", 1_000, D("1.00"), coins=(Coin("c0:0", 1_000, 1_000),)
    )
    sale = Disposal("ws", "w", date(2025, 2, 1), datetime(2025, 2, 1, 9, tzinfo=UTC), "spend", 900, D("9.00"))
    tx = WalletTx(
        "wtx",
        "w",
        date(2025, 2, 1),
        datetime(2025, 2, 1, 9, tzinfo=UTC),
        "t1",
        (Spent("c0:0", 1_000, "w"),),
        (Output(0, 900, b"\x00\x14pay", event="ws"),),
        sale,
    )
    late_at = datetime(2025, 2, 2, tzinfo=UTC)
    events = [
        HELD[0],
        coin,
        HELD[1],
        opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=late_at),
        tx,
    ]
    (warning,) = run(events, wallets={"w": Wallet(datetime(2024, 1, 1, tzinfo=UTC))}).opening_late
    assert warning.first == "ws"
    consolidation = WalletTx(
        "wtx",
        "w",
        date(2025, 2, 1),
        datetime(2025, 2, 1, 9, tzinfo=UTC),
        "t1",
        (Spent("c0:0", 1_000, "w"),),
        (Output(0, 900, b"\x00\x14chg", account="w", kind="self_custody"),),
    )
    events = [
        HELD[0],
        coin,
        HELD[1],
        opening(OpeningLot(1_000, D("12.00"), date(2023, 1, 1)), recorded_at=late_at),
        consolidation,
    ]
    (warning,) = run(events, wallets={"w": Wallet(datetime(2024, 1, 1, tzinfo=UTC))}).opening_late
    assert warning.first == "wtx"


def test_an_allocation_that_moves_the_unused_basis_raises_no_warning() -> None:
    # the same units of unused basis, each with its date, reallocated: nothing differs
    same = opening(OpeningLot(600, D("6.00"), date(2023, 5, 1)), OpeningLot(400, D("8.00"), date(2024, 5, 1)))
    assert run([*HELD, same]).opening_differs == ()


def test_an_allocation_that_adds_basis_or_backdates_warns() -> None:
    # SPLIT puts 700 sats with $5 on 2022-01-01 and 300 with $9 on 2024-06-01; the replaced lots were
    # 600 ($6) from 2023-05-01 and 400 ($8) from 2024-05-01: every date differs
    assert run([*HELD, SPLIT]).opening_differs == (
        OpeningDiffers(date(2022, 1, 1), False, 700, D("5.00"), 0, D("0.00")),
        OpeningDiffers(date(2023, 5, 1), False, 0, D("0.00"), 600, D("6.00")),
        OpeningDiffers(date(2024, 5, 1), False, 0, D("0.00"), 400, D("8.00")),
        OpeningDiffers(date(2024, 6, 1), False, 300, D("9.00"), 0, D("0.00")),
    )


def test_basis_moved_between_accounts_is_compared_across_them() -> None:
    # x holds 600 sats (2023, $6), y 400 (2024, $8). The allocations move the units across accounts:
    # x gets all of 2024's $8 on 400 sats and 200 of 2023's ($2), y the other 400 of 2023's ($4).
    # Across both accounts the units, by date, are the same: no warning
    events: list[Event] = [
        HELD[0],
        Acquisition("c", "y", date(2024, 5, 1), "buy", 400, D("8.00")),
        opening(
            OpeningLot(400, D("8.00"), date(2024, 5, 1)),
            OpeningLot(200, D("2.00"), date(2023, 5, 1)),
            id="ox",
        ),
        opening(OpeningLot(400, D("4.00"), date(2023, 5, 1)), id="oy", account="y"),
    ]
    result = run(events)
    assert (result.opening_differs, result.blocking) == ((), ())


def test_the_replay_judges_a_late_choice_against_the_allocated_lots() -> None:
    # a late 2025 pick of open@2: the replay's alternative (FIFO) is open@1, the allocation's lot
    late = Disposal(
        "s",
        "x",
        date(2025, 3, 1),
        datetime(2025, 3, 1, 12, tzinfo=UTC),
        "sell",
        100,
        D("2.00"),
        picks=(Pick("open@2", 100),),
        identified_at=datetime(2025, 3, 2, tzinfo=UTC),
    )
    (warning,) = run([*HELD, SPLIT, late]).late
    assert [a.lot for a in warning.standing] == ["open@1"]
    assert warning.standing[0].basis == share(D("5.00"), 100, 700)


def test_an_allocation_that_inflates_basis_on_the_same_units_warns() -> None:
    # the same sats on the same dates, with $1,000,000 of basis instead of $6: basis from nothing
    inflated = opening(
        OpeningLot(600, D("1000000.00"), date(2023, 5, 1)), OpeningLot(400, D("8.00"), date(2024, 5, 1))
    )
    assert run([*HELD, inflated]).opening_differs == (
        OpeningDiffers(date(2023, 5, 1), False, 600, D("1000000.00"), 600, D("6.00")),
    )

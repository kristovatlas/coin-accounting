"""The late choices' full replay (#238; ADR 0008 §4, ADR 0021 §2, T-508): a late choice is judged, and
its warning figured, against the standing method in a replay where every late choice is disregarded."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from coinacct.tax import engine
from coinacct.tax.engine import (
    Acquisition,
    Allocation,
    Disposal,
    Event,
    LateIdentification,
    Moved,
    Pick,
    Transfer,
    run,
)

D = Decimal
SALE = date(2025, 3, 1)
LATE = datetime(2026, 1, 1, 12, tzinfo=UTC)  # chosen months after every sale below: late
ONTIME = datetime(2025, 3, 1, 9, tzinfo=UTC)  # chosen before the noon sale: on time


def noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def lot(id: str, day: int, basis: str) -> Acquisition:
    return Acquisition(id, "w", date(2024, 1, day), "buy", 10, D(basis))


def sale(id: str, day: int, lot_id: str, chosen: datetime | None = LATE) -> Disposal:
    on = date(2025, 3, day)
    picks = (Pick(lot_id, 10),)
    return Disposal(id, "w", on, noon(on), "sell", 10, D("50.00"), picks, chosen)


THREE: list[Event] = [lot("b1", 1, "1.00"), lot("b2", 2, "2.00"), lot("b3", 3, "3.00")]


def fifo(sale_id: str, day: int, lot_id: str, basis: str, acquired: int) -> Allocation:
    return Allocation(
        sale_id, lot_id, 10, D(basis), D("50.00"), date(2024, 1, acquired), date(2025, 3, day), True
    )


def test_a_later_late_choice_is_judged_as_if_the_earlier_one_were_disregarded() -> None:
    # s1 chooses b3 late: FIFO would have sold b1. s2 then chooses b1 late. Against the lots the user's
    # choices left, b1 *is* FIFO's next lot, so s2 would look fine. In the replay s1 sold b1, so FIFO's
    # next lot is b2: s2 differs, is warned about, and its figures are b2's.
    result = run([*THREE, sale("s1", 1, "b3"), sale("s2", 2, "b1")])
    assert result.late == (
        LateIdentification("s1", LATE, (fifo("s1", 1, "b1", "1.00", 1),)),
        LateIdentification("s2", LATE, (fifo("s2", 2, "b2", "2.00", 2),)),
    )
    assert [a.lot for a in result.allocations] == ["b3", "b1"]  # the user's choices still stand
    assert result.replay_stopped is None


def test_a_late_choice_that_matches_the_replays_fifo_is_not_warned() -> None:
    # s1 chooses b3 late (the replay sells b1). s2 chooses b2 late: against the user's lots FIFO's next
    # is b1, so it would be warned; in the replay FIFO's next *is* b2, so it isn't
    result = run([*THREE, sale("s1", 1, "b3"), sale("s2", 2, "b2")])
    assert [w.disposal for w in result.late] == ["s1"]


def test_an_on_time_choice_the_replay_cant_follow_uses_the_standing_method_there() -> None:
    # s1 chooses b3 late (the replay sells b1 instead). s2 chooses b1 on time: the user holds b1, the
    # replay doesn't, so the replay sells by FIFO (b2). s3 then chooses b2 late: the replay holds only
    # b3, so FIFO's lot is b3 and s3's warning shows b3
    result = run([*THREE, sale("s1", 1, "b3"), sale("s2", 2, "b1", ONTIME), sale("s3", 3, "b2")])
    assert [w.disposal for w in result.late] == ["s1", "s3"]
    assert result.late[1].standing == (fifo("s3", 3, "b3", "3.00", 3),)
    assert result.replay_stopped is None


def test_a_late_transfer_is_judged_and_figured_in_the_replay() -> None:
    # s1 chooses b3 late (the replay sells b1). The deposit then chooses b1 late: in the replay FIFO
    # moves b2, so the warning shows b2 arriving
    on = date(2025, 3, 2)
    deposit = Transfer(
        "d", "w", "x", on, noon(on), "deposit", 10, picks=(Pick("b1", 10),), identified_at=LATE
    )
    result = run([*THREE, sale("s1", 1, "b3"), deposit])
    assert result.late[1] == LateIdentification(
        "d", LATE, (Moved("d", "b2", "b2@d", "x", 10, D("2.00"), date(2024, 1, 2)),)
    )
    assert [m.lot for m in result.moves] == ["b1"]  # the user's choice moved b1


def _diverged(**last: object) -> list[Event]:
    """A history where the two runs disagree on whether account x holds lots the user recorded: the
    user moves a lot the engine created into x (still unrecorded), the replay moves the recorded b1
    (recorded). Both then sell x empty, and a withdrawal from x follows."""
    created = Transfer(
        "wd0", "e", "w", date(2025, 2, 1), noon(date(2025, 2, 1)), "withdrawal", 10,
        missing_basis=D("2.00"), missing_acquired=date(2024, 6, 1),
    )  # fmt: skip
    hop = Transfer(
        "t1", "w", "x", date(2025, 3, 1), noon(date(2025, 3, 1)), "deposit", 10,
        picks=(Pick("wd0@@unrecorded@wd0", 10),), identified_at=LATE,
    )  # fmt: skip
    empty = Disposal("s1", "x", date(2025, 3, 2), noon(date(2025, 3, 2)), "sell", 10, D("50.00"))
    out = Transfer("wd2", "x", "y", date(2025, 3, 3), noon(date(2025, 3, 3)), "withdrawal", 10, **last)  # type: ignore[arg-type]
    return [Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")), created, hop, empty, out]


def test_a_replay_that_cant_follow_an_event_stops_there_and_names_it() -> None:
    # The real run creates a lot in x for wd2's unrecorded sats; the replay, where x holds recorded
    # lots, refuses a basis for unrecorded sats there and stops. A late choice after that is figured
    # on the user's lots, and says so
    events = _diverged(missing_basis=D("3.00"), missing_acquired=date(2024, 7, 1))
    on = date(2025, 3, 4)
    later = Disposal(
        "s2", "w", on, noon(on), "sell", 1, D("5.00"), (Pick("b1", 1),), LATE
    )  # w holds only b1 in the real run: matches FIFO there, so no warning
    result = run([*events, later])
    assert result.replay_stopped == "wd2"
    assert [w.disposal for w in result.late] == ["t1"]  # t1 before the stop: judged in the replay
    assert result.late[0].replayed
    assert [lot.id for lot in result.created] == ["wd0@@unrecorded", "wd2@@unrecorded"]


def test_after_a_stop_a_differing_late_choice_is_figured_on_the_users_lots() -> None:
    events = _diverged(missing_basis=D("3.00"), missing_acquired=date(2024, 7, 1))
    on = date(2025, 3, 4)
    buy2 = Acquisition("b9", "w", on, "buy", 10, D("9.00"))
    late = Disposal("s2", "w", on, noon(on), "sell", 10, D("5.00"), (Pick("b9", 10),), LATE)
    result = run([*events, buy2, late])
    warning = result.late[-1]
    assert (warning.disposal, warning.replayed) == ("s2", False)  # the replay had stopped at wd2
    assert [a.lot for a in warning.standing] == ["b1"]  # FIFO on the user's lots


def test_where_only_the_real_run_creates_a_lot_the_replay_stops() -> None:
    # wd2 names no basis: the real run creates an unknown-basis lot in x for it; the replay, with x
    # recorded, reports missing lots instead. The two would hold different sats from here, so the
    # replay stops at wd2, and a late choice after it is judged on the user's lots: y's only lot is
    # FIFO's there, so no warning
    events = _diverged()
    on = date(2025, 3, 4)
    late = Disposal("s2", "y", on, noon(on), "sell", 10, D("5.00"), (Pick("wd2@@unrecorded@wd2", 10),), LATE)
    result = run([*events, late])
    assert result.replay_stopped == "wd2"
    assert [w.disposal for w in result.late] == ["t1"]


def test_where_only_the_replay_would_create_a_lot_it_stops_too() -> None:
    # The other way round: the engine-created lot is older than b1, so the replay's FIFO moves it to x
    # (x stays unrecorded there) while the user's late choice moves b1 (x recorded). Both sell x empty;
    # a withdrawal from x naming no basis is missing lots for the user but would create a lot in the
    # replay: it stops there instead of inventing a lot the real run never had
    created = Transfer(
        "wd0", "e", "w", date(2025, 2, 1), noon(date(2025, 2, 1)), "withdrawal", 10,
        missing_basis=D("2.00"), missing_acquired=date(2023, 6, 1),
    )  # fmt: skip
    hop = Transfer(
        "t1", "w", "x", date(2025, 3, 1), noon(date(2025, 3, 1)), "deposit", 10,
        picks=(Pick("b1", 10),), identified_at=LATE,
    )  # fmt: skip
    empty = Disposal("s1", "x", date(2025, 3, 2), noon(date(2025, 3, 2)), "sell", 10, D("50.00"))
    out = Transfer("wd2", "x", "y", date(2025, 3, 3), noon(date(2025, 3, 3)), "withdrawal", 10)
    b1 = Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00"))
    result = run([b1, created, hop, empty, out])
    assert result.replay_stopped == "wd2"
    assert [type(b).__name__ for b in result.blocking] == ["MissingLots"]  # the user's run: no lot made
    assert [lot.id for lot in result.created] == ["wd0@@unrecorded"]


def test_a_late_gift_given_is_figured_in_the_replay() -> None:
    # s1 chooses b3 late (the replay sells b1). The gift then chooses b1 late: in the replay FIFO
    # gives b2, so the warning's gift record is b2's basis and date
    on = date(2025, 3, 2)
    gift = Disposal("g", "w", on, noon(on), "gift_out", 10, D("0.00"), (Pick("b1", 10),), LATE)
    result = run([*THREE, sale("s1", 1, "b3"), gift])
    record = result.late[1].standing[0]
    assert isinstance(record, engine.GiftGiven)
    assert (record.lot, record.basis, record.acquired) == ("b2", D("2.00"), date(2024, 1, 2))


def test_a_late_transfers_fee_is_figured_on_the_replays_lot() -> None:
    # s1 chooses b3 late (the replay sells b1). A deposit of 10 with a 2-sat fee then chooses b1 late:
    # in the replay FIFO moves b2 (basis 2.00): 8 sats arrive with all of b2's basis (ADR 0043)
    on = date(2025, 3, 2)
    deposit = Transfer(
        "d", "w", "x", on, noon(on), "deposit", 10, fee_sats=2, picks=(Pick("b1", 10),), identified_at=LATE
    )
    standing = run([*THREE, sale("s1", 1, "b3"), deposit]).late[1].standing
    assert standing == (Moved("d", "b2", "b2@d", "x", 8, D("2.00"), date(2024, 1, 2), 2),)


def test_the_same_coins_moved_in_other_amounts_are_the_same_lot_adr0040() -> None:
    # b1 is 10 sats with $0.01 of basis. The deposit late-chooses 5 of b1 and 5 of b2: the user's
    # b1@d gets half a cent, rounded to $0.00; the replay moves all of b1 ($0.01). s1 sells b2@d on
    # time, which the replay doesn't hold, so it sells 5 of its b1@d by FIFO. Both runs then hold 5
    # sats of b1@d, the same coins of b1 moved by d, with $0.00 and $0.01. s2's late choice of b1@d
    # is the replay's FIFO too: not warned (ADR 0040: the lots are the same; the cent is d's, which is
    # warned about)
    on1, on2, on3 = date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3)
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("0.01")),
        Acquisition("b2", "w", date(2024, 1, 2), "buy", 10, D("0.02")),
        Transfer("d", "w", "x", on1, noon(on1), "deposit", 10,
                 picks=(Pick("b1", 5), Pick("b2", 5)), identified_at=LATE),
        Disposal("s1", "x", on2, noon(on2), "sell", 5, D("5.00"), (Pick("b2@d", 5),), noon(on1)),
        Disposal("s2", "x", on3, noon(on3), "sell", 5, D("5.00"), (Pick("b1@d", 5),), LATE),
    ]  # fmt: skip
    result = run(events)
    assert [w.disposal for w in result.late] == ["d"]
    assert [(a.lot, a.basis) for a in result.allocations] == [("b2@d", D("0.01")), ("b1@d", D("0.00"))]


def _pick(id: str, day: int, sats: int, picks: tuple[Pick, ...]) -> Disposal:
    on = date(2025, 3, day)
    return Disposal(id, "w", on, noon(on), "sell", sats, D("5.00"), picks, LATE)


def test_a_lot_whose_remainder_differs_only_by_rounding_is_not_warned_adr0040() -> None:
    # b1 is 10 sats with $0.03. s1 late-picks 3 of b1 and 2 of b2: the real b1 keeps 7 sats and $0.02,
    # the replay's (FIFO took 5, $0.015 rounded to $0.02) 5 sats and $0.01. s2 late-picks 2 of b1, which
    # is the replay's FIFO too: the same lot, so no warning, though a share of each remainder would
    # differ by a cent
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("0.03")),
        Acquisition("b2", "w", date(2024, 1, 2), "buy", 10, D("0.04")),
        _pick("s1", 1, 5, (Pick("b1", 3), Pick("b2", 2))),
        _pick("s2", 2, 2, (Pick("b1", 2),)),
    ]
    assert [w.disposal for w in run(events).late] == ["s1"]


def test_a_lot_used_differently_before_is_still_the_same_lot() -> None:
    # b0 is 1 sat, b1 10 sats with $0.05. s1 late-picks 3 of b1: the real b1 keeps 7 sats and $0.03,
    # the replay's (FIFO took b0 and 2 of b1) 8 sats and $0.04. s2 late-picks 7 of b1, the replay's
    # FIFO too: not warned
    events: list[Event] = [
        Acquisition("b0", "w", date(2024, 1, 1), "buy", 1, D("0.00")),
        Acquisition("b1", "w", date(2024, 1, 2), "buy", 10, D("0.05")),
        _pick("s1", 1, 3, (Pick("b1", 3),)),
        _pick("s2", 2, 7, (Pick("b1", 7),)),
    ]
    assert [w.disposal for w in run(events).late] == ["s1"]


def test_a_lot_moved_the_same_way_in_both_runs_is_the_same_lot_after_the_move() -> None:
    # b1 is 10 sats with $0.05. s1 late-picks 3 of b1 and 2 of b2: the real b1 keeps 7 sats and $0.03,
    # the replay's (FIFO took 5) 5 sats and $0.03. The deposit then moves 5 of b1 on time in both runs:
    # the real b1@d gets $0.02, the replay's $0.03, a cent inherited from s1. s2 late-picks those 5
    # sats of b1@d, the replay's FIFO too: the same coins by the same move, so not warned
    on = date(2025, 3, 2)
    events: list[Event] = [
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("0.05")),
        Acquisition("b2", "w", date(2024, 1, 2), "buy", 10, D("0.04")),
        _pick("s1", 1, 5, (Pick("b1", 3), Pick("b2", 2))),
        Transfer("d", "w", "x", on, noon(on), "deposit", 5, picks=(Pick("b1", 5),), identified_at=noon(on)),
        Disposal("s2", "x", date(2025, 3, 3), noon(date(2025, 3, 3)), "sell", 5, D("5.00"),
                 (Pick("b1@d", 5),), LATE),
    ]  # fmt: skip
    result = run(events)
    assert [w.disposal for w in result.late] == ["s1"]
    assert [(a.lot, a.basis) for a in result.allocations if a.disposal == "s2"] == [("b1@d", D("0.02"))]


def test_the_replay_follows_an_on_time_choice_of_the_same_coins_moved_in_other_amounts() -> None:
    # x holds b0 (5 sats, the oldest). The late deposit d moves 5 of b1 and 5 of b2 to x; the replay
    # moves 10 of b1. s1 sells 5 of b1@d on time: the replay holds 10 sats of those same coins, so it
    # follows (ADR 0040). s2 late-picks b0, which is the replay's FIFO next as well: not warned
    d1, d2, d3 = date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3)
    events: list[Event] = [
        Acquisition("b0", "x", date(2023, 1, 1), "buy", 5, D("0.50")),
        Acquisition("b1", "w", date(2024, 1, 1), "buy", 10, D("1.00")),
        Acquisition("b2", "w", date(2024, 1, 2), "buy", 10, D("2.00")),
        Transfer("d", "w", "x", d1, noon(d1), "deposit", 10,
                 picks=(Pick("b1", 5), Pick("b2", 5)), identified_at=LATE),
        Disposal("s1", "x", d2, noon(d2), "sell", 5, D("5.00"), (Pick("b1@d", 5),), noon(d1)),
        Disposal("s2", "x", d3, noon(d3), "sell", 5, D("5.00"), (Pick("b0", 5),), LATE),
    ]  # fmt: skip
    result = run(events)
    assert [w.disposal for w in result.late] == ["d"]


def test_the_replay_follows_an_on_time_choice_it_can_hold() -> None:
    # s1 chooses b3 on time, and the replay follows it. s2 then chooses b2 late: the replay's FIFO is
    # b1, so s2 is warned with b1's figures (were the replay to use FIFO for s1, it would hold b2 and
    # b3, and s2 would match it)
    result = run([*THREE, sale("s1", 1, "b3", ONTIME), sale("s2", 2, "b2")])
    assert result.late == (LateIdentification("s2", LATE, (fifo("s2", 2, "b1", "1.00", 1),)),)


def test_without_late_choices_the_replay_changes_nothing() -> None:
    events: list[Event] = [*THREE, sale("s1", 1, "b3", ONTIME), sale("s2", 2, "b1", ONTIME)]
    result = run(events)
    assert result.late == () and result.replay_stopped is None
    assert [a.lot for a in result.allocations] == ["b3", "b1"]

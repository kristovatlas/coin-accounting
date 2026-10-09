"""The late choices' full replay (#238; ADR 0008 §4, ADR 0021 §2, T-508): a late choice is judged, and
its warning figured, against the standing method in a replay where every late choice is disregarded."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

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


def test_a_replay_that_cant_follow_an_event_stops_there_and_later_warnings_say_so() -> None:
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


def test_where_the_replay_falls_short_the_warning_counts_the_missing_sats() -> None:
    # wd2 names no basis: the real run creates an unknown-basis lot in x and moves it to y; the
    # replay, with x recorded, reports missing lots and moves nothing. A late sale from y choosing the
    # only lot y holds matches FIFO for the user, but the replay has nothing to sell: it is warned,
    # with no figures and 10 sats missing
    events = _diverged()
    on = date(2025, 3, 4)
    late = Disposal("s2", "y", on, noon(on), "sell", 10, D("5.00"), (Pick("wd2@@unrecorded@wd2", 10),), LATE)
    result = run([*events, late])
    assert result.replay_stopped is None
    assert result.late[-1] == LateIdentification("s2", LATE, (), 10, True)
    deposit = Transfer(
        "d2",
        "y",
        "z",
        on,
        noon(on),
        "deposit",
        10,
        picks=(Pick("wd2@@unrecorded@wd2", 10),),
        identified_at=LATE,
    )
    assert run([*events, deposit]).late[-1] == LateIdentification("d2", LATE, (), 10, True)


def test_a_late_gift_given_is_figured_in_the_replay() -> None:
    # s1 chooses b3 late (the replay sells b1). The gift then chooses b1 late: in the replay FIFO
    # gives b2, so the warning's gift record is b2's basis and date
    on = date(2025, 3, 2)
    gift = Disposal("g", "w", on, noon(on), "gift_out", 10, D("0.00"), (Pick("b1", 10),), LATE)
    result = run([*THREE, sale("s1", 1, "b3"), gift])
    record = result.late[1].standing[0]
    assert isinstance(record, engine.GiftGiven)
    assert (record.lot, record.basis, record.acquired) == ("b2", D("2.00"), date(2024, 1, 2))


@pytest.mark.parametrize("treatment", ["carry", "dispose"])
def test_a_late_transfers_fee_is_figured_on_the_replays_lot(treatment: engine.FeeTreatment) -> None:
    # s1 chooses b3 late (the replay sells b1). A deposit of 10 with a 2-sat fee then chooses b1 late:
    # in the replay FIFO moves b2 (basis 2.00). Carry: 8 sats arrive with all of b2's basis; dispose:
    # the fee is a disposal of 2 of b2's sats (basis 0.40) and 8 arrive with 1.60
    on = date(2025, 3, 2)
    deposit = Transfer(
        "d", "w", "x", on, noon(on), "deposit", 10, fee_sats=2, fee_value=D("10.00"),
        picks=(Pick("b1", 10),), identified_at=LATE,
    )  # fmt: skip
    standing = run([*THREE, sale("s1", 1, "b3"), deposit], fee_treatment=treatment).late[1].standing
    moved = [m for m in standing if isinstance(m, Moved)]
    fees = [a for a in standing if isinstance(a, Allocation)]
    assert [(m.lot, m.sats) for m in moved] == [("b2", 8)]
    if treatment == "carry":
        assert (moved[0].basis, moved[0].carried_fee, fees) == (D("2.00"), 2, [])
    else:
        assert moved[0].basis == D("1.60")
        assert [(a.lot, a.sats, a.basis, a.proceeds) for a in fees] == [("b2", 2, D("0.40"), D("10.00"))]


def test_without_late_choices_the_replay_changes_nothing() -> None:
    events: list[Event] = [*THREE, sale("s1", 1, "b3", ONTIME), sale("s2", 2, "b1", ONTIME)]
    result = run(events)
    assert result.late == () and result.replay_stopped is None
    assert [a.lot for a in result.allocations] == ["b3", "b1"]

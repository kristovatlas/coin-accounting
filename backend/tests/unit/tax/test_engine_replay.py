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
    EngineError,
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


def test_a_replay_that_cant_follow_an_event_stops_there_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive: an event valid only after the user's own choices. Simulated: the replay refuses s2.
    real = engine._Engine._dispose

    def dispose(self: engine._Engine, d: Disposal) -> None:
        if self._replay and d.id == "s2":
            raise EngineError("the replay can't follow this event")
        real(self, d)

    monkeypatch.setattr(engine._Engine, "_dispose", dispose)
    result = run([*THREE, sale("s1", 1, "b3"), sale("s2", 2, "b2"), sale("s3", 3, "b1")])
    assert result.replay_stopped == "s2"
    # s2 was still judged in the replay (b2 is its FIFO there: no warning); s3, after the stop, against
    # the user's lots: they hold b1 and b2's gone, so b1 is FIFO's and s3 isn't warned either
    assert [w.disposal for w in result.late] == ["s1"]
    assert [a.lot for a in result.allocations] == ["b3", "b2", "b1"]  # the real run is unaffected


def test_without_late_choices_the_replay_changes_nothing() -> None:
    events: list[Event] = [*THREE, sale("s1", 1, "b3", ONTIME), sale("s2", 2, "b1", ONTIME)]
    result = run(events)
    assert result.late == () and result.replay_stopped is None
    assert [a.lot for a in result.allocations] == ["b3", "b1"]

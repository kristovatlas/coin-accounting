"""The lot engine's gifts (dual basis, gifts given), signed net proceeds, and the moment checks (PLAN §7;
ADRs 0008, 0009, 0011; IRC §1015, §1223(2); T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.tax.engine import (
    Acquisition,
    Disposal,
    EngineError,
    Event,
    GiftGiven,
    Lot,
    MissingLots,
    Pick,
    UnknownBasis,
    run,
)

BTC = 100_000_000
D = Decimal
GIFT_DAY = date(2024, 3, 1)
DONOR_DAY = date(2020, 5, 1)


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def gift(basis: str | None, fmv: str, sats: int = BTC, id: str = "g") -> Acquisition:
    return Acquisition(
        id, "w", GIFT_DAY, "gift_in", sats, None if basis is None else D(basis), D(fmv), DONOR_DAY
    )


def sell(id: str, on: date, sats: int, proceeds: str, kind: str = "sell") -> Disposal:
    return Disposal(id, "w", on, at(on), kind, sats, D(proceeds))  # type: ignore[arg-type]


SOLD = date(2024, 6, 1)


@pytest.mark.parametrize(
    ("proceeds", "expected"),
    [
        # Above the donor's basis: a gain against it, held from the donor's date (tacked, IRC §1223(2)).
        ("12000.00", ("donor", "10000.00", DONOR_DAY, True, "2000.00")),
        # Below the FMV at the gift: a loss against the FMV, held from the gift date: three months.
        ("5000.00", ("fmv_at_gift", "6000.00", GIFT_DAY, False, "-1000.00")),
        # Between the two: no gain and no loss (Treas. Reg. §1.1015-1(a)(2)).
        ("8000.00", ("no_gain_or_loss", "8000.00", DONOR_DAY, True, "0.00")),
        # At either edge, the edge itself: no gain or loss.
        ("10000.00", ("no_gain_or_loss", "10000.00", DONOR_DAY, True, "0.00")),
        ("6000.00", ("no_gain_or_loss", "6000.00", DONOR_DAY, True, "0.00")),
    ],
)
def test_a_gift_with_a_lower_fmv_has_a_dual_basis_irc1015(
    proceeds: str, expected: tuple[str, str, date, bool, str]
) -> None:
    rule, basis, acquired, long_term, gain = expected
    (a,) = run([gift("10000.00", "6000.00"), sell("s", SOLD, BTC, proceeds)]).allocations
    assert (a.rule, a.basis, a.acquired, a.long_term, a.gain) == (
        rule,
        D(basis),
        acquired,
        long_term,
        D(gain),
    )
    assert a.lot_basis in (None, D("10000.00"))  # the lot gives up its own basis whatever the rule
    assert a.lot_loss_basis == D("6000.00")


def test_a_gift_with_a_higher_fmv_keeps_the_donors_basis_and_date() -> None:
    result = run([gift("5000.00", "9000.00"), sell("s", SOLD, BTC, "4000.00")])
    (a,) = result.allocations
    assert (a.rule, a.basis, a.acquired, a.long_term, a.gain) == (
        "donor",
        D("5000.00"),
        DONOR_DAY,
        True,
        D("-1000.00"),
    )
    assert a.lot_loss_basis is None


def test_a_gift_whose_fmv_equals_the_donors_basis_has_one_basis() -> None:
    # IRC §1015(a) uses the FMV for losses only when the donor's basis is *greater* than it: at equal
    # values, a loss keeps the donor's basis and date (here held more than a year, tacked).
    result = run([gift("6000.00", "6000.00"), sell("s", SOLD, BTC, "5000.00")])
    (a,) = result.allocations
    assert (a.rule, a.acquired, a.long_term, a.lot_loss_basis) == ("donor", DONOR_DAY, True, None)


def test_a_partial_sale_of_a_gift_splits_both_bases() -> None:
    result = run([gift("10000.00", "6000.00"), sell("s", SOLD, BTC // 2, "2500.00")])
    (a,) = result.allocations
    # Half the lot: gain basis 5000.00, loss basis 3000.00; 2500.00 is below 3000.00: a loss of 500.00.
    assert (a.rule, a.basis, a.gain, a.lot_basis) == ("fmv_at_gift", D("3000.00"), D("-500.00"), D("5000.00"))
    assert result.holdings == (
        Lot("g", "w", DONOR_DAY, BTC // 2, D("5000.00"), False, D("3000.00"), GIFT_DAY, False),
    )


def test_an_unknown_donor_basis_blocks_reports_t509() -> None:
    result = run([gift(None, "6000.00"), sell("s", SOLD, BTC, "7000.00")])
    assert result.blocking == (UnknownBasis("g"),)
    (a,) = result.allocations
    assert (a.rule, a.basis) == ("unknown", D("0.00"))
    assert run([gift(None, "6000.00")]).holdings[0].unknown_basis


@pytest.mark.parametrize(
    ("acquisition", "message"),
    [
        (Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00")), "names its FMV"),
        (Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00"), D("1.00")), "names its FMV"),
        (
            Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00"), D("1.00"), date(2024, 3, 2)),
            "on or before",
        ),
        (Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00"), D("1.00"), at(DONOR_DAY)), "names its FMV"),
        (Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00"), D("-1.00"), DONOR_DAY), "negative"),
        (Acquisition("b", "w", GIFT_DAY, "buy", 1, D("1.00"), D("1.00")), "only a gift"),
        (Acquisition("b", "w", GIFT_DAY, "buy", 1, D("1.00"), None, DONOR_DAY), "only a gift"),
        (Acquisition("b", "w", GIFT_DAY, "buy", 1, None), "only a gift's basis"),
    ],
)
def test_an_invalid_gift_is_refused(acquisition: Acquisition, message: str) -> None:
    with pytest.raises(EngineError, match=message):
        run([acquisition])


def test_a_gift_given_has_no_gain_and_passes_on_basis_and_date_adr0011() -> None:
    events: list[Event] = [
        Acquisition("b", "w", date(2023, 1, 1), "buy", BTC, D("10000.00")),
        sell("out", date(2024, 1, 1), BTC // 2, "0.00", kind="gift_out"),
    ]
    result = run(events)
    assert result.allocations == ()  # no Form 8949 row
    assert result.gifts == (GiftGiven("out", "b", BTC // 2, D("5000.00"), date(2023, 1, 1), None, False),)
    assert result.holdings[0].sats == BTC // 2 and result.holdings[0].basis == D("5000.00")


def test_a_gift_given_of_a_gift_passes_on_both_bases() -> None:
    result = run([gift("10000.00", "6000.00"), sell("out", SOLD, BTC // 4, "0.00", kind="gift_out")])
    assert result.gifts == (GiftGiven("out", "g", BTC // 4, D("2500.00"), DONOR_DAY, D("1500.00"), False),)


def test_a_gift_given_has_no_proceeds() -> None:
    events: list[Event] = [
        Acquisition("b", "w", date(2023, 1, 1), "buy", 1, D("1.00")),
        sell("out", date(2024, 1, 1), 1, "5.00", kind="gift_out"),
    ]
    with pytest.raises(EngineError, match="no proceeds"):
        run(events)


def test_a_gift_given_without_lots_blocks() -> None:
    assert run([sell("out", date(2024, 1, 1), 5, "0.00", kind="gift_out")]).blocking == (
        MissingLots("out", 5, D("0.00")),
    )


def test_net_proceeds_can_be_negative_adr0009() -> None:
    # A spend whose network fee is more than the value it moved: net proceeds below zero.
    events: list[Event] = [
        Acquisition("b", "w", date(2024, 1, 1), "buy", 1000, D("10.00")),
        sell("s", date(2024, 2, 1), 1000, "-2.00", kind="spend"),
    ]
    (a,) = run(events).allocations
    assert (a.proceeds, a.gain) == (D("-2.00"), D("-12.00"))


def test_negative_proceeds_split_across_lots_add_up() -> None:
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    result = run([*events, sell("s", date(2024, 2, 1), 3, "-0.10", kind="spend")])
    assert [a.proceeds for a in result.allocations] == [D("-0.03"), D("-0.04"), D("-0.03")]


# The moment checks (#239): a disposal's tax date agrees with its moment, and moments are in order.


def _sale(on: date, moment: datetime, id: str = "s") -> Disposal:
    return Disposal(id, "w", on, moment, "sell", 1, D("1.00"))


BOUGHT = Acquisition("b", "w", date(2025, 1, 1), "buy", 10, D("10.00"))


@pytest.mark.parametrize(
    "moment",
    [
        at(date(2025, 3, 1), 0),
        at(date(2025, 2, 28), 23),  # a time zone ahead of UTC: still 1 March there
        at(date(2025, 3, 2), 9),  # a time zone behind UTC
    ],
)
def test_a_tax_date_within_a_day_of_the_moment_is_accepted(moment: datetime) -> None:
    assert len(run([BOUGHT, _sale(date(2025, 3, 1), moment)]).allocations) == 1


@pytest.mark.parametrize("moment", [at(date(2025, 3, 3)), at(date(2025, 2, 27)), at(date(2026, 3, 1))])
def test_a_tax_date_far_from_the_moment_is_refused_t508(moment: datetime) -> None:
    with pytest.raises(EngineError, match="more than a day"):
        run([BOUGHT, _sale(date(2025, 3, 1), moment)])


def test_disposals_must_be_in_the_order_of_their_moments() -> None:
    day = date(2025, 3, 1)
    with pytest.raises(EngineError, match="out of time order"):
        run([BOUGHT, _sale(day, at(day, 15), "s1"), _sale(day, at(day, 9), "s2")])


def test_a_moment_must_be_in_utc_itself() -> None:
    gmt = datetime(2025, 3, 1, 12, tzinfo=timezone(timedelta(0), "GMT"))  # zero offset, but not UTC
    with pytest.raises(EngineError, match="UTC"):
        run([BOUGHT, _sale(date(2025, 3, 1), gmt)])


def test_fifo_skips_a_later_lot_a_choice_used_up() -> None:
    events: list[Event] = [
        Acquisition(f"b{i}", "w", date(2024, 1, i), "buy", 1, D("1.00")) for i in (1, 2, 3)
    ]
    choice = Disposal(
        "s1",
        "w",
        date(2024, 2, 1),
        at(date(2024, 2, 1)),
        "sell",
        1,
        D("1.00"),
        (Pick("b2", 1),),
        at(date(2024, 2, 1)),
    )
    result = run([*events, choice, sell("s2", date(2024, 2, 2), 2, "1.00")])
    assert [(a.disposal, a.lot) for a in result.allocations] == [("s1", "b2"), ("s2", "b1"), ("s2", "b3")]


# Property: for any gift and any proceeds, the dual-basis rule picks exactly one region (IRC §1015).


@settings(max_examples=300, deadline=None)
@given(
    donor=st.integers(0, 10**8),
    fmv=st.integers(0, 10**8),
    proceeds=st.integers(-(10**6), 2 * 10**8),
    sold=st.integers(1, BTC),
)
def test_the_dual_basis_rule_holds_for_any_gift(donor: int, fmv: int, proceeds: int, sold: int) -> None:
    g = Acquisition("g", "w", GIFT_DAY, "gift_in", BTC, D(donor).scaleb(-2), D(fmv).scaleb(-2), DONOR_DAY)
    result = run([g, sell("s", SOLD, sold, str(D(proceeds).scaleb(-2)))])
    (a,) = result.allocations
    gain_basis = a.lot_basis if a.lot_basis is not None else a.basis
    if a.lot_loss_basis is None:
        assert a.rule == "donor" and a.basis == gain_basis
    elif a.proceeds > gain_basis:
        assert a.rule == "donor" and a.gain > 0
    elif a.proceeds < a.lot_loss_basis:
        assert a.rule == "fmv_at_gift" and a.gain < 0 and a.acquired == GIFT_DAY
    else:
        assert a.rule == "no_gain_or_loss" and a.gain == 0
    held = result.holdings[0].basis if result.holdings else D("0.00")
    assert gain_basis + held == D(donor).scaleb(-2)  # the lot's own basis is conserved


@pytest.mark.parametrize("proceeds", ["1.001", "-1000000000000000"])
def test_invalid_proceeds_are_refused(proceeds: str) -> None:
    with pytest.raises(EngineError, match="event 's'"):
        run([BOUGHT, Disposal("s", "w", date(2025, 3, 1), at(date(2025, 3, 1)), "sell", 1, D(proceeds))])

"""The lot engine's gifts (dual basis, gifts given), signed net proceeds, and the moment checks (PLAN §7;
ADRs 0008, 0009, 0011; IRC §1015, §1223(2); T-502, T-508, T-509). Every figure here is worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
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
    # The lot gives up its own 10000.00 whatever the rule; `lot_basis` is set where it differs from `basis`.
    assert a.lot_basis == (None if D(basis) == D("10000.00") else D("10000.00"))
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
            "from genesis",
        ),
        (Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00"), D("1.00"), at(DONOR_DAY)), "names its FMV"),
        (
            Acquisition("g", "w", GIFT_DAY, "gift_in", 1, D("1.00"), D("1.00"), date(2009, 1, 2)),
            "from genesis",
        ),
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
    assert result.gifts == (GiftGiven("out", "b", BTC // 2, D("5000.00"), date(2023, 1, 1), False),)
    assert result.holdings[0].sats == BTC // 2 and result.holdings[0].basis == D("5000.00")


def test_a_gift_given_of_a_gift_passes_on_the_donors_basis_only_irc1015() -> None:
    # The recipient's loss limit uses the FMV at *this* gift (the caller's), never the FMV at the gift
    # the user received: $10,000 donor basis, old FMV $6,000, re-gifted when worth more is one basis.
    result = run([gift("10000.00", "6000.00"), sell("out", SOLD, BTC // 4, "0.00", kind="gift_out")])
    assert result.gifts == (GiftGiven("out", "g", BTC // 4, D("2500.00"), DONOR_DAY, False),)
    assert result.holdings[0].loss_basis == D("4500.00")  # the user's own loss basis stays with the lot


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
    if a.lot_loss_basis is None:  # FMV not below the donor's basis: one basis
        assert a.rule == "donor" and a.basis == gain_basis and a.acquired == DONOR_DAY
    elif a.proceeds > gain_basis:
        assert a.rule == "donor" and a.gain > 0 and a.acquired == DONOR_DAY
    elif a.proceeds < a.lot_loss_basis:
        assert a.rule == "fmv_at_gift" and a.gain < 0 and a.acquired == GIFT_DAY
    else:
        assert a.rule == "no_gain_or_loss" and a.gain == 0 and a.acquired == DONOR_DAY
    held = result.holdings[0].basis if result.holdings else D("0.00")
    assert gain_basis + held == D(donor).scaleb(-2)  # the lot's own basis is conserved


@pytest.mark.parametrize("proceeds", ["1.001", "-1000000000000000"])
def test_invalid_proceeds_are_refused(proceeds: str) -> None:
    with pytest.raises(EngineError, match="event 's'"):
        run([BOUGHT, Disposal("s", "w", date(2025, 3, 1), at(date(2025, 3, 1)), "sell", 1, D(proceeds))])


def test_a_gift_given_can_name_its_lots_and_a_late_one_warns_with_gift_records() -> None:
    events: list[Event] = [
        Acquisition("b1", "w", date(2023, 1, 1), "buy", 10, D("1.00")),
        Acquisition("b2", "w", date(2023, 2, 1), "buy", 10, D("3.00")),
    ]
    day = date(2024, 1, 1)
    late = Disposal("out", "w", day, at(day), "gift_out", 10, D("0.00"), (Pick("b2", 10),), at(day, 18))
    result = run([*events, late])
    assert result.gifts == (GiftGiven("out", "b2", 10, D("3.00"), date(2023, 2, 1), False),)
    (warning,) = result.late
    # FIFO would have given b1 away: a gift record, never a taxable "sale for 0.00" (ADR 0009).
    assert warning.standing == (GiftGiven("out", "b1", 10, D("1.00"), date(2023, 1, 1), False),)


@pytest.mark.parametrize(
    "amount", [D("1." + "0" * 100), D("1.000000000000000000000000000000000000000000000000000000000000")]
)
def test_long_decimals_with_trailing_zeros_are_exact_cents_in_the_engine(amount: Decimal) -> None:
    (lot,) = run([Acquisition("b", "w", date(2024, 1, 1), "buy", 1, amount)]).holdings
    assert lot.basis == D("1.00")


@pytest.mark.parametrize("amount", [D("1." + "0" * 59 + "1"), D("1" + "0" * 70)])
def test_long_decimals_that_arent_cents_are_refused_as_engine_errors_t502(amount: Decimal) -> None:
    with pytest.raises(EngineError):
        run([Acquisition("b", "w", date(2024, 1, 1), "buy", 1, amount)])


# Property: however a dust-sized gift with a lower FMV is cut up, every part keeps a loss basis no
# higher than its gain basis, and only a loss below it takes the gift date (IRC §1015(a)).


@settings(max_examples=300, deadline=None)
@given(
    donor=st.integers(1, 500),
    gap=st.integers(1, 5),
    sats=st.integers(2, 40),
    cuts=st.lists(st.integers(1, 5), min_size=1, max_size=12),
    cents=st.integers(-50, 600),
)
def test_a_dust_split_gift_keeps_its_dual_basis(
    donor: int, gap: int, sats: int, cuts: list[int], cents: int
) -> None:
    fmv = max(donor - gap, 0)
    events: list[Event] = [
        Acquisition("g", "w", GIFT_DAY, "gift_in", sats, D(donor).scaleb(-2), D(fmv).scaleb(-2), DONOR_DAY)
    ]
    left = sats
    for i, cut in enumerate(cuts):
        take = min(cut, left)
        if take == 0:
            break
        events.append(sell(f"s{i}", SOLD + timedelta(days=i), take, str(D(cents).scaleb(-2))))
        left -= take
    result = run(events)
    for a in result.allocations:
        # The FMV was lower at the gift, so every part keeps a loss basis no higher than its gain basis.
        assert a.lot_loss_basis is not None and a.lot_loss_basis <= (
            a.lot_basis if a.lot_basis is not None else a.basis
        )
        if a.rule == "fmv_at_gift":
            assert a.acquired == GIFT_DAY and a.proceeds < a.lot_loss_basis
        else:
            assert a.acquired == DONOR_DAY
    used = sum((a.lot_basis if a.lot_basis is not None else a.basis) for a in result.allocations)
    held = result.holdings[0].basis if result.holdings else D("0.00")
    assert used + held == D(donor).scaleb(-2)  # the gain basis is conserved


def test_a_gift_keeps_its_loss_basis_when_rounded_shares_meet_irc1015() -> None:
    # Donor basis 0.11, FMV 0.10, 4 sats; one sold: shares 0.03 and 0.02, and what is left is 0.08 and
    # 0.08. In law the FMV was lower at the gift, so a loss on the rest is still against the FMV and
    # held from the gift date (Treas. Reg. §1.1223-1(b)): rounding doesn't make it one basis.
    events: list[Event] = [gift("0.11", "0.10", sats=4), sell("s", SOLD, 1, "0.00")]
    result = run(events)
    assert result.holdings == (Lot("g", "w", DONOR_DAY, 3, D("0.08"), False, D("0.08"), GIFT_DAY, False),)
    later = run([*events, sell("s2", SOLD + timedelta(days=1), 3, "0.00")])
    (a,) = [a for a in later.allocations if a.disposal == "s2"]
    assert (a.rule, a.acquired, a.long_term) == ("fmv_at_gift", GIFT_DAY, False)


# Property: any mix of buys, gifts received, sales and gifts given conserves every lot's sats and gain
# basis across sales, gifts given and holdings (T-502).


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_any_mix_with_gifts_conserves_sats_and_basis(data: st.DataObject) -> None:
    events: list[Event] = []
    start = date(2021, 1, 1).toordinal()
    for i in range(data.draw(st.integers(0, 16))):
        on = date.fromordinal(start + 2 * i)
        sats = data.draw(st.integers(1, 2 * BTC))
        cents = D(data.draw(st.integers(0, 10**8))).scaleb(-2)
        kind = data.draw(st.sampled_from(["buy", "gift_in", "sell", "gift_out"]))
        if kind == "buy":
            events.append(Acquisition(f"e{i}", "w", on, "buy", sats, cents))
        elif kind == "gift_in":
            fmv = D(data.draw(st.integers(0, 10**8))).scaleb(-2)
            events.append(Acquisition(f"e{i}", "w", on, "gift_in", sats, cents, fmv, DONOR_DAY))
        elif kind == "sell":
            events.append(sell(f"e{i}", on, sats, str(cents)))
        else:
            events.append(sell(f"e{i}", on, sats, "0.00", kind="gift_out"))
    result = run(events)
    lots = [e for e in events if isinstance(e, Acquisition)]
    sold = sum(a.sats for a in result.allocations)
    given = sum(g.sats for g in result.gifts)
    held = sum(h.sats for h in result.holdings)
    assert sum(lot.sats for lot in lots) == sold + given + held
    basis_used = sum((a.lot_basis if a.lot_basis is not None else a.basis) for a in result.allocations)
    basis_given = sum(g.basis for g in result.gifts)
    basis_held = sum(h.basis for h in result.holdings)
    assert sum((lot.basis or D("0.00")) for lot in lots) == basis_used + basis_given + basis_held
    for h in result.holdings:  # a lot's loss basis, when it has one, is never above its gain basis
        assert h.loss_basis is None or h.loss_basis <= h.basis


def test_a_gift_of_an_inherited_lot_can_carry_its_long_term_status_irc1223() -> None:
    # The donor inherited the coins two months before the gift: the caller says the lot counted as
    # long-term in the donor's hands (§1223(9)); with tacking (§1223(2)) a sale soon after is long-term.
    inherited = Acquisition(
        "g",
        "w",
        GIFT_DAY,
        "gift_in",
        BTC,
        D("5000.00"),
        D("9000.00"),
        date(2024, 1, 1),
        donor_always_long=True,
    )
    (a,) = run([inherited, sell("s", SOLD, BTC, "9500.00")]).allocations
    assert a.long_term
    plain = Acquisition("g", "w", GIFT_DAY, "gift_in", BTC, D("5000.00"), D("9000.00"), date(2024, 1, 1))
    (b,) = run([plain, sell("s", SOLD, BTC, "9500.00")]).allocations
    assert not b.long_term  # without it: held from 1 January, five months


def test_a_gift_given_of_an_inherited_lot_says_so() -> None:
    events: list[Event] = [
        Acquisition("i", "w", date(2024, 5, 1), "inherit", BTC, D("60000.00")),
        sell("out", date(2024, 6, 1), BTC, "0.00", kind="gift_out"),
    ]
    assert run(events).gifts == (GiftGiven("out", "i", BTC, D("60000.00"), date(2024, 5, 1), False, True),)


def test_only_a_gift_can_carry_a_donors_long_term_status() -> None:
    with pytest.raises(EngineError, match="only a gift"):
        run([Acquisition("b", "w", GIFT_DAY, "buy", 1, D("1.00"), donor_always_long=True)])


def test_an_inherited_gift_loss_against_the_fmv_is_held_from_the_gift_date() -> None:
    # Donor basis 10000, FMV 6000, inherited by the donor; sold for 5000 three months after the gift:
    # a loss against the FMV, held from the gift date (Treas. Reg. §1.1223-1(b)), so short-term even
    # though the donor's lot counted as long-term.
    g = Acquisition(
        "g", "w", GIFT_DAY, "gift_in", BTC, D("10000.00"), D("6000.00"), DONOR_DAY, donor_always_long=True
    )
    (a,) = run([g, sell("s", SOLD, BTC, "5000.00")]).allocations
    assert (a.rule, a.acquired, a.long_term, a.gain) == ("fmv_at_gift", GIFT_DAY, False, D("-1000.00"))
    (b,) = run([g, sell("s", SOLD, BTC, "12000.00")]).allocations
    assert (b.rule, b.long_term) == ("donor", True)  # the donor's basis: the status tacks


@pytest.mark.parametrize("flag", [1, "yes", None])
def test_donor_always_long_is_a_bool(flag: Any) -> None:  # Any: deliberately not a bool
    g = Acquisition(
        "g", "w", GIFT_DAY, "gift_in", BTC, D("1.00"), D("1.00"), DONOR_DAY, donor_always_long=flag
    )
    with pytest.raises(EngineError, match="True or False"):
        run([g])

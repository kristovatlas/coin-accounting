"""Report builders: pure functions from the engine's result to report rows (PLAN §8; ADR 0011, ADR 0040,
ADR 0043; T-510). They return values; the CSV writing and the input hash are the caller's. The free text
in them (`Row.override_reason`, `Row.disposal` and `Row.lot`, `Row.review`, `BrokerFee.description`,
`IncomeRow.event` and `IncomeRow.source`, `Holding.lot` and `Holding.account`, every `replay_stopped`,
the ids in every `late`, and whatever the caller passes as `blocking`) is the user's or an import's: the
CSV writer must escape it against formula injection (T-702). The caller also adds what every report
embeds besides the rule set and the stated positions (PLAN §8, T-507): the app version, the settings,
the unsupported situations it doesn't model, and the input hash. A blocking condition of any year blocks
every year's report (PLAN §7).

**Form 8949** (`form_8949`): one row per allocation of a sale or spend in the tax year, the engine's
lot-by-lot figures. A gift given never has a row, and neither does a fee: the engine makes no
allocation for either (ADR 0011, ADR 0043). The box is chosen per disposal from the tax year, whether
a Form 1099-B (2024 and earlier) or 1099-DA (2025 and later) reports it, and whether that form reports
its basis (`tax/rules`), or the user's override, whose reason the row carries. The caller says how each
disposal was made (`Sale`); `Sale.default` gives ADR 0011's defaults:
- an on-chain disposal (from a wallet, or sent straight from an exchange with no 1099-DA reporting it,
  ADR 0043): not reported, so C/F through 2024 and I/L from 2025
- an exchange sale: from 2025 reported without basis, H/K, including coins deposited from outside,
  which brokers treat as noncovered; G/J only for covered coins (bought in that exchange account from
  2026-01-01, a buy in that account), when the caller marks a reported basis; through 2024, C/F unless a
  1099-B reported it

A row of an inherited lot reads "INHERITED" in column (b) instead of a date (`Row.inherited`).

**Income** (`income_summary`): each `income` acquisition in the tax year, with its USD FMV at receipt
(its basis, which is also ordinary income) and the source the caller names (PLAN §8).

**Year summary** (`year_summary`): the tax year's realized short- and long-term proceeds, basis and
gain, from the same allocations as Form 8949. **Holdings** (`holdings`): the lots an engine run up to a
date left, each with its basis, its holding periods on that date and, given a price, its value and
unrealized gain (a gift's by its dual basis: a gain over the donor's basis, a loss under the FMV at the
gift with the gift date's holding period, none between). Every one of them is blocked as Form 8949 is,
and carries the rule set, the stated positions, the late warnings and a stopped replay.

**Blocked:** a report isn't built while anything blocks (ADR 0009, ADR 0041 §3, ADR 0043; PLAN §7):
the engine's blocking conditions (unknown basis, missing lots), and those only the caller knows of,
which it must pass (`blocking`): coin tracing's blocks until the engine traces wallets, unconfirmed
transactions and the reorg review queue (T-207, T-506), ADR 0043's unsupported fee cases. `Blocked`
lists them all. A late lot identification only warns (T-508): its rows are flagged, with whether the
replay judged it (ADR 0040); the report lists every late warning, a transfer's too, since a late
withdrawal choice shapes later rows; and a stopped late-choice replay is shown.

**Review flags:** where the caller gives a disposal's broker figures (`Broker`: what its 1099-B or
1099-DA reports), the rows are flagged when the proceeds, basis or acquisition date differ from the
app's (ADR 0011; Notice 2026-20), which covers an exchange sale whose fee was taken in BTC (its gross
proceeds differ from the broker's net). A fee the broker lists as a sale of its own (`BrokerFee`) is
listed for review (ADR 0043). Rule sets cover 2013 to 2026; other years raise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from coinacct.domain.chain import SATS_PER_BTC
from coinacct.domain.money import add, signed_usd, subtract, usd, value_of
from coinacct.tax.engine import (
    Acquisition,
    Allocation,
    EngineError,
    Event,
    LateIdentification,
    Result,
    long_term,
)
from coinacct.tax.rules import BOXES, RULESET, Box, box, rules_for, y2024

type Channel = Literal["exchange", "onchain"]

SHORT_BOXES: Final[frozenset[str]] = frozenset("ABCGHI")
BASIS_BOXES: Final[frozenset[str]] = frozenset("ADGJ")  # the boxes of sales a form reports with basis
ZERO: Final = Decimal("0.00")
COVERED_FROM: Final = date(2026, 1, 1)  # a digital asset bought from then is covered (ADR 0011)
# The stated tax positions every report prints (PLAN §8), by the ADR that records each
POSITIONS: Final[tuple[str, ...]] = (
    "ADR 0008: lots are identified per account; in a self-custody wallet the coins spent identify them.",
    "ADR 0009: a gift received has a dual basis; an unknown basis blocks reports until entered.",
    "ADR 0040: a late lot choice is judged against a full replay with every late choice disregarded.",
    "ADR 0041: in a coin-traced self-custody wallet, the coins a transaction spends identify its lots,"
    " oldest first inside a coin; an account that isn't coin-traced is figured by its FIFO order.",
    "ADR 0043: a network fee, or a BTC fee an exchange takes, is never a taxable event and has no Form"
    " 8949 row of its own.",
    "ADR 0043, sales and spends: Treasury's 2024 broker regulations charge a sale's transaction costs to"
    " the sale (Treas. Reg. §1.1001-7(b)(2)), and their basis rules' example treats other digital assets"
    " used to pay such costs as themselves disposed of (§1.1012-1(h)). Instead, the app adds the fee's"
    " basis to the sale and reports no separate row. For a lot with one basis the total gain is the"
    " same; only the separate fee row is missing. It can differ when the fee's basis joins another lot's"
    " row (it takes that row's holding period) or comes from a gift lot with two bases (the lower basis"
    " moves).",
    "ADR 0043, buys: a BTC fee line tied to a buy is treated as withheld from the coins bought, so it"
    " takes no older lot.",
    "ADR 0043, transfers, deposits and withdrawals: no guidance squarely addresses a fee when bitcoin moves"
    " between one's own accounts. The app treats it as a non-taxable cost: its basis stays with the"
    " coins. When the fee's lot has no row or fragment at the destination, its basis joins the oldest one"
    " and takes that holding period.",
    "ADR 0043, gifts: the fee's cost isn't recovered.",
    "ADR 0043, brokers: a broker's proceeds on Form 1099-DA are net of transaction costs, and a 1099-DA may"
    " list a fee as a sale. On an exchange sale with a fee taken in BTC, the app's proceeds are gross,"
    " with the fee's basis added instead. The total gain is the same for a one-basis lot, but the row's"
    " proceeds and basis differ from the broker's, and the report's comparison with broker figures flags"
    " each such row, and any fee the broker lists, for review.",
)


@dataclass(frozen=True)
class Broker:
    """What a Form 1099-B or 1099-DA reports for a disposal, as the user entered or imported it: its
    proceeds and, where reported, its basis and acquisition date. The report flags the disposal's rows
    where these differ from the app's figures (ADR 0011; Notice 2026-20 lets the two differ)."""

    proceeds: Decimal
    basis: Decimal | None = None
    acquired: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.proceeds, Decimal) or not isinstance(self.basis, Decimal | None):
            raise EngineError("a broker's figures are USD Decimals")
        try:
            signed_usd(self.proceeds)
            if self.basis is not None:
                usd(self.basis)
        except (TypeError, ValueError) as e:
            raise EngineError(f"a broker's figures are USD Decimals: {e}") from None
        if self.acquired is not None and type(self.acquired) is not date:
            raise EngineError("a broker's acquisition date is a date")


@dataclass(frozen=True)
class BrokerFee:
    """A fee a broker's 1099-DA lists as a sale of its own (ADR 0043): no row of the app's, so the
    report lists it for review."""

    description: str
    sold: date
    proceeds: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or type(self.sold) is not date:
            raise EngineError("a broker's fee row has its description and date")
        if not isinstance(self.proceeds, Decimal):
            raise EngineError("a broker's figures are USD Decimals")
        try:
            signed_usd(self.proceeds)
        except ValueError as e:
            raise EngineError(f"a broker's figures are USD Decimals: {e}") from None


@dataclass(frozen=True)
class Sale:
    """How a disposal was made, for its box: whether a 1099-B or 1099-DA reports it (`reported`) and
    its basis (`basis_reported`). `override_short`/`override_long` replace the box of its short- and
    long-term rows, with the user's `reason` (ADR 0011)."""

    channel: Channel
    reported: bool
    basis_reported: bool = False
    override_short: Box | None = None
    override_long: Box | None = None
    reason: str | None = None
    broker: Broker | None = None  # the form's figures, when the user has them

    def __post_init__(self) -> None:
        if self.channel not in ("exchange", "onchain"):
            raise EngineError(f"a sale's channel is exchange or onchain, not {self.channel!r}")
        if self.basis_reported and not self.reported:
            raise EngineError("basis is reported only on a form that reports the sale")
        if self.reason is not None and not isinstance(self.reason, str):
            raise EngineError("an override's reason is text")
        if type(self.reported) is not bool or type(self.basis_reported) is not bool:
            raise EngineError("a sale's reported and basis_reported are True or False")
        if self.broker is not None and (not isinstance(self.broker, Broker) or not self.reported):
            raise EngineError("broker figures are a Broker, for a sale a form reports")
        if self.broker is not None and self.broker.basis is not None and not self.basis_reported:
            raise EngineError("a broker's basis is given only for a form that reports basis")
        overridden = self.override_short is not None or self.override_long is not None
        if overridden != (isinstance(self.reason, str) and bool(self.reason.strip())):
            raise EngineError("an overridden box records the user's reason, and only then")

    @staticmethod
    def default(channel: Channel, tax_year: int) -> Sale:
        """ADR 0011's default: an exchange sale is reported (without basis) from 2025; an on-chain
        disposal, and an exchange sale through 2024, aren't. (An on-chain sale sent straight from an
        exchange that a 1099-DA reports anyway is `Sale("onchain", reported=True)`: ADR 0043.)"""
        rules_for(tax_year)
        return Sale(channel, reported=channel == "exchange" and tax_year > y2024.LAST_YEAR)


@dataclass(frozen=True)
class Row:
    """A Form 8949 row: (a) `description`, (b) `acquired` ("INHERITED" where `inherited`), (c) `sold`,
    (d) `proceeds`, (e) `basis`, (f)/(g) none (no adjustments are made), (h) `gain`; with its box, term
    and the allocation it is."""

    box: Box
    description: str
    acquired: date
    sold: date
    proceeds: Decimal
    basis: Decimal
    gain: Decimal
    long_term: bool
    disposal: str
    lot: str
    late: bool = False  # the disposal's lot choice was late and differs from the standing method's
    late_replayed: bool | None = None  # judged in the replay (False: after it stopped, ADR 0040)
    override_reason: str | None = None
    inherited: bool = False  # (b) reads "INHERITED" (Instructions for Form 8949, column (b))
    review: tuple[str, ...] = ()  # why to check the row against the broker's figures


@dataclass(frozen=True)
class Total:
    """A box's totals, for Schedule D (the sum of its rows)."""

    box: Box
    rows: int
    proceeds: Decimal
    basis: Decimal
    gain: Decimal


@dataclass(frozen=True)
class Form8949:
    tax_year: int
    rows: tuple[Row, ...]
    totals: tuple[Total, ...]
    replay_stopped: str | None  # the event the late-choice replay stopped at (ADR 0040), or None
    late: tuple[LateIdentification, ...] = ()  # every late warning of the run, any year's, a transfer's too
    broker_fees: tuple[BrokerFee, ...] = ()  # the year's fees the broker lists as sales: for review
    ruleset: str = RULESET
    positions: tuple[str, ...] = POSITIONS


@dataclass(frozen=True)
class Blocked:
    """No report: the blocking conditions, which the user resolves first."""

    blocking: tuple[object, ...]


def form_8949(
    result: Result,
    sales: Mapping[str, Sale],
    tax_year: int,
    *,
    blocking: Sequence[object],
    broker_fees: Sequence[BrokerFee] = (),
) -> Form8949 | Blocked:
    """The tax year's Form 8949 rows and box totals, or why it can't be built: the engine's blocking
    conditions and the caller's (`blocking`, see the module docstring)."""
    rules_for(tax_year)
    if not isinstance(blocking, tuple | list):
        raise EngineError("blocking is a list or tuple of the caller's blocking conditions")
    if result.blocking or blocking:
        return Blocked((*result.blocking, *blocking))
    this_year = {a.disposal for a in result.allocations if a.disposed.year == tax_year}
    for disposal, sale in sales.items():
        if not isinstance(sale, Sale):
            raise EngineError(f"disposal {disposal!r}: its facts are a Sale")
        if disposal in this_year:  # another year's sale is checked in that year's report
            _check_overrides(disposal, sale, tax_year)
    late = {w.disposal: w for w in result.late}
    rows: list[Row] = []
    for a in result.allocations:
        if a.disposed.year != tax_year:
            continue
        facts = sales.get(a.disposal)
        if facts is None:
            raise EngineError(f"disposal {a.disposal!r}: a sale's channel and forms are needed for its box")
        rows.append(_row(a, facts, tax_year, late.get(a.disposal)))
    rows = _reviewed(rows, sales)
    rows.sort(key=lambda r: (r.box, r.sold, r.disposal, r.acquired, r.lot))
    if not isinstance(broker_fees, tuple | list) or not all(isinstance(f, BrokerFee) for f in broker_fees):
        raise EngineError("a broker's fee rows are a list or tuple of BrokerFees")
    fees = tuple(f for f in broker_fees if f.sold.year == tax_year)
    return Form8949(tax_year, tuple(rows), _totals(rows), result.replay_stopped, tuple(result.late), fees)


def describe(sats: int) -> str:
    """Form 8949's (a): the amount and the property, "0.12345678 BTC" (from the integer sats, so no
    Decimal context can round it)."""
    whole, rest = divmod(sats, SATS_PER_BTC)
    return f"{whole}.{rest:08d} BTC"


def _row(a: Allocation, sale: Sale, tax_year: int, late: LateIdentification | None) -> Row:
    override = sale.override_long if a.long_term else sale.override_short
    # a 1099-B through 2024 reports basis as it does; from 2025 a 1099-DA only for covered coins
    covered = sale.basis_reported and (tax_year <= y2024.LAST_YEAR or _covered(a))
    if override is None:
        chosen, reason = box(tax_year, a.long_term, sale.reported, covered), None
    else:
        chosen, reason = override, sale.reason
    return Row(
        chosen,
        describe(a.sats),
        a.acquired,
        a.disposed,
        a.proceeds,
        a.basis,
        a.gain,
        a.long_term,
        a.disposal,
        a.lot,
        late is not None,
        None if late is None else late.replayed,
        reason,
        a.inherited,
    )


def _covered(a: Allocation) -> bool:
    """Whether a row's lot can be a covered security, whose basis a 1099-DA reports: bought in that
    exchange account from 2026-01-01 (Treas. Reg. §1.6045-1(a)(15)(i)(J); ADR 0011). A lot moved in
    from another account, inherited, given, received as income or bought peer to peer is not."""
    return a.bought and a.acquired >= COVERED_FROM


def _check_overrides(disposal: str, sale: Sale, tax_year: int) -> None:
    """Both of a sale's overrides, whether or not it has rows of that term."""
    for chosen, is_long in ((sale.override_short, False), (sale.override_long, True)):
        if chosen is None:
            continue
        if not isinstance(chosen, str) or chosen not in BOXES or (chosen in SHORT_BOXES) == is_long:
            term = "long" if is_long else "short"
            raise EngineError(f"disposal {disposal!r}: box {chosen!r} isn't one for its {term}-term rows")
        if (chosen in "ABCDEF") != (tax_year <= y2024.LAST_YEAR):
            year = f"{tax_year}"
            raise EngineError(
                f"disposal {disposal!r}: box {chosen!r} isn't used for digital assets in {year}"
            )


def _reviewed(rows: list[Row], sales: Mapping[str, Sale]) -> list[Row]:
    """Flag the rows of each disposal whose broker figures differ from the app's: its proceeds in all,
    its basis in all where reported, and its acquisition date where reported (any row of another date
    differs) (ADR 0011, PLAN §8)."""
    by: dict[str, list[Row]] = {}
    for r in rows:
        by.setdefault(r.disposal, []).append(r)
    out: list[Row] = []
    for disposal, its in by.items():
        broker = sales[disposal].broker
        flags: list[str] = []
        if sales[disposal].basis_reported and not all(r.box in BASIS_BOXES for r in its):
            flags.append("the form reports basis, but these coins aren't covered: check the broker's box")
        if broker is not None:
            if _sum(r.proceeds for r in its) != broker.proceeds:
                flags.append("proceeds differ from the broker's")
            if broker.basis is not None and _sum(r.basis for r in its) != broker.basis:
                # Form 8949 instructions, columns (f) and (g): an incorrect reported basis takes code B
                flags.append("basis differs from the broker's: Form 8949 needs code B and the adjustment")
            if broker.acquired is not None and {r.acquired for r in its} != {broker.acquired}:
                flags.append("acquisition date differs from the broker's")
        out.extend(replace(r, review=tuple(flags)) for r in its)
    return out


def _sum(values: Iterable[Decimal]) -> Decimal:
    total = ZERO
    for v in values:
        total = add(total, v)
    return total


def _totals(rows: list[Row]) -> tuple[Total, ...]:
    out: dict[str, Total] = {}
    for r in rows:
        t = out.get(r.box, Total(r.box, 0, ZERO, ZERO, ZERO))
        out[r.box] = Total(
            r.box, t.rows + 1, add(t.proceeds, r.proceeds), add(t.basis, r.basis), add(t.gain, r.gain)
        )
    return tuple(out[b] for b in sorted(out))


@dataclass(frozen=True)
class IncomeRow:
    event: str
    received: date
    sats: int
    fmv: Decimal  # USD at receipt: ordinary income, and the lot's basis
    source: str | None  # who paid it (an employer, a pool), as the caller names it


@dataclass(frozen=True)
class IncomeSummary:
    tax_year: int
    rows: tuple[IncomeRow, ...]
    total: Decimal
    replay_stopped: str | None = None
    late: tuple[LateIdentification, ...] = ()  # every late warning: the report shows them all
    ruleset: str = RULESET
    positions: tuple[str, ...] = POSITIONS


def income_summary(
    events: Sequence[Event],
    result: Result,
    tax_year: int,
    *,
    blocking: Sequence[object],
    sources: Mapping[str, str] | None = None,
) -> IncomeSummary | Blocked:
    """The tax year's `income` acquisitions, by date, with their FMV and total (PLAN §8), or why it can't
    be built: `result` is the engine's run of every event (`events`, checked), whose blocking conditions
    of any year block it, as the caller's do (`blocking`, as for `form_8949`). `sources` names who paid
    each income event; a row the caller names no source for has none (`source` None)."""
    rules_for(tax_year)
    _check_blocking(blocking)
    _check_run(result, events)
    named = {} if sources is None else sources
    income = [e for e in result.events if isinstance(e, Acquisition) and e.kind == "income"]
    if not isinstance(named, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in named.items()
    ):
        raise EngineError("sources maps income events to the names of who paid them")
    if not set(named) <= {e.id for e in income}:
        raise EngineError(
            f"sources names events that aren't income: {sorted(set(named) - {e.id for e in income})}"
        )
    if result.blocking or blocking:
        return Blocked((*result.blocking, *blocking))
    rows = [
        IncomeRow(e.id, e.on, e.sats, _usd(e.basis, e.id), named.get(e.id))
        for e in income
        if e.on.year == tax_year and e.basis is not None  # the engine refuses income without its FMV
    ]
    rows.sort(key=lambda r: (r.received, r.event))
    return IncomeSummary(
        tax_year, tuple(rows), _sum(r.fmv for r in rows), result.replay_stopped, tuple(result.late)
    )


@dataclass(frozen=True)
class Term:
    rows: int
    proceeds: Decimal
    basis: Decimal
    gain: Decimal


@dataclass(frozen=True)
class YearSummary:
    tax_year: int
    short: Term
    long: Term
    replay_stopped: str | None = None
    late: tuple[LateIdentification, ...] = ()  # every late warning, as on Form 8949
    ruleset: str = RULESET
    positions: tuple[str, ...] = POSITIONS


def year_summary(
    events: Sequence[Event], result: Result, tax_year: int, *, blocking: Sequence[object]
) -> YearSummary | Blocked:
    """The tax year's realized short- and long-term totals, from its sales' and spends' allocations, or
    why it can't be built (the engine's blocking conditions and the caller's, as for `form_8949`).
    `result` is the engine's run of every event (`events`, checked), so a block of any year blocks it.
    """
    rules_for(tax_year)
    _check_blocking(blocking)
    _check_run(result, events)
    if result.blocking or blocking:
        return Blocked((*result.blocking, *blocking))
    terms = {False: Term(0, ZERO, ZERO, ZERO), True: Term(0, ZERO, ZERO, ZERO)}
    for a in result.allocations:
        if a.disposed.year == tax_year:
            t = terms[a.long_term]
            gain = subtract(a.proceeds, a.basis)
            terms[a.long_term] = Term(
                t.rows + 1, add(t.proceeds, a.proceeds), add(t.basis, a.basis), add(t.gain, gain)
            )
    return YearSummary(tax_year, terms[False], terms[True], result.replay_stopped, tuple(result.late))


@dataclass(frozen=True)
class Holding:
    lot: str
    account: str
    acquired: date
    sats: int
    basis: Decimal  # the gain basis (a gift's donor basis)
    loss_basis: Decimal | None  # a gift's FMV at the gift, where lower
    loss_from: date | None  # and the gift date its holding period runs from
    long_term: bool  # the gain side's holding period on the date asked about
    loss_long_term: bool | None  # a gift's loss side's (None without a loss basis)
    value: Decimal | None  # at the price given, if any
    unrealized: Decimal | None  # gain over the gain basis, loss under the loss basis, none between


@dataclass(frozen=True)
class Holdings:
    as_of: date
    rows: tuple[Holding, ...]
    replay_stopped: str | None = None
    late: tuple[LateIdentification, ...] = ()
    ruleset: str = RULESET
    positions: tuple[str, ...] = POSITIONS


def holdings(  # noqa: PLR0913 - the events, both runs, the date, the price and the caller's blocks
    events: Sequence[Event],
    result: Result,
    upto: Result,
    as_of: date,
    price: Decimal | None = None,
    *,
    blocking: Sequence[object],
) -> Holdings | Blocked:
    """The lots `result` left, by account in the engine's order, with their holding periods on `as_of`
    and, given a USD price per BTC, their value and unrealized gain (a gift's by its dual basis), or why
    they can't be shown. `result` is the engine's run of every event (`events`), whose blocking conditions
    of any date block them, as the caller's do; `upto` is its run of the events up to `as_of`, whose lots
    are shown. Both are checked against `events`."""
    _check_blocking(blocking)
    if type(as_of) is not date:
        raise EngineError("holdings are as of a date")
    _check_run(result, events)
    if upto.events != tuple(e for e in events if e.on <= as_of):
        raise EngineError(f"upto is the engine's run of the events up to {as_of}: it took others")
    btc = None if price is None else _usd(price, "the price")
    if btc is not None and btc == ZERO:
        raise EngineError("the price: a BTC price is at least a cent")
    if result.blocking or blocking:
        return Blocked((*result.blocking, *blocking))
    rows: list[Holding] = []
    for lot in upto.holdings:
        value = None if btc is None else _value(lot.id, lot.sats, btc)
        # the FMV side runs from the gift date alone: a donor's long-term status (an inheritance) carries
        # over only with the donor's basis (Treas. Reg. §1.1223-1(b)), as the engine figures a sale
        loss_long = None if lot.loss_from is None else long_term(lot.loss_from, as_of)
        rows.append(
            Holding(
                lot.id,
                lot.account,
                lot.acquired,
                lot.sats,
                lot.basis,
                lot.loss_basis,
                lot.loss_from,
                lot.always_long or long_term(lot.acquired, as_of),
                loss_long,
                value,
                None if value is None else _unrealized(value, lot.basis, lot.loss_basis),
            )
        )
    rows.sort(key=lambda h: h.account)  # stable: each account's lots stay in the engine's order
    return Holdings(as_of, tuple(rows), upto.replay_stopped, tuple(upto.late))


def _unrealized(value: Decimal, basis: Decimal, loss_basis: Decimal | None) -> Decimal:
    """The gain or loss a sale at `value` would give: against the gain basis, or for a gift worth less
    than its loss basis against that, and none between the two (ADR 0009; IRC §1015(a))."""
    if loss_basis is None or value >= basis:
        return subtract(value, basis)
    if value < loss_basis:
        return subtract(value, loss_basis)
    return ZERO


def _check_run(result: Result, events: Sequence[Event]) -> None:
    if result.events != tuple(events):
        raise EngineError("result is the engine's run of these events: it took others")


def _value(lot: str, sats: int, price: Decimal) -> Decimal:
    try:
        return value_of(sats, price)
    except (TypeError, ValueError):
        raise EngineError(f"lot {lot!r}: its value at that price is too large") from None


def _check_blocking(blocking: object) -> None:
    if not isinstance(blocking, tuple | list):
        raise EngineError("blocking is a list or tuple of the caller's blocking conditions")


def _usd(value: Decimal, what: str) -> Decimal:
    try:
        return usd(value)
    except (TypeError, ValueError) as e:
        raise EngineError(f"{what}: {e}") from None

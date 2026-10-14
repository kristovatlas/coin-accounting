"""Report builders: pure functions from the engine's result to report rows (PLAN §8; ADR 0011,
ADR 0040, ADR 0043; T-510). They return values; the CSV writing and the input hash are the caller's.

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
  2026-01-01, with the broker reporting basis), which the caller marks; through 2024, C/F unless a
  1099-B reported it

**Income** (`income_summary`): each `income` acquisition in the tax year, with its USD FMV at receipt
(its basis, which is also ordinary income) and the source the caller names (PLAN §8).

**Year summary** (`year_summary`): the tax year's realized short- and long-term proceeds, basis and
gain, from the same allocations as Form 8949. **Holdings** (`holdings`): the lots an engine run up to a
date left, each with its basis, its holding period on that date and, given a price, its value and
unrealized gain (against its gain basis; a gift's loss basis is shown apart).

**Blocked:** a report isn't built while the engine reports a blocking condition (unknown basis,
missing lots, an untraced transaction: ADR 0009, ADR 0041 §3): `Blocked` lists them. A late lot
identification only warns (T-508): its rows are flagged, and a stopped late-choice replay is shown
(ADR 0040).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from coinacct.domain.money import add, share, subtract, usd
from coinacct.tax.engine import Acquisition, Allocation, EngineError, Event, Result, long_term
from coinacct.tax.rules import BOXES, RULESET, Box, box, y2024

type Channel = Literal["exchange", "onchain"]

SHORT_BOXES: Final[frozenset[str]] = frozenset("ABCGHI")
SAT: Final = Decimal("0.00000001")
SATS_PER_BTC: Final = 100_000_000
ZERO: Final = Decimal("0.00")
# The stated tax positions every report prints (PLAN §8), by the ADR that records each
POSITIONS: Final[tuple[str, ...]] = (
    "ADR 0008: lots are identified per account; in a self-custody wallet the coins spent identify them.",
    "ADR 0009: a gift received has a dual basis; an unknown basis blocks reports until entered.",
    "ADR 0040: a late lot choice is judged against a full replay with every late choice disregarded.",
    "ADR 0041: lots follow the coins in self-custody wallets, oldest first inside a coin.",
    "ADR 0043: a network or BTC fee is never a taxable event; its basis joins the event it belongs to.",
)


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

    @staticmethod
    def default(channel: Channel, tax_year: int) -> Sale:
        """ADR 0011's default: an exchange sale is reported (without basis) from 2025; an on-chain
        disposal, and an exchange sale through 2024, aren't."""
        return Sale(channel, reported=channel == "exchange" and tax_year > y2024.LAST_YEAR)


@dataclass(frozen=True)
class Row:
    """A Form 8949 row: (a) `description`, (b) `acquired`, (c) `sold`, (d) `proceeds`, (e) `basis`,
    (f)/(g) none (no adjustments are made), (h) `gain`; with its box, term and the allocation it is."""

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
    override_reason: str | None = None


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
    ruleset: str = RULESET
    positions: tuple[str, ...] = POSITIONS


@dataclass(frozen=True)
class Blocked:
    """No report: the engine's blocking conditions, which the user resolves first."""

    blocking: tuple[object, ...]


def form_8949(result: Result, sales: Mapping[str, Sale], tax_year: int) -> Form8949 | Blocked:
    """The tax year's Form 8949 rows and box totals, or why it can't be built."""
    if result.blocking:
        return Blocked(tuple(result.blocking))
    late = {w.disposal for w in result.late}
    rows: list[Row] = []
    for a in result.allocations:
        if a.disposed.year != tax_year:
            continue
        sale = sales.get(a.disposal)
        if not isinstance(sale, Sale):
            raise EngineError(f"disposal {a.disposal!r}: a sale's channel and forms are needed for its box")
        rows.append(_row(a, sale, tax_year, a.disposal in late))
    rows.sort(key=lambda r: (r.box, r.sold, r.disposal, r.acquired, r.lot))
    return Form8949(tax_year, tuple(rows), _totals(rows), result.replay_stopped)


def describe(sats: int) -> str:
    """Form 8949's (a): the amount and the property, "0.12345678 BTC"."""
    return f"{(Decimal(sats) * SAT).quantize(SAT):f} BTC"


def _row(a: Allocation, sale: Sale, tax_year: int, late: bool) -> Row:
    override = sale.override_long if a.long_term else sale.override_short
    if override is None:
        chosen = box(tax_year, a.long_term, sale.reported, sale.basis_reported)
        reason = None
    else:
        _check_override(a.disposal, override, a.long_term, tax_year, sale.reason)
        chosen, reason = override, sale.reason
    return Row(
        chosen,
        describe(a.sats),
        a.acquired,
        a.disposed,
        a.proceeds,
        a.basis,
        subtract(a.proceeds, a.basis),
        a.long_term,
        a.disposal,
        a.lot,
        late,
        reason,
    )


def _check_override(disposal: str, chosen: str, long_term: bool, tax_year: int, reason: str | None) -> None:
    if chosen not in BOXES or (chosen in SHORT_BOXES) == long_term:
        term = "long" if long_term else "short"
        raise EngineError(f"disposal {disposal!r}: box {chosen!r} isn't one for its {term}-term rows")
    if (chosen in "ABCDEF") != (tax_year <= y2024.LAST_YEAR):
        raise EngineError(
            f"disposal {disposal!r}: box {chosen!r} isn't used for digital assets in {tax_year}"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise EngineError(f"disposal {disposal!r}: an overridden box records the user's reason")


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


def income_summary(
    events: Sequence[Event], tax_year: int, sources: Mapping[str, str] | None = None
) -> IncomeSummary:
    """The tax year's `income` acquisitions, by date, with their FMV and total (PLAN §8)."""
    named = sources or {}
    rows: list[IncomeRow] = []
    for e in events:
        if not isinstance(e, Acquisition) or e.kind != "income" or e.on.year != tax_year:
            continue
        if e.basis is None:
            raise EngineError(f"income {e.id!r}: its FMV at receipt is needed")
        rows.append(IncomeRow(e.id, e.on, e.sats, usd(e.basis), named.get(e.id)))
    rows.sort(key=lambda r: (r.received, r.event))
    total = ZERO
    for r in rows:
        total = add(total, r.fmv)
    return IncomeSummary(tax_year, tuple(rows), total)


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
    replay_stopped: str | None
    ruleset: str = RULESET
    positions: tuple[str, ...] = POSITIONS


def year_summary(result: Result, tax_year: int) -> YearSummary | Blocked:
    """The tax year's realized short- and long-term totals, from its sales' and spends' allocations."""
    if result.blocking:
        return Blocked(tuple(result.blocking))
    terms = {False: Term(0, ZERO, ZERO, ZERO), True: Term(0, ZERO, ZERO, ZERO)}
    for a in result.allocations:
        if a.disposed.year == tax_year:
            t = terms[a.long_term]
            gain = subtract(a.proceeds, a.basis)
            terms[a.long_term] = Term(
                t.rows + 1, add(t.proceeds, a.proceeds), add(t.basis, a.basis), add(t.gain, gain)
            )
    return YearSummary(tax_year, terms[False], terms[True], result.replay_stopped)


@dataclass(frozen=True)
class Holding:
    lot: str
    account: str
    acquired: date
    sats: int
    basis: Decimal  # the gain basis (a gift's donor basis)
    loss_basis: Decimal | None  # a gift's FMV at the gift, where lower
    long_term: bool  # on the date asked about
    unknown_basis: bool
    value: Decimal | None  # at the price given, if any
    unrealized: Decimal | None  # value less basis


def holdings(result: Result, as_of: date, price: Decimal | None = None) -> tuple[Holding, ...]:
    """The lots `result` left (an engine run on the events up to `as_of`), by account then lot, with
    their holding period on `as_of` and, given a USD price per BTC, their value and unrealized gain."""
    btc = None if price is None else usd(price)
    out: list[Holding] = []
    for lot in result.holdings:
        if lot.acquired > as_of:
            raise EngineError(f"lot {lot.id!r} was acquired after {as_of}: run the engine up to that date")
        value = None if btc is None else value_of(lot.sats, btc)
        out.append(
            Holding(
                lot.id,
                lot.account,
                lot.acquired,
                lot.sats,
                lot.basis,
                lot.loss_basis,
                lot.always_long or long_term(lot.acquired, as_of),
                lot.unknown_basis,
                value,
                None if value is None else subtract(value, lot.basis),
            )
        )
    out.sort(key=lambda h: (h.account, h.acquired, h.lot))
    return tuple(out)


def value_of(sats: int, price: Decimal) -> Decimal:
    """`sats` at `price` USD per BTC, to the cent (half to even)."""
    whole, rest = divmod(sats, SATS_PER_BTC)
    return add(price * whole, share(price, rest, SATS_PER_BTC))

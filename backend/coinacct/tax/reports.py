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

**Blocked:** a report isn't built while the engine reports a blocking condition (unknown basis,
missing lots, an untraced transaction: ADR 0009, ADR 0041 §3): `Blocked` lists them. A late lot
identification only warns (T-508): its rows are flagged, and a stopped late-choice replay is shown
(ADR 0040).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from coinacct.domain.money import add, subtract
from coinacct.tax.engine import Allocation, EngineError, Result
from coinacct.tax.rules import BOXES, RULESET, Box, box, y2024

type Channel = Literal["exchange", "onchain"]

SHORT_BOXES: Final[frozenset[str]] = frozenset("ABCGHI")
SAT: Final = Decimal("0.00000001")
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

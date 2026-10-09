"""Lots in an account: acquisitions create them, sales and spends use them (PLAN §7; ADRs 0008, 0009, 0021).

Pure: events in, allocations and holdings out. The caller (`services/`) gives the events in time order.
The engine never reads the clock. Each event carries its **tax date** (`on`, already in the user's time
zone, for the holding period); a disposal also carries the **moment** it happened (`at`), and a user's
choice of lots the moment it was made (`identified_at`): both timezone-aware UTC (ENGINEERING §5.2).

- **Acquisitions** (`buy`, `p2p_buy`, `income`, `inherit`) each create one lot: its sats, its basis
  (cost plus fees, or the FMV the caller computed) and its date. Gifts, the 2025 opening allocation,
  movements between accounts and UTXO tracing come in later slices.
- **Disposals** (`sell`, `spend`) use lots of **the same account** only (ADR 0008 §1). Proceeds are net of
  disposal costs (ADR 0009). Lots are chosen:
  - **automatically,** by the account's standing method (FIFO unless another is recorded; ADR 0021 §1).
    These choices are never late;
  - **or by the user.** A choice made after the moment of the disposal (Treas. Reg. §1.1012-1(j): no
    later than the date and time of the sale) is reported as late **only if** its lots differ from the
    standing method's (ADR 0021 §2), with the standing method's figures (ADR 0008 §4). The user's
    choice is used either way. The standing method is applied to the lots as they stand at that
    disposal, after the user's earlier choices (a full replay without them is a later decision, #238).
- **Splits are exact.** A lot's basis and a disposal's proceeds are split by sats with `domain.money.share`,
  always as a share of what remains, and the last part takes the remainder. So every split adds up
  to the whole, to the cent. All money arithmetic runs in the fixed `domain.money` context (T-502).
- **Holding period** (IRC §1222; Rev. Rul. 66-7): the day after acquisition starts it, and a disposal
  is long-term if it is more than one year later: after the first anniversary of the acquisition. A
  lot acquired on the last day of a month has its anniversary on the last day of that month a year
  later (Rev. Rul. 66-6): bought 28 February 2023, it is long-term from 1 March 2024, not 29 February.
  An inherited lot is always long-term (IRC §1223(9)).
- **Not enough lots** for an automatic disposal is a **blocking condition** (ADR 0009): the lots that
  exist are used, and the rest of the disposal is reported as missing basis. Invalid input (a choice
  naming lots the account doesn't hold, a wrong type) raises `EngineError`.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Final, Literal

from coinacct.domain.chain import MAX_SATS
from coinacct.domain.money import CONTEXT, share, subtract, usd

type AcquisitionKind = Literal["buy", "p2p_buy", "income", "inherit"]
type DisposalKind = Literal["sell", "spend"]
type Method = Literal["fifo"]

ACQUISITION_KINDS: Final[frozenset[str]] = frozenset({"buy", "p2p_buy", "income", "inherit"})
DISPOSAL_KINDS: Final[frozenset[str]] = frozenset({"sell", "spend"})
METHODS: Final[frozenset[str]] = frozenset({"fifo"})


class EngineError(ValueError):
    """The events are invalid input: the caller must not have produced them."""


@dataclass(frozen=True)
class Acquisition:
    id: str
    account: str
    on: date
    kind: AcquisitionKind
    sats: int
    basis: Decimal  # USD: cost plus acquisition fees, or the FMV at receipt


@dataclass(frozen=True)
class Pick:
    lot: str  # the id of the acquisition that created the lot
    sats: int


@dataclass(frozen=True)
class Disposal:
    id: str
    account: str
    on: date  # the tax date
    at: datetime  # the moment, timezone-aware UTC
    kind: DisposalKind
    sats: int
    proceeds: Decimal  # USD, net of disposal costs
    picks: tuple[Pick, ...] | None = None  # None: the account's standing method
    identified_at: datetime | None = None  # when the user chose `picks`, timezone-aware UTC


type Event = Acquisition | Disposal


@dataclass(frozen=True)
class Lot:
    """What is left of a lot."""

    id: str
    account: str
    acquired: date
    sats: int
    basis: Decimal
    always_long: bool


@dataclass(frozen=True)
class Allocation:
    """One disposal's use of one lot: a Form 8949 row's numbers (ADR 0011 chooses its box)."""

    disposal: str
    lot: str
    sats: int
    basis: Decimal
    proceeds: Decimal
    acquired: date
    disposed: date
    long_term: bool

    @property
    def gain(self) -> Decimal:
        """Proceeds less basis: negative for a loss."""
        return subtract(self.proceeds, self.basis)


@dataclass(frozen=True)
class LateIdentification:
    """A choice made after its disposal whose lots differ from the standing method's (ADR 0021 §2).
    `standing` is what the standing method would have used, with its figures, shown with the warning
    (ADR 0008 §4). Its sats may fall short of the disposal's if the account held fewer lots then."""

    disposal: str
    identified_at: datetime
    standing: tuple[Allocation, ...]


@dataclass(frozen=True)
class MissingLots:
    """Blocking: a disposal's sats the account holds no lots for (ADR 0009)."""

    disposal: str
    sats: int
    proceeds: Decimal


@dataclass(frozen=True)
class Result:
    allocations: tuple[Allocation, ...]
    holdings: tuple[Lot, ...]  # the lots left, in acquisition order across accounts, without empty ones
    late: tuple[LateIdentification, ...]
    blocking: tuple[MissingLots, ...]


@dataclass
class _Open:
    lot: Lot
    sats: int
    basis: Decimal


def long_term(acquired: date, disposed: date) -> bool:
    """Held more than one year: disposed after the first anniversary of the acquisition (see the module
    docstring for the last day of a month)."""
    year = acquired.year + 1
    if acquired.day == monthrange(acquired.year, acquired.month)[1]:
        anniversary = date(year, acquired.month, monthrange(year, acquired.month)[1])
    else:  # not the last day of its month, so the same day exists a year later
        anniversary = date(year, acquired.month, acquired.day)
    return disposed > anniversary


def run(events: Sequence[Event], methods: Mapping[str, Method] | None = None) -> Result:
    """Apply `events` in order. `methods` holds each account's standing method (FIFO if absent)."""
    with localcontext(CONTEXT):
        return _Engine(methods or {}).run(events)


class _Engine:
    def __init__(self, methods: Mapping[str, Method]) -> None:
        for account, method in methods.items():
            if method not in METHODS:
                raise EngineError(f"account {account!r}: unknown standing method {method!r}")
        self._methods = methods
        self._open: dict[str, list[_Open]] = {}  # account -> its lots, in acquisition order
        self._lots: dict[str, _Open] = {}  # every lot, in acquisition order
        self._seen: set[str] = set()
        self._allocations: list[Allocation] = []
        self._late: list[LateIdentification] = []
        self._blocking: list[MissingLots] = []

    def run(self, events: Sequence[Event]) -> Result:
        last: date | None = None
        for event in events:
            _check_common(event)
            if event.id in self._seen:
                raise EngineError(f"event {event.id!r} appears twice")
            self._seen.add(event.id)
            if last is not None and event.on < last:
                raise EngineError(f"event {event.id!r} is out of date order")
            last = event.on
            if isinstance(event, Acquisition):
                self._acquire(event)
            else:
                self._dispose(event)
        holdings = tuple(
            Lot(o.lot.id, o.lot.account, o.lot.acquired, o.sats, o.basis, o.lot.always_long)
            for o in self._lots.values()
            if o.sats > 0
        )
        return Result(tuple(self._allocations), holdings, tuple(self._late), tuple(self._blocking))

    def _acquire(self, a: Acquisition) -> None:
        if a.kind not in ACQUISITION_KINDS:
            raise EngineError(f"acquisition {a.id!r}: unknown kind {a.kind!r}")
        basis = _usd(a.basis, a.id)
        lot = Lot(a.id, a.account, a.on, a.sats, basis, always_long=a.kind == "inherit")
        entry = _Open(lot, a.sats, basis)
        self._open.setdefault(a.account, []).append(entry)
        self._lots[a.id] = entry

    def _standing(self, account: str, sats: int) -> tuple[list[Pick], int]:
        """The standing method's picks for `sats`, and the sats it couldn't cover. The only standing
        method so far is FIFO (METHODS; checked in __init__)."""
        picks: list[Pick] = []
        left = sats
        for o in self._open.get(account, []):
            if left == 0:
                break
            take = min(o.sats, left)
            if take > 0:
                picks.append(Pick(o.lot.id, take))
                left -= take
        return picks, left

    def _dispose(self, d: Disposal) -> None:
        if d.kind not in DISPOSAL_KINDS:
            raise EngineError(f"disposal {d.id!r}: unknown kind {d.kind!r}")
        _check_moment(d.at, f"disposal {d.id!r}: its moment")
        proceeds = _usd(d.proceeds, d.id)
        missing = 0
        if d.picks is None:
            if d.identified_at is not None:
                raise EngineError(f"disposal {d.id!r}: a standing-method disposal has no identification time")
            picks, missing = self._standing(d.account, d.sats)
        else:
            if d.identified_at is None:
                raise EngineError(f"disposal {d.id!r}: a chosen set of lots needs the time it was chosen")
            _check_moment(d.identified_at, f"disposal {d.id!r}: its identification time")
            # Valid picks mean the account holds the sats, so FIFO isn't short either.
            picks = self._checked_picks(d, d.picks)
            if d.identified_at > d.at:
                standing, _ = self._standing(d.account, d.sats)
                if _merged(picks) != _merged(standing):
                    figures, _ = self._split(d, proceeds, standing, apply=False)
                    self._late.append(LateIdentification(d.id, d.identified_at, tuple(figures)))
        allocations, rest = self._split(d, proceeds, picks, apply=True)
        self._allocations.extend(allocations)
        if missing:  # the standing picks covered all but `missing` sats; `rest` is their proceeds
            self._blocking.append(MissingLots(d.id, missing, rest))

    def _split(
        self, d: Disposal, proceeds: Decimal, picks: Sequence[Pick], *, apply: bool
    ) -> tuple[list[Allocation], Decimal]:
        """The allocations of `picks`, and the proceeds left for sats they don't cover. Proceeds follow
        the sats: each part is a share of what remains of the disposal, the last part the rest; basis
        is a share of what remains of the lot. With `apply` the lots are used up; without, the figures
        are only computed (the late warning's)."""
        out: list[Allocation] = []
        sats_left, proceeds_left = d.sats, proceeds
        lots_left = {p.lot: (self._lots[p.lot].sats, self._lots[p.lot].basis) for p in picks}
        for pick in picks:
            o = self._lots[pick.lot]
            part = share(proceeds_left, pick.sats, sats_left)
            sats, basis_left = lots_left[pick.lot]
            basis = share(basis_left, pick.sats, sats)
            lots_left[pick.lot] = (sats - pick.sats, subtract(basis_left, basis))
            sats_left -= pick.sats
            proceeds_left = subtract(proceeds_left, part)
            term = o.lot.always_long or long_term(o.lot.acquired, d.on)
            out.append(Allocation(d.id, pick.lot, pick.sats, basis, part, o.lot.acquired, d.on, term))
        if apply:
            for lot, (sats, basis) in lots_left.items():
                self._lots[lot].sats, self._lots[lot].basis = sats, basis
        return out, proceeds_left

    def _checked_picks(self, d: Disposal, chosen: tuple[Pick, ...]) -> list[Pick]:
        """The user's picks, checked, with repeats of a lot merged (first-seen order)."""
        wanted: dict[str, int] = {}
        for pick in chosen:
            if not isinstance(pick, Pick):
                raise EngineError(f"disposal {d.id!r}: a choice is a tuple of Picks")
            _check_sats(pick.sats, f"disposal {d.id!r}")
            entry = self._lots.get(pick.lot)
            if entry is None or entry.lot.account != d.account:
                raise EngineError(f"disposal {d.id!r}: lot {pick.lot!r} isn't held in account {d.account!r}")
            wanted[pick.lot] = wanted.get(pick.lot, 0) + pick.sats
            if wanted[pick.lot] > entry.sats:
                raise EngineError(f"disposal {d.id!r}: lot {pick.lot!r} holds fewer sats than chosen")
        if sum(wanted.values()) != d.sats:
            raise EngineError(f"disposal {d.id!r}: the chosen lots don't add up to its sats")
        return [Pick(lot, sats) for lot, sats in wanted.items()]


def _merged(picks: Sequence[Pick]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for p in picks:
        merged[p.lot] = merged.get(p.lot, 0) + p.sats
    return merged


def _check_common(event: Event) -> None:
    if not isinstance(event, Acquisition | Disposal):
        raise EngineError("an event is an Acquisition or a Disposal")
    if not (isinstance(event.id, str) and event.id and isinstance(event.account, str) and event.account):
        raise EngineError("an event names its id and its account")
    if type(event.on) is not date:  # a datetime is a date too, but not a tax date
        raise EngineError(f"event {event.id!r}: its tax date must be a date")
    _check_sats(event.sats, f"event {event.id!r}")


def _check_moment(moment: object, where: str) -> None:
    if not isinstance(moment, datetime) or moment.utcoffset() != UTC.utcoffset(None):
        raise EngineError(f"{where} must be a timezone-aware UTC datetime")


def _check_sats(sats: object, where: str) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or not 0 < sats <= MAX_SATS:
        raise EngineError(f"{where}: sats must be a positive whole number no larger than the supply")


def _usd(value: Decimal, event: str) -> Decimal:
    try:
        return usd(value)
    except ValueError as e:
        raise EngineError(f"event {event!r}: {e}") from None

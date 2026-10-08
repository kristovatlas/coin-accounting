"""Lots in an account: acquisitions create them, sales and spends use them (PLAN §7; ADRs 0008, 0009, 0021).

Pure: events in, allocations and holdings out. The caller (`services/`) gives the events in time order;
the engine never reads the clock, and dates are tax dates, already in the user's time zone.

- **Acquisitions** (`buy`, `p2p_buy`, `income`, `inherit`) each create one lot: its sats, its basis
  (cost plus fees, or the FMV the caller computed) and its date. Gifts, the 2025 opening allocation,
  movements between accounts and UTXO tracing come in later slices.
- **Disposals** (`sell`, `spend`) use lots of **the same account** only (ADR 0008 §1). Proceeds are net of
  disposal costs (ADR 0009). Lots are chosen:
  - **automatically,** by the account's standing method (FIFO unless another is recorded; ADR 0021 §1).
    These choices are never late;
  - **or by the user,** with the date the choice was made. A choice made after the disposal is reported
    as late **only if** its lots differ from the standing method's (ADR 0021 §2). The user's choice is
    used either way (ADR 0008 §4).
- **Splits are exact.** A lot's basis and a disposal's proceeds are split by sats with `domain.money.share`,
  always as a share of what remains, and the last part takes the remainder. So every split adds up
  to the whole, to the cent, and nothing is lost to rounding (T-502).
- **Holding period** (IRC §1222; Rev. Rul. 66-7): the day after acquisition starts it, and a disposal
  is long-term if it is more than one year later: after the first anniversary of the acquisition. A
  lot acquired on the last day of a month has its anniversary on the last day of that month a year
  later (Rev. Rul. 66-6): bought 28 February 2023, it is long-term from 1 March 2024, not 29 February.
  An inherited lot is always long-term (IRC §1223(9)).
- **Not enough lots** for an automatic disposal is a **blocking condition** (ADR 0009): the lots that
  exist are used, and the rest of the disposal is reported as missing basis. A user choice that
  names lots the account doesn't hold is invalid input and raises `EngineError`.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, FloatOperation, localcontext
from typing import Final, Literal

from coinacct.domain.chain import MAX_SATS
from coinacct.domain.money import share, usd

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
    on: date
    kind: DisposalKind
    sats: int
    proceeds: Decimal  # USD, net of disposal costs
    picks: tuple[Pick, ...] | None = None  # None: the account's standing method
    identified_on: date | None = None  # when the user chose `picks`


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
        return self.proceeds - self.basis


@dataclass(frozen=True)
class LateIdentification:
    """A choice made after its disposal whose lots differ from the standing method's (ADR 0021 §2).
    `standing` is what the standing method would have used, shown with the warning (ADR 0008 §4)."""

    disposal: str
    identified_on: date
    standing: tuple[Pick, ...]


@dataclass(frozen=True)
class MissingLots:
    """Blocking: a disposal's sats the account holds no lots for (ADR 0009)."""

    disposal: str
    sats: int
    proceeds: Decimal


@dataclass(frozen=True)
class Result:
    allocations: tuple[Allocation, ...]
    holdings: tuple[Lot, ...]  # the lots left, in acquisition order, without empty ones
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
    last_day = monthrange(year, acquired.month)[1]
    if acquired.day == monthrange(acquired.year, acquired.month)[1]:
        anniversary = date(year, acquired.month, last_day)
    else:
        anniversary = date(year, acquired.month, min(acquired.day, last_day))
    return disposed > anniversary


def run(events: Sequence[Event], methods: Mapping[str, Method] | None = None) -> Result:
    """Apply `events` in order. `methods` holds each account's standing method (FIFO if absent)."""
    with localcontext() as ctx:
        ctx.traps[FloatOperation] = True
        return _Engine(methods or {}).run(events)


class _Engine:
    def __init__(self, methods: Mapping[str, Method]) -> None:
        for account, method in methods.items():
            if method not in METHODS:
                raise EngineError(f"account {account!r}: unknown standing method {method!r}")
        self._methods = methods
        self._open: dict[str, list[_Open]] = {}  # account -> its lots, in acquisition order
        self._lots: dict[str, _Open] = {}
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
            for lots in self._open.values()
            for o in lots
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
        """The standing method's picks for `sats`, and the sats it couldn't cover."""
        # The only standing method so far is FIFO (METHODS; checked in __init__).
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
        proceeds = _usd(d.proceeds, d.id)
        standing, missing = self._standing(d.account, d.sats)
        if d.picks is None:
            if d.identified_on is not None:
                raise EngineError(f"disposal {d.id!r}: a standing-method disposal has no identification date")
            picks = standing
        else:
            if d.identified_on is None:
                raise EngineError(f"disposal {d.id!r}: a chosen set of lots needs the date it was chosen")
            # Valid picks mean the account holds the sats, so FIFO isn't short either: missing is 0.
            picks = self._checked_picks(d, d.picks)
            if d.identified_on > d.on and _merged(picks) != _merged(standing):
                self._late.append(LateIdentification(d.id, d.identified_on, tuple(standing)))
        # Proceeds follow the sats: each part is a share of what remains, the last takes the rest.
        sats_left, proceeds_left = d.sats, proceeds
        for pick in picks:
            part = share(proceeds_left, pick.sats, sats_left)
            self._allocate(d, pick, part)
            sats_left -= pick.sats
            proceeds_left -= part
        if missing:  # the standing picks covered all but `missing` sats, so sats_left == missing
            self._blocking.append(MissingLots(d.id, missing, proceeds_left))

    def _checked_picks(self, d: Disposal, chosen: tuple[Pick, ...]) -> list[Pick]:
        wanted: dict[str, int] = {}
        for pick in chosen:
            _check_sats(pick.sats, f"disposal {d.id!r}")
            entry = self._lots.get(pick.lot)
            if entry is None or entry.lot.account != d.account:
                raise EngineError(f"disposal {d.id!r}: lot {pick.lot!r} isn't held in account {d.account!r}")
            wanted[pick.lot] = wanted.get(pick.lot, 0) + pick.sats
            if wanted[pick.lot] > entry.sats:
                raise EngineError(f"disposal {d.id!r}: lot {pick.lot!r} holds fewer sats than chosen")
        if sum(wanted.values()) != d.sats:
            raise EngineError(f"disposal {d.id!r}: the chosen lots don't add up to its sats")
        return list(chosen)

    def _allocate(self, d: Disposal, pick: Pick, proceeds: Decimal) -> None:
        o = self._lots[pick.lot]
        basis = share(o.basis, pick.sats, o.sats)
        o.sats -= pick.sats
        o.basis -= basis
        term = o.lot.always_long or long_term(o.lot.acquired, d.on)
        self._allocations.append(
            Allocation(d.id, pick.lot, pick.sats, basis, proceeds, o.lot.acquired, d.on, term)
        )


def _merged(picks: Sequence[Pick]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for p in picks:
        merged[p.lot] = merged.get(p.lot, 0) + p.sats
    return merged


def _check_common(event: Event) -> None:
    if not isinstance(event, Acquisition | Disposal):
        raise EngineError("an event is an Acquisition or a Disposal")
    if (
        not isinstance(event.id, str)
        or not event.id
        or not isinstance(event.account, str)
        or not event.account
    ):
        raise EngineError("an event names its id and its account")
    if not isinstance(event.on, date):
        raise EngineError(f"event {event.id!r}: its date must be a date")
    _check_sats(event.sats, f"event {event.id!r}")


def _check_sats(sats: object, where: str) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or not 0 < sats <= MAX_SATS:
        raise EngineError(f"{where}: sats must be a positive whole number no larger than the supply")


def _usd(value: Decimal, event: str) -> Decimal:
    try:
        return usd(value)
    except ValueError as e:
        raise EngineError(f"event {event!r}: {e}") from None

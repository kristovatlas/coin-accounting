"""Lots in an account: acquisitions create them, sales, spends and gifts use them (PLAN §7; ADRs 0008,
0009, 0011, 0021).

Pure: events in, allocations and holdings out. The caller (`services/`) gives the events in time order:
by tax date, and disposals by moment. The engine never reads the clock. Each event carries its **tax
date** (`on`, already in the user's time zone, for the holding period); a disposal also carries the
**moment** it happened (`at`), and a user's choice of lots the moment it was made (`identified_at`):
both timezone-aware UTC (ENGINEERING §5.2). A disposal's tax date is within a day of its moment's UTC
date (time zones run from UTC-12 to UTC+14), and disposals come in the order of their moments.

- **Acquisitions** (`buy`, `p2p_buy`, `income`, `inherit`, `gift_in`) each create one lot: its sats, its
  basis (cost plus fees, or the FMV the caller computed) and its date. The 2025 opening allocation,
  movements between accounts, fees by role and UTXO tracing come in later slices.
- **A gift received** (`gift_in`; IRC §1015(a), Treas. Reg. §1.1015-1(a)) has a **dual basis**. Its gain
  basis is the donor's basis, held from the donor's date (tacked, IRC §1223(2)). If the FMV at the gift
  was lower, that FMV is its loss basis, held from the gift date. At a disposal, proceeds above the
  gain basis give a gain against it; proceeds below the loss basis give a loss against that; proceeds
  in between give neither (the basis used is the proceeds). Whether a lot has a loss basis is decided
  once, at the gift, from the exact values: only an FMV strictly below the donor's basis makes one,
  and then every part of the lot keeps it, even where its cent-rounded share equals the gain share
  (the loss share is capped at the gain share). The donor's basis is entered as the user has it:
  §1015(d) gift-tax adjustments are out of scope for v1 (ADR 0009). An unknown donor basis is a
  **blocking condition** (ADR 0009, T-509): the lot counts as basis 0.00 until the user enters one.
- **Disposals** (`sell`, `spend`, `gift_out`) use lots of **the same account** only (ADR 0008 §1). Proceeds
  are net of disposal costs, and can be negative when the costs exceed what was received (ADR 0009). A
  **gift given** (`gift_out`) has no proceeds and no gain or loss, and makes no Form 8949 row (ADR 0011):
  it reports the basis and date that pass to the recipient. Lots are chosen:
  - **automatically,** by the account's standing method (FIFO unless another is recorded; ADR 0021 §1).
    These choices are never late. FIFO takes lots in the order they reached the account: a gift by the
    day it was received, not its tacked donor date (a tax position for the owner to confirm, #241);
  - **or by the user.** A choice made after the moment of the disposal (Treas. Reg. §1.1012-1(j): no
    later than the date and time of the sale) is reported as late **only if** its lots differ from the
    standing method's (ADR 0021 §2), with the standing method's figures (ADR 0008 §4). The user's
    choice is used either way. The standing method is applied to the lots as they stand at that
    disposal, after the user's earlier choices (a full replay without them is a later decision, #238).
- **Splits are exact.** Bases and proceeds are split by sats with `domain.money.share`, always as a share
  of what remains, and the last part takes the remainder. So every split adds up to the whole, to the
  cent. All money arithmetic runs in the fixed `domain.money` context (T-502).
- **Holding period** (IRC §1222; Rev. Rul. 66-7): the day after acquisition starts it, and a disposal
  is long-term if it is more than one year later: after the first anniversary of the acquisition. A
  lot acquired on the last day of a month has its anniversary on the last day of that month a year
  later (Rev. Rul. 66-6, applied to the one-year period: a tax position for the owner to confirm,
  #239): bought 28 February 2023, it is long-term from 1 March 2024, not 29 February. An inherited lot
  is always long-term (IRC §1223(9)).
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
from coinacct.domain.money import CONTEXT, share, signed_usd, subtract, usd

type AcquisitionKind = Literal["buy", "p2p_buy", "income", "inherit", "gift_in"]
type DisposalKind = Literal["sell", "spend", "gift_out"]
type Method = Literal["fifo"]
# How an allocation's basis was found: the lot's own basis; a gift's donor basis (a gain) or FMV at the
# gift (a loss); between the two, neither; or a gift's donor basis that isn't known (blocking).
type BasisRule = Literal["cost", "donor", "fmv_at_gift", "no_gain_or_loss", "unknown"]

ACQUISITION_KINDS: Final[frozenset[str]] = frozenset({"buy", "p2p_buy", "income", "inherit", "gift_in"})
DISPOSAL_KINDS: Final[frozenset[str]] = frozenset({"sell", "spend", "gift_out"})
METHODS: Final[frozenset[str]] = frozenset({"fifo"})
ZERO: Final = Decimal("0.00")
GENESIS: Final = date(2009, 1, 3)  # no bitcoin was acquired before the genesis block


class EngineError(ValueError):
    """The events are invalid input: the caller must not have produced them."""


@dataclass(frozen=True)
class Acquisition:
    id: str
    account: str
    on: date
    kind: AcquisitionKind
    sats: int
    basis: Decimal | None  # USD: cost plus fees, the FMV at receipt, or a gift's donor basis (None: unknown)
    fmv: Decimal | None = None  # gift_in only: the FMV at the gift
    donor_acquired: date | None = None  # gift_in only: the donor's acquisition date
    # gift_in only: the donor's lot counted as long-term whatever its dates (an inheritance, IRC §1223(9));
    # whether that passes through a gift is the caller's tax position (#241).
    donor_always_long: bool = False


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
    proceeds: Decimal  # USD, net of disposal costs; 0.00 for a gift given
    picks: tuple[Pick, ...] | None = None  # None: the account's standing method
    identified_at: datetime | None = None  # when the user chose `picks`, timezone-aware UTC


type Event = Acquisition | Disposal


@dataclass(frozen=True)
class Lot:
    """What is left of a lot. For a gift, `basis` and `acquired` are the donor's (the gain basis), and
    `loss_basis`/`loss_from` the FMV at the gift and the gift date when that FMV was lower."""

    id: str
    account: str
    acquired: date
    sats: int
    basis: Decimal
    always_long: bool
    loss_basis: Decimal | None = None
    loss_from: date | None = None
    unknown_basis: bool = False


@dataclass(frozen=True)
class Allocation:
    """One disposal's use of one lot: a Form 8949 row's numbers (ADR 0011 chooses its box). `basis`
    and `acquired` are the ones the gain or loss is figured with (`rule`); `lot_basis` is what the
    lot gave up of its own basis."""

    disposal: str
    lot: str
    sats: int
    basis: Decimal
    proceeds: Decimal
    acquired: date
    disposed: date
    long_term: bool
    rule: BasisRule = "cost"
    lot_basis: Decimal | None = None  # None: the same as `basis`
    lot_loss_basis: Decimal | None = None  # a gift's FMV basis the lot gave up, when it has one

    @property
    def gain(self) -> Decimal:
        """Proceeds less basis: negative for a loss."""
        return subtract(self.proceeds, self.basis)


@dataclass(frozen=True)
class GiftGiven:
    """A gift's use of one lot: no gain or loss, and no Form 8949 row (ADR 0011). It records what the
    recipient's statement needs from the user (ADR 0009): the user's basis and date (`basis`,
    `acquired`; for a lot the user was given, the donor's, tacked). The recipient's own loss basis
    depends on the FMV at this gift (IRC §1015(a)), which is the caller's to supply, not a basis the
    user held: an FMV basis from a gift the user received is never passed on."""

    disposal: str
    lot: str
    sats: int
    basis: Decimal
    acquired: date
    unknown_basis: bool
    always_long: bool = False  # the lot counted as long-term whatever its dates (an inheritance)


@dataclass(frozen=True)
class LateIdentification:
    """A choice made after its disposal whose lots differ from the standing method's (ADR 0021 §2).
    `standing` is what the standing method would have used, with its figures, shown with the warning
    (ADR 0008 §4): allocations for a sale or spend, and gift records (no gain or loss) for a gift given."""

    disposal: str
    identified_at: datetime
    standing: tuple[Allocation, ...] | tuple[GiftGiven, ...]


@dataclass(frozen=True)
class MissingLots:
    """Blocking: a disposal's sats the account holds no lots for (ADR 0009)."""

    disposal: str
    sats: int
    proceeds: Decimal


@dataclass(frozen=True)
class UnknownBasis:
    """Blocking: a gift received whose donor basis isn't known (ADR 0009, T-509). The user enters one;
    zero is the conservative choice."""

    lot: str


@dataclass(frozen=True)
class Result:
    allocations: tuple[Allocation, ...]
    gifts: tuple[GiftGiven, ...]
    holdings: tuple[Lot, ...]  # the lots left, in acquisition order across accounts, without empty ones
    late: tuple[LateIdentification, ...]
    blocking: tuple[MissingLots | UnknownBasis, ...]


@dataclass
class _Open:
    lot: Lot
    sats: int
    basis: Decimal
    loss_basis: Decimal | None
    gift: bool


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
        self._open: dict[str, list[_Open]] = {}  # account -> its lots, in acquisition order
        self._first: dict[str, int] = {}  # account -> the index of its first lot that isn't used up
        self._lots: dict[str, _Open] = {}  # every lot, in acquisition order
        self._seen: set[str] = set()
        self._allocations: list[Allocation] = []
        self._gifts: list[GiftGiven] = []
        self._late: list[LateIdentification] = []
        self._blocking: list[MissingLots | UnknownBasis] = []

    def run(self, events: Sequence[Event]) -> Result:
        last: date | None = None
        last_moment: datetime | None = None
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
                continue
            _check_moment(event.at, f"disposal {event.id!r}: its moment")
            if abs((event.on - event.at.date()).days) > 1:
                raise EngineError(f"disposal {event.id!r}: its tax date is more than a day from its moment")
            if last_moment is not None and event.at < last_moment:
                raise EngineError(f"disposal {event.id!r} is out of time order")
            last_moment = event.at
            self._dispose(event)
        holdings = tuple(
            Lot(
                o.lot.id,
                o.lot.account,
                o.lot.acquired,
                o.sats,
                o.basis,
                o.lot.always_long,
                o.loss_basis,
                None if o.loss_basis is None else o.lot.loss_from,
                o.lot.unknown_basis,
            )
            for o in self._lots.values()
            if o.sats > 0
        )
        return Result(
            tuple(self._allocations), tuple(self._gifts), holdings, tuple(self._late), tuple(self._blocking)
        )

    def _acquire(self, a: Acquisition) -> None:
        if a.kind not in ACQUISITION_KINDS:
            raise EngineError(f"acquisition {a.id!r}: unknown kind {a.kind!r}")
        loss_basis: Decimal | None = None
        loss_from: date | None = None
        unknown = False
        acquired = a.on
        always_long = a.kind == "inherit" or (a.kind == "gift_in" and a.donor_always_long is True)
        if a.kind == "gift_in":
            if a.fmv is None or type(a.donor_acquired) is not date or not GENESIS <= a.donor_acquired <= a.on:
                raise EngineError(
                    f"gift {a.id!r}: a gift names its FMV and the donor's date, from genesis to it"
                )
            fmv = _usd(a.fmv, a.id)
            acquired = a.donor_acquired
            if a.basis is None:
                unknown, basis = True, ZERO
                self._blocking.append(UnknownBasis(a.id))
            else:
                basis = _usd(a.basis, a.id)
                if fmv < basis:
                    loss_basis, loss_from = fmv, a.on
        else:
            if a.fmv is not None or a.donor_acquired is not None or a.donor_always_long:
                raise EngineError(
                    f"acquisition {a.id!r}: only a gift has an FMV at the gift and a donor's date"
                )
            if a.basis is None:
                raise EngineError(f"acquisition {a.id!r}: only a gift's basis can be unknown")
            basis = _usd(a.basis, a.id)
        lot = Lot(a.id, a.account, acquired, a.sats, basis, always_long, loss_basis, loss_from, unknown)
        entry = _Open(lot, a.sats, basis, loss_basis, a.kind == "gift_in")
        self._open.setdefault(a.account, []).append(entry)
        self._first.setdefault(a.account, 0)
        self._lots[a.id] = entry

    def _standing(self, account: str, sats: int) -> tuple[list[Pick], int]:
        """FIFO's picks for `sats`, and the sats it couldn't cover. FIFO is the only standing method so
        far (METHODS, checked in __init__). The scan starts at the account's first lot not used up."""
        lots = self._open.get(account, [])
        first = self._first.get(account, 0)
        while first < len(lots) and lots[first].sats == 0:
            first += 1
        self._first[account] = first
        picks: list[Pick] = []
        left = sats
        for i in range(first, len(lots)):  # by index: a slice would copy the rest of the list each time
            o = lots[i]
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
        proceeds = _signed_usd(d.proceeds, d.id)
        if d.kind == "gift_out" and proceeds != 0:
            raise EngineError(f"disposal {d.id!r}: a gift given has no proceeds")
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
                    alternative = (
                        tuple(self._gift(a) for a in figures) if d.kind == "gift_out" else tuple(figures)
                    )
                    self._late.append(LateIdentification(d.id, d.identified_at, alternative))
        allocations, rest = self._split(d, proceeds, picks, apply=True)
        if d.kind == "gift_out":
            self._gifts.extend(self._gift(a) for a in allocations)
        else:
            self._allocations.extend(allocations)
        if missing:  # the standing picks covered all but `missing` sats; `rest` is their proceeds
            self._blocking.append(MissingLots(d.id, missing, rest))

    def _gift(self, a: Allocation) -> GiftGiven:
        lot = self._lots[a.lot].lot
        lot_basis = a.basis if a.lot_basis is None else a.lot_basis
        return GiftGiven(
            a.disposal, a.lot, a.sats, lot_basis, lot.acquired, lot.unknown_basis, lot.always_long
        )

    def _split(
        self, d: Disposal, proceeds: Decimal, picks: Sequence[Pick], *, apply: bool
    ) -> tuple[list[Allocation], Decimal]:
        """The allocations of `picks`, and the proceeds left for sats they don't cover. Proceeds follow
        the sats: each part is a share of what remains of the disposal, the last part the rest; a lot's
        bases are shares of what remains of them. With `apply` the lots are used up; without, the
        figures are only computed (the late warning's)."""
        out: list[Allocation] = []
        sats_left, proceeds_left = d.sats, proceeds
        left = {
            p.lot: (self._lots[p.lot].sats, self._lots[p.lot].basis, self._lots[p.lot].loss_basis)
            for p in picks
        }
        for pick in picks:
            o = self._lots[pick.lot]
            part = share(proceeds_left, pick.sats, sats_left)
            sats, basis_left, loss_left = left[pick.lot]
            basis = share(basis_left, pick.sats, sats)
            loss = None if loss_left is None else share(loss_left, pick.sats, sats)
            rest = subtract(basis_left, basis)
            loss_rest = None if loss_left is None or loss is None else subtract(loss_left, loss)
            # The loss basis stays (decided at the gift), but never above the gain basis left.
            left[pick.lot] = (sats - pick.sats, rest, None if loss_rest is None else min(loss_rest, rest))
            sats_left -= pick.sats
            proceeds_left = subtract(proceeds_left, part)
            out.append(self._allocation(d, o.lot, (pick.sats, basis, loss), part))
        if apply:
            for lot, (sats, basis, loss) in left.items():
                entry = self._lots[lot]
                entry.sats, entry.basis, entry.loss_basis = sats, basis, loss
        return out, proceeds_left

    def _allocation(
        self, d: Disposal, lot: Lot, taken: tuple[int, Decimal, Decimal | None], proceeds: Decimal
    ) -> Allocation:
        """The basis and holding period a gain or loss is figured with (the module docstring). `taken`
        is the sats and the shares of the lot's bases this allocation uses."""
        sats, basis, loss = taken
        acquired = lot.acquired
        rule: BasisRule = "cost"
        used = basis
        if lot.unknown_basis:
            rule = "unknown"
        elif loss is not None and lot.loss_from is not None:  # a gift whose FMV was lower: dual basis
            if proceeds > basis:
                rule = "donor"
            elif proceeds < loss:
                rule, used, acquired = "fmv_at_gift", loss, lot.loss_from
            else:
                rule, used = "no_gain_or_loss", proceeds
        elif self._lots[lot.id].gift:
            rule = "donor"
        term = lot.always_long or long_term(acquired, d.on)
        lot_basis = None if used == basis else basis
        return Allocation(d.id, lot.id, sats, used, proceeds, acquired, d.on, term, rule, lot_basis, loss)

    def _checked_picks(self, d: Disposal, chosen: tuple[Pick, ...]) -> list[Pick]:
        """The user's picks, checked, with repeats of a lot merged (first-seen order)."""
        wanted: dict[str, int] = {}
        for pick in chosen:
            if not isinstance(pick, Pick) or not isinstance(pick.lot, str) or not pick.lot:
                raise EngineError(f"disposal {d.id!r}: a choice is a tuple of Picks naming lots")
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
    if not isinstance(moment, datetime) or moment.tzinfo is not UTC:
        raise EngineError(f"{where} must be a timezone-aware UTC datetime")


def _check_sats(sats: object, where: str) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or not 0 < sats <= MAX_SATS:
        raise EngineError(f"{where}: sats must be a positive whole number no larger than the supply")


def _usd(value: Decimal, event: str) -> Decimal:
    try:
        return usd(value)
    except ValueError as e:
        raise EngineError(f"event {event!r}: {e}") from None


def _signed_usd(value: Decimal, event: str) -> Decimal:
    try:
        return signed_usd(value)
    except ValueError as e:
        raise EngineError(f"event {event!r}: {e}") from None

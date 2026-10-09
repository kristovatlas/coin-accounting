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
    These choices are never late. FIFO takes lots by the date they reached the user (a purchase, an
    income or an inheritance on its date; a gift by the day it was received, not its tacked donor
    date: a tax position for the owner to confirm, #241), whichever account they have moved through;
  - **or by the user.** A choice made after the moment of the disposal (Treas. Reg. §1.1012-1(j): no
    later than the date and time of the sale) is reported as late **only if** its lots differ from the
    standing method's (ADR 0021 §2), with the standing method's figures (ADR 0008 §4). The user's
    choice is used either way. The standing method's lots and figures come from a **full replay**
    (the owner's decision on #238): the events again, with every late choice disregarded and the
    standing method used instead, as the IRS might (ADR 0008 §4, T-508). So a choice is judged against
    what the standing method would have had left after the earlier late choices were disregarded, not
    after they were applied. In the replay, an on-time choice naming lots the replay doesn't hold in
    full there falls back, whole, to the standing method. The warning's lot ids are the replay's (a lot
    moved in the replay has its own `lot@transfer` id), and each figure carries its own basis and dates.
    The replay holds the same sats in every account as the real run, so the standing method is never
    short there; it **stops** wherever the two would part: when the runs disagree on whether a
    withdrawal creates a lot for unrecorded sats (one may count an account's lots as recorded and the
    other not), or when the replay can't apply an event at all (a fee that would use up the last sats
    of a gift's part). `Result.replay_stopped` then names the event, and every later late choice is
    judged and figured on the lots as the user's choices left them, its warning marked
    `replayed=False`: a late choice that matches the user's lots but not what the replay would have
    held is then not warned about. Reports must show a stopped replay (ADR 0040).
- **Splits are exact.** Bases and proceeds are split by sats with `domain.money.share`, always as a share
  of what remains, and the last part takes the remainder. So every split adds up to the whole, to the
  cent. All money arithmetic runs in the fixed `domain.money` context (T-502).
- **Holding period** (IRC §1222; Rev. Rul. 66-7): the day after acquisition starts it, and a disposal
  is long-term if it is more than one year later: after the first anniversary of the acquisition. A
  lot acquired on the last day of a month has its anniversary on the last day of that month a year
  later (Rev. Rul. 66-6, applied to the one-year period: a tax position for the owner to confirm,
  #239): bought 28 February 2023, it is long-term from 1 March 2024, not 29 February. An inherited lot
  is always long-term (IRC §1223(9)).
- **Movements never create lots** (ADR 0009). A **transfer** (`deposit` into an exchange account,
  `withdrawal` out of one, `self_transfer` between the user's own wallets) moves lots from one account
  to another: each moved part keeps its lot's basis, dates and status, under a new id (`lot@transfer`),
  and keeps its FIFO place: the date it reached the user (a gift by the day it was received, #241).
  Lots are chosen as for a disposal, with the same late check (ADR 0008 §3 and §4); a late choice's
  warning shows the standing method's whole result, its fee disposal included (dust the standing
  method could not carry is shown as its own parts, none arriving: the user's choice still stands).
  **A self-transfer is refused** (`EngineError`) until UTXO tracing exists (the owner's decision on
  #243): between the user's own wallets the spent outputs identify the lots (ADR 0008 §2, ADR 0009), and
  the account's FIFO order would silently give other figures. **Deposits and spends from a self-custody
  wallet have the same gap** and still use the account's order: the engine doesn't know which accounts
  are wallets. UTXO tracing, the next M6 slice, covers all three, and lifts the refusal (or limits it,
  once a wallet can record whole-wallet FIFO, ADR 0008 §2); it blocks release.
  - **A network fee** on a deposit or self-transfer (`fee_sats` of the sats that leave) is, by default,
    no disposal: its basis stays with the coins that arrive. With `fee_treatment="dispose"` it is a
    small taxable disposal at `fee_value`, its FMV (ADR 0009's stated tax position, printed on reports).
  - **An exchange's BTC withdrawal fee** is a small disposal at its FMV (ADR 0009's default; no other
    treatment is offered yet).
  - A fee that is disposed needs its FMV (`fee_value`): without one, the engine refuses rather than book
    a loss at proceeds 0.00. A short transfer's fee value is split by the sats covered; the rest goes
    with the missing basis.
  - The fee is taken from the chosen lots in proportion to their sats, every moved part keeping at
    least one sat where it can, the smallest parts paying any rest first; a part that is all fee (dust
    only) carries its basis to the transfer's last moved part, which records it (`carried_from`).
  - **A withdrawal from an account with no lots the user recorded and no sats held** (its buys were
    never entered) creates a lot for its sats in that account first (ADR 0009, PLAN §7): with the
    basis and original date the user supplies (`missing_basis` and `missing_acquired`, both or
    neither), or with **unknown basis**, a blocking condition (T-509). The lot is listed in
    `Result.created`. A shortfall in an account that holds sats, or lots the user recorded, is missing
    basis, as for a disposal, never a new lot.
- **Not enough lots** for an automatic disposal is a **blocking condition** (ADR 0009): the lots that
  exist are used, and the rest of the disposal is reported as missing basis. Invalid input (a choice
  naming lots the account doesn't hold, a wrong type) raises `EngineError`.
"""

from __future__ import annotations

from bisect import bisect_right
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Final, Literal

from coinacct.domain.chain import MAX_SATS
from coinacct.domain.money import CONTEXT, add, share, signed_usd, subtract, usd

type AcquisitionKind = Literal["buy", "p2p_buy", "income", "inherit", "gift_in"]
type DisposalKind = Literal["sell", "spend", "gift_out"]
type Method = Literal["fifo"]
type TransferKind = Literal["deposit", "withdrawal", "self_transfer"]
type FeeTreatment = Literal["carry", "dispose"]
# How an allocation's basis was found: the lot's own basis; a gift's donor basis (a gain) or FMV at the
# gift (a loss); between the two, neither; or a gift's donor basis that isn't known (blocking).
type BasisRule = Literal["cost", "donor", "fmv_at_gift", "no_gain_or_loss", "unknown"]

ACQUISITION_KINDS: Final[frozenset[str]] = frozenset({"buy", "p2p_buy", "income", "inherit", "gift_in"})
DISPOSAL_KINDS: Final[frozenset[str]] = frozenset({"sell", "spend", "gift_out"})
METHODS: Final[frozenset[str]] = frozenset({"fifo"})
TRANSFER_KINDS: Final[frozenset[str]] = frozenset({"deposit", "withdrawal", "self_transfer"})
FEE_TREATMENTS: Final[frozenset[str]] = frozenset({"carry", "dispose"})
ZERO: Final = Decimal("0.00")
GENESIS: Final = date(2009, 1, 3)  # no bitcoin was acquired before the genesis block
RESERVED: Final = ("@",)  # the separator in the lot ids the engine builds (lot@transfer)
UNRECORDED: Final = "@@unrecorded"  # a created lot's suffix: no event or moved lot id can contain "@@"


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


@dataclass(frozen=True)
class Transfer:
    id: str
    account: str  # where the coins leave
    to: str  # where they arrive
    on: date  # the tax date
    at: datetime  # the moment, timezone-aware UTC
    kind: TransferKind
    sats: int  # the sats that leave `account`, the fee included
    fee_sats: int = 0  # of those, the network or withdrawal fee
    fee_value: Decimal | None = None  # USD FMV of the fee sats: a fee disposal's proceeds
    picks: tuple[Pick, ...] | None = None  # None: the source account's standing method
    identified_at: datetime | None = None  # when the user chose `picks`, timezone-aware UTC
    missing_basis: Decimal | None = None  # withdrawal only: the basis of sats the account holds no lots for
    missing_acquired: date | None = None  # withdrawal only: and their acquisition date


type Event = Acquisition | Disposal | Transfer


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
class Moved:
    """A transfer's move of part of a lot: `sats` arrive in `to` as lot `into`, carrying `basis` (the fee's
    basis too, when it was carried) and the lot's dates and status."""

    transfer: str
    lot: str
    into: str
    to: str
    sats: int
    basis: Decimal
    acquired: date
    carried_fee: int = 0  # fee sats whose basis this part carries (the carry treatment)
    carried_from: tuple[str, ...] = ()  # other lots whose all-fee dust parts' basis this part carries


@dataclass(frozen=True)
class LateIdentification:
    """A choice made after its disposal whose lots differ from the standing method's (ADR 0021 §2).
    `standing` is what the standing method would have used, with its figures, shown with the warning
    (ADR 0008 §4): allocations for a sale or spend, gift records (no gain or loss) for a gift given, and
    for a transfer, its fee disposal's allocations (if the fee is disposed) and the parts it would move."""

    disposal: str
    identified_at: datetime
    standing: tuple[Allocation, ...] | tuple[GiftGiven, ...] | tuple[Allocation | Moved, ...]
    replayed: bool = True  # figured in the replay; False once it had stopped (Result.replay_stopped)


@dataclass(frozen=True)
class MissingLots:
    """Blocking: a disposal's sats the account holds no lots for (ADR 0009)."""

    disposal: str
    sats: int
    proceeds: Decimal


@dataclass(frozen=True)
class UnknownBasis:
    """Blocking: a lot whose basis isn't known (ADR 0009, T-509): a gift received without its donor's
    basis, or a lot created for a withdrawal from an account without lots. The user enters one; zero is
    a conservative resolution (the legal default for a gift is in Treas. Reg. §1.1015-1(a)(3))."""

    lot: str


@dataclass(frozen=True)
class Result:
    allocations: tuple[Allocation, ...]
    gifts: tuple[GiftGiven, ...]
    holdings: tuple[Lot, ...]  # the lots left, in the order they were created or moved in, without empty ones
    late: tuple[LateIdentification, ...]
    blocking: tuple[MissingLots | UnknownBasis, ...]
    moves: tuple[Moved, ...] = ()
    created: tuple[Lot, ...] = ()  # lots created for a withdrawal from an account without lots (ADR 0009)
    replay_stopped: str | None = None  # the event the late choices' replay couldn't follow (#238)


@dataclass(frozen=True)
class _Late:
    """A late choice that differs from the standing method: the engine that judged it (the replay, or
    the real run once the replay has stopped) and the standing method's picks there."""

    engine: _Engine
    picks: tuple[Pick, ...]
    replayed: bool


@dataclass
class _Open:
    lot: Lot
    sats: int
    basis: Decimal
    loss_basis: Decimal | None
    gift: bool
    fifo: date  # the date the lot reached the user: its FIFO place in every account
    created: bool = (
        False  # made by the engine for a withdrawal from an account without lots, or moved from one
    )


def long_term(acquired: date, disposed: date) -> bool:
    """Held more than one year: disposed after the first anniversary of the acquisition (see the module
    docstring for the last day of a month)."""
    year = acquired.year + 1
    if acquired.day == monthrange(acquired.year, acquired.month)[1]:
        anniversary = date(year, acquired.month, monthrange(year, acquired.month)[1])
    else:  # not the last day of its month, so the same day exists a year later
        anniversary = date(year, acquired.month, acquired.day)
    return disposed > anniversary


def run(
    events: Sequence[Event],
    methods: Mapping[str, Method] | None = None,
    fee_treatment: FeeTreatment = "carry",
) -> Result:
    """Apply `events` in order. `methods` holds each account's standing method (FIFO if absent);
    `fee_treatment` is the user's choice for network fees on deposits and self-transfers (ADR 0009)."""
    if fee_treatment not in FEE_TREATMENTS:
        raise EngineError(f"unknown fee treatment {fee_treatment!r}")
    with localcontext(CONTEXT):
        return _Engine(methods or {}, fee_treatment).run(events)


class _Engine:
    def __init__(
        self, methods: Mapping[str, Method], fee_treatment: FeeTreatment = "carry", *, replay: bool = False
    ) -> None:
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
        self._moves: list[Moved] = []
        self._created: list[Lot] = []
        self._recorded: set[str] = set()  # accounts holding lots the user recorded (not created lots)
        self._keys: dict[str, list[date]] = {}  # account -> its lots' FIFO keys, in step with _open
        self._fee_treatment = fee_treatment
        # The replay (#238): the same events with every late choice disregarded. Only the real run has
        # one; the replay itself is `replay=True` and records no warnings.
        self._replay = replay
        self._shadow: _Engine | None = None if replay else _Engine(methods, fee_treatment, replay=True)
        self._replay_stopped: str | None = None

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
                self._follow(event)
                continue
            _check_moment(event.at, f"event {event.id!r}: its moment")
            if abs((event.on - event.at.date()).days) > 1:
                raise EngineError(f"event {event.id!r}: its tax date is more than a day from its moment")
            if last_moment is not None and event.at < last_moment:
                raise EngineError(f"event {event.id!r} is out of time order")
            last_moment = event.at
            if isinstance(event, Transfer):
                self._transfer(event)
            else:
                self._dispose(event)
            self._follow(event)
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
            tuple(self._allocations),
            tuple(self._gifts),
            holdings,
            tuple(self._late),
            tuple(self._blocking),
            tuple(self._moves),
            tuple(self._created),
            self._replay_stopped,
        )

    def _follow(self, event: Event) -> None:
        """Apply `event` to the replay too, after the real run accepted it. An event the replay can't
        apply, or one where only one of the runs creates a lot for unrecorded sats, stops the replay
        there: so while it runs, it holds the same sats in every account as the real run."""
        shadow = self._shadow
        if shadow is None:
            return
        try:
            if isinstance(event, Acquisition):
                shadow._acquire(event)
            elif isinstance(event, Transfer):
                shadow._transfer(event)
            else:
                shadow._dispose(event)
        except EngineError:
            self._shadow, self._replay_stopped = None, event.id
            return
        if len(shadow._created) != len(self._created):  # one run made a lot the other didn't
            self._shadow, self._replay_stopped = None, event.id

    def _acquire(self, a: Acquisition) -> None:
        if a.kind not in ACQUISITION_KINDS:
            raise EngineError(f"acquisition {a.id!r}: unknown kind {a.kind!r}")
        loss_basis: Decimal | None = None
        loss_from: date | None = None
        unknown = False
        acquired = a.on
        if type(a.donor_always_long) is not bool:
            raise EngineError(f"acquisition {a.id!r}: donor_always_long is True or False")
        always_long = a.kind == "inherit" or (a.kind == "gift_in" and a.donor_always_long)
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
        entry = _Open(lot, a.sats, basis, loss_basis, a.kind == "gift_in", a.on)
        self._insert(a.account, entry)
        self._recorded.add(a.account)

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

    def _choose(self, e: Disposal | Transfer) -> tuple[list[Pick], int, _Late | None]:
        """The lots `e` uses: the standing method's, or the user's checked picks; the sats the account
        holds no lots for (standing method only); and, for a late choice whose lots differ from the
        standing method's in the replay (ADR 0021 §2, #238), what the standing method uses there."""
        if e.picks is None:
            if e.identified_at is not None:
                raise EngineError(f"event {e.id!r}: a standing-method choice has no identification time")
            picks, missing = self._standing(e.account, e.sats)
            return picks, missing, None
        if self._replay:  # the replay disregards a late choice, and an on-time one it can't follow
            if e.identified_at is not None and e.identified_at <= e.at:
                try:
                    return self._checked_picks(e, e.picks), 0, None
                except EngineError:
                    pass
            picks, missing = self._standing(e.account, e.sats)
            return picks, missing, None
        if e.identified_at is None:
            raise EngineError(f"event {e.id!r}: a chosen set of lots needs the time it was chosen")
        _check_moment(e.identified_at, f"event {e.id!r}: its identification time")
        picks = self._checked_picks(e, e.picks)
        if e.identified_at > e.at:
            judge = self._shadow or self  # the replay, or these lots once it has stopped
            # the same sats as the real run (see _follow), so never short: valid picks cover the sats
            standing, _ = judge._standing(e.account, e.sats)
            if _merged(picks) != _merged(standing):
                return picks, 0, _Late(judge, tuple(standing), judge is not self)
        return picks, 0, None

    def _dispose(self, d: Disposal) -> None:
        if d.kind not in DISPOSAL_KINDS:
            raise EngineError(f"disposal {d.id!r}: unknown kind {d.kind!r}")
        proceeds = _signed_usd(d.proceeds, d.id)
        if d.kind == "gift_out" and proceeds != 0:
            raise EngineError(f"disposal {d.id!r}: a gift given has no proceeds")
        picks, missing, late = self._choose(d)
        if late is not None and d.identified_at is not None:
            judge = late.engine
            figures, _ = judge._split(d, proceeds, late.picks, apply=False)
            alternative = tuple(judge._gift(a) for a in figures) if d.kind == "gift_out" else tuple(figures)
            self._late.append(LateIdentification(d.id, d.identified_at, alternative, late.replayed))
        allocations, rest = self._split(d, proceeds, picks, apply=True)
        if d.kind == "gift_out":
            self._gifts.extend(self._gift(a) for a in allocations)
        else:
            self._allocations.extend(allocations)
        if missing:  # the standing picks covered all but `missing` sats; `rest` is their proceeds
            self._blocking.append(MissingLots(d.id, missing, rest))

    def _transfer(self, t: Transfer) -> None:
        fee_value = _checked_transfer(t, self._fee_treatment)
        if t.kind == "self_transfer":
            raise EngineError(
                f"transfer {t.id!r}: a self-transfer's lots follow the outputs it spends, which needs UTXO"
                " tracing (not built yet, #243)"
            )
        self._unrecorded_if_needed(t)
        picks, missing, late = self._choose(t)
        if late is not None and t.identified_at is not None:
            alternative = late.engine._alternative(t, late.picks, fee_value)
            self._late.append(LateIdentification(t.id, t.identified_at, alternative, late.replayed))
        # A shortfall is blocking anyway; the fee comes out of what is covered, leaving a sat to arrive,
        # and its value follows its sats: the uncovered part goes with the missing basis.
        picks = self._in_fifo_order(t.account, picks)
        covered = sum(p.sats for p in picks)
        fees = _fee_shares(min(t.fee_sats, covered - 1), picks) if picks else []
        charged = sum(fees)
        value = share(fee_value, charged, t.fee_sats) if t.fee_sats else ZERO
        if missing:
            self._blocking.append(MissingLots(t.id, missing, subtract(fee_value, value)))
        if not picks:
            return
        dispose = t.kind == "withdrawal" or self._fee_treatment == "dispose"
        if dispose and charged:
            fee_picks = [Pick(p.lot, f) for p, f in zip(picks, fees, strict=True) if f]
            disposal = Disposal(t.id, t.account, t.on, t.at, "spend", charged, value)
            allocations, _ = self._split(disposal, value, fee_picks, apply=True)
            self._allocations.extend(allocations)
        orphans: list[tuple[_Open, Decimal]] = []
        moved: list[Moved] = []
        for pick, fee in zip(picks, fees, strict=True):
            moving = pick.sats - fee
            if dispose:
                if moving:
                    moved.append(self._move(t, pick.lot, moving))
                continue
            if moving:  # carry: the whole part leaves the lot, its fee's basis with the sats that arrive
                moved.append(self._move(t, pick.lot, pick.sats, arrive=moving))
            else:  # a part that is all fee (dust): its basis goes to a moved part of the same kind
                o = self._lots[pick.lot]
                orphans.append((o, self._take(pick.lot, pick.sats)[0]))
        if orphans:  # a moved part always exists: fees leave a sat to arrive
            sources = [o for o, _ in orphans]
            target = _dust_target(moved, sources, {m.into: self._lots[m.lot] for m in moved}, t.id)
            self._carry(
                target,
                sum((b for _, b in orphans), ZERO),
                sum(f for p, f in zip(picks, fees, strict=True) if p.sats == f),
                tuple(o.lot.id for o in sources),
            )

    def _unrecorded_if_needed(self, t: Transfer) -> None:
        """For a withdrawal from an account without lots (its buys were never entered), a lot for its sats
        (ADR 0009). In an account with lots a shortfall is missing basis, never a new lot."""
        held = any(o.sats for o in self._open.get(t.account, []))
        if t.kind == "withdrawal" and t.account not in self._recorded and not held:
            if t.picks is not None:
                raise EngineError(f"transfer {t.id!r}: an account without lots has none to choose")
            self._unrecorded(t, t.sats)
        elif t.missing_basis is not None:
            raise EngineError(
                f"transfer {t.id!r}: the account has lots, so unrecorded sats are missing basis"
            )

    def _unrecorded(self, t: Transfer, sats: int) -> None:
        """A lot, in the source account, for a withdrawal from an account without lots (ADR 0009)."""
        lot_id = f"{t.id}{UNRECORDED}"
        if t.missing_basis is None:
            basis, unknown = ZERO, True
            self._blocking.append(UnknownBasis(lot_id))
        else:
            basis, unknown = _usd(t.missing_basis, t.id), False
        acquired = t.missing_acquired if t.missing_acquired is not None else t.on
        lot = Lot(lot_id, t.account, acquired, sats, basis, False, None, None, unknown)
        self._insert(t.account, _Open(lot, sats, basis, None, False, acquired, created=True))
        self._created.append(lot)

    def _take(self, lot_id: str, sats: int) -> tuple[Decimal, Decimal | None]:
        """Take `sats` from a lot: their shares of its bases, the lot used up by them."""
        o = self._lots[lot_id]
        basis = share(o.basis, sats, o.sats)
        loss = None if o.loss_basis is None else share(o.loss_basis, sats, o.sats)
        rest = subtract(o.basis, basis)
        loss_rest = None if o.loss_basis is None or loss is None else min(subtract(o.loss_basis, loss), rest)
        o.sats, o.basis, o.loss_basis = o.sats - sats, rest, loss_rest
        return basis, loss

    def _in_fifo_order(self, account: str, picks: Sequence[Pick]) -> list[Pick]:
        """`picks` in the account's FIFO order, so a transfer's fee roles and its dust carry depend only
        on which lots it uses, not on the order they were named in."""
        place = {o.lot.id: k for k, o in enumerate(self._open.get(account, []))}
        return sorted(picks, key=lambda p: place[p.lot])

    def _alternative(
        self, t: Transfer, picks: Sequence[Pick], fee_value: Decimal
    ) -> tuple[Allocation | Moved, ...]:
        """What `picks` would give, nothing used up: the fee disposal's allocations (when the fee is
        disposed) and the parts that would arrive, with their bases (ADR 0008 §4). Dust parts that would
        be all fee are left out."""
        picks = self._in_fifo_order(t.account, picks)
        fees = _fee_shares(min(t.fee_sats, sum(p.sats for p in picks) - 1), picks)
        dispose = t.kind == "withdrawal" or self._fee_treatment == "dispose"
        out: list[Allocation | Moved] = []
        orphans: list[tuple[_Open, Decimal, int]] = []
        charged = sum(fees)
        if dispose and charged > 0:  # the fee's value follows its sats, as in the real run
            value = share(fee_value, charged, t.fee_sats)
            disposal = Disposal(t.id, t.account, t.on, t.at, "spend", charged, value)
            fee_picks = [Pick(p.lot, f) for p, f in zip(picks, fees, strict=True) if f]
            out.extend(self._split(disposal, value, fee_picks, apply=False)[0])
        for pick, fee in zip(picks, fees, strict=True):
            o = self._lots[pick.lot]
            moving = pick.sats - fee
            if not moving:
                # all fee: under carry its share of the lot's basis goes to a moved part, as in the real run
                orphans.append((o, share(o.basis, pick.sats, o.sats), pick.sats))
                continue
            if dispose:  # the fee's basis leaves with the fee: the rest is a share of what remains
                fee_basis = share(o.basis, fee, o.sats)
                basis = share(subtract(o.basis, fee_basis), moving, o.sats - fee)
                out.append(Moved(t.id, pick.lot, f"{pick.lot}@{t.id}", t.to, moving, basis, o.lot.acquired))
            else:  # carry: the whole part's basis arrives with the sats that arrive
                basis = share(o.basis, pick.sats, o.sats)
                out.append(
                    Moved(t.id, pick.lot, f"{pick.lot}@{t.id}", t.to, moving, basis, o.lot.acquired, fee)
                )
        if orphans and not dispose:
            moved = [m for m in out if isinstance(m, Moved)]
            sources = [o for o, _, _ in orphans]
            lots = {m.into: self._lots[m.lot] for m in moved}
            if all(_plain(o) for o in sources) and any(_plain(lots[m.into]) for m in moved):
                target = _dust_target(moved, sources, lots, t.id)
                i = out.index(target)
                out[i] = Moved(
                    target.transfer,
                    target.lot,
                    target.into,
                    target.to,
                    target.sats,
                    add(target.basis, sum((b for _, b, _ in orphans), ZERO)),
                    target.acquired,
                    target.carried_fee + sum(n for _, _, n in orphans),
                    tuple(o.lot.id for o, _, _ in orphans),
                )
            else:  # dust the real run would refuse: shown as its own parts, none arriving (warn-only)
                out.extend(
                    Moved(t.id, o.lot.id, f"{o.lot.id}@{t.id}", t.to, 0, b, o.lot.acquired, n)
                    for o, b, n in orphans
                )
        return tuple(out)

    def _move(self, t: Transfer, lot_id: str, sats: int, *, arrive: int | None = None) -> Moved:
        """Move `sats` of a lot to `t.to` (`arrive` of them arrive: the rest was a carried fee)."""
        o = self._lots[lot_id]
        into = f"{lot_id}@{t.id}"
        basis, loss = self._take(lot_id, sats)
        arriving = sats if arrive is None else arrive
        lot = Lot(
            into,
            t.to,
            o.lot.acquired,
            arriving,
            basis,
            o.lot.always_long,
            loss,
            o.lot.loss_from if loss is not None else None,
            o.lot.unknown_basis,
        )
        self._insert(t.to, _Open(lot, arriving, basis, loss, o.gift, o.fifo, created=o.created))
        if not o.created:  # a created lot, however far it moves, is still not one the user recorded
            self._recorded.add(t.to)
        moved = Moved(t.id, lot_id, into, t.to, arriving, basis, o.lot.acquired, sats - arriving)
        self._moves.append(moved)
        return moved

    def _carry(self, moved: Moved, basis: Decimal, sats: int, lots: tuple[str, ...]) -> None:
        """Add the basis of fee parts (`sats` of them) to a moved part (the carry treatment's dust case)."""
        entry = self._lots[moved.into]
        entry.basis = add(entry.basis, basis)
        i = next(k for k in range(len(self._moves) - 1, -1, -1) if self._moves[k].into == moved.into)
        self._moves[i] = Moved(
            moved.transfer,
            moved.lot,
            moved.into,
            moved.to,
            moved.sats,
            entry.basis,
            moved.acquired,
            moved.carried_fee + sats,
            moved.carried_from + lots,
        )

    def _insert(self, account: str, entry: _Open) -> None:
        """Place a lot in an account's FIFO order by the date it reached the user (after lots of the same
        date). Acquisitions arrive in date order, so each account's list stays sorted by that key."""
        if entry.lot.id in self._lots:
            raise EngineError(f"lot {entry.lot.id!r} exists already")
        lots = self._open.setdefault(account, [])
        keys = self._keys.setdefault(account, [])
        at = bisect_right(keys, entry.fifo)
        lots.insert(at, entry)
        keys.insert(at, entry.fifo)
        self._first[account] = min(self._first.get(account, 0), at)
        self._lots[entry.lot.id] = entry

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
            # The loss basis stays (decided at the gift), but never above the gain basis left. The cap
            # never binds in practice: the bases differ by at least a cent and a partial take's shares
            # by less, so half-to-even rounding can't reverse their order; it bounds, never drops, cents.
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
        # A loss against the FMV is held from the gift date only (Treas. Reg. §1.1223-1(b)): the donor's
        # inherited long-term status tacks only where the donor's basis is used.
        term = (lot.always_long and rule != "fmv_at_gift") or long_term(acquired, d.on)
        lot_basis = None if used == basis else basis
        return Allocation(d.id, lot.id, sats, used, proceeds, acquired, d.on, term, rule, lot_basis, loss)

    def _checked_picks(self, d: Disposal | Transfer, chosen: tuple[Pick, ...]) -> list[Pick]:
        """The user's picks, checked, with repeats of a lot merged (first-seen order)."""
        wanted: dict[str, int] = {}
        for pick in chosen:
            if not isinstance(pick, Pick) or not isinstance(pick.lot, str) or not pick.lot:
                raise EngineError(f"event {d.id!r}: a choice is a tuple of Picks naming lots")
            _check_sats(pick.sats, f"event {d.id!r}")
            entry = self._lots.get(pick.lot)
            if entry is None or entry.lot.account != d.account:
                raise EngineError(f"event {d.id!r}: lot {pick.lot!r} isn't held in account {d.account!r}")
            wanted[pick.lot] = wanted.get(pick.lot, 0) + pick.sats
            if wanted[pick.lot] > entry.sats:
                raise EngineError(f"event {d.id!r}: lot {pick.lot!r} holds fewer sats than chosen")
        if sum(wanted.values()) != d.sats:
            raise EngineError(f"event {d.id!r}: the chosen lots don't add up to its sats")
        return [Pick(lot, sats) for lot, sats in wanted.items()]


def _checked_transfer(t: Transfer, fee_treatment: FeeTreatment) -> Decimal:
    """Check a transfer's own fields; return its fee value (0.00 when its fee isn't disposed)."""
    if t.kind not in TRANSFER_KINDS:
        raise EngineError(f"transfer {t.id!r}: unknown kind {t.kind!r}")
    if not isinstance(t.to, str) or not t.to or t.to == t.account:
        raise EngineError(f"transfer {t.id!r}: it moves coins to another account")
    if isinstance(t.fee_sats, bool) or not isinstance(t.fee_sats, int) or not 0 <= t.fee_sats < t.sats:
        raise EngineError(f"transfer {t.id!r}: its fee is a whole number of sats below the sats it moves")
    disposed = t.fee_sats > 0 and (t.kind == "withdrawal" or fee_treatment == "dispose")
    if t.fee_value is None:
        if disposed:  # refuse rather than book a loss at proceeds 0.00
            raise EngineError(f"transfer {t.id!r}: a fee that is disposed needs its value (fee_value)")
        fee_value = ZERO
    else:
        fee_value = _usd(t.fee_value, t.id)
        if t.fee_sats == 0:
            raise EngineError(f"transfer {t.id!r}: a fee value needs fee sats")
        if not disposed:
            fee_value = ZERO  # a carried fee is no disposal: its value plays no part
    if t.kind != "withdrawal" and (t.missing_basis is not None or t.missing_acquired is not None):
        raise EngineError(f"transfer {t.id!r}: only a withdrawal names a basis for unrecorded sats")
    if (t.missing_basis is None) != (t.missing_acquired is None):
        raise EngineError(f"transfer {t.id!r}: unrecorded sats need both their basis and their original date")
    if t.missing_acquired is not None and (
        type(t.missing_acquired) is not date or not GENESIS <= t.missing_acquired <= t.on
    ):
        raise EngineError(f"transfer {t.id!r}: unrecorded sats' date must be from genesis to the withdrawal")
    if t.picks is not None and t.missing_basis is not None:
        raise EngineError(f"transfer {t.id!r}: a chosen set of lots leaves no sats unrecorded")
    return fee_value


def _fee_shares(fee: int, picks: Sequence[Pick]) -> list[int]:
    """The fee sats each pick pays, the picks in the account's FIFO order: in proportion to its sats,
    rounded down, then one more sat each to the picks with the largest remainders (ties in order),
    while each part keeps at least one sat. Any rest after that (dust: fewer sats than the parts can
    spare) is paid by the smallest picks first, which then arrive empty."""
    total = sum(p.sats for p in picks)
    shares = [fee * p.sats // total for p in picks]
    rest = fee - sum(shares)  # fewer than len(picks)
    for i in sorted(range(len(picks)), key=lambda i: (-(fee * picks[i].sats % total), i)):
        if rest and shares[i] < picks[i].sats - 1:
            shares[i] += 1
            rest -= 1
    for i in sorted(range(len(picks)), key=lambda i: (picks[i].sats, i)):
        room = min(rest, picks[i].sats - shares[i])
        shares[i] += room
        rest -= room
    return shares


def _plain(o: _Open) -> bool:
    """A lot with one known cost basis: not a gift (no donor's basis or loss basis) and not unknown."""
    return not o.gift and o.loss_basis is None and not o.lot.unknown_basis


def _dust_target(
    moved: Sequence[Moved], orphans: Sequence[_Open], lots: Mapping[str, _Open], tid: str
) -> Moved:
    """The moved part that takes all-fee dust parts' basis: the last plain one in FIFO order. Basis is
    only carried between plain lots (one known cost basis each): a gift's dual basis or an unknown
    basis can't be merged into another lot, so dust that is such a lot, or that has no plain moved part
    to go to, is refused rather than mis-stated."""
    plain = [m for m in moved if _plain(lots[m.into])]
    if not all(_plain(o) for o in orphans) or not plain:
        raise EngineError(
            f"transfer {tid!r}: a fee that uses up a part of a gift or unknown-basis lot, or with no plain "
            "lot moving to take its basis, can't carry the basis to another lot; use fee_treatment='dispose'"
        )
    return plain[-1]


def _merged(picks: Sequence[Pick]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for p in picks:
        merged[p.lot] = merged.get(p.lot, 0) + p.sats
    return merged


def _check_common(event: Event) -> None:
    if not isinstance(event, Acquisition | Disposal | Transfer):
        raise EngineError("an event is an Acquisition, a Disposal or a Transfer")
    if not (isinstance(event.id, str) and event.id and isinstance(event.account, str) and event.account):
        raise EngineError("an event names its id and its account")
    if any(c in event.id for c in RESERVED):  # the engine builds lot ids with them (lot@transfer)
        raise EngineError(f"event {event.id!r}: an id can't contain {' or '.join(RESERVED)}")
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

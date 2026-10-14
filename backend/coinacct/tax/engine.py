"""Lots in an account: acquisitions create them, sales, spends and gifts use them (PLAN §7; ADRs 0008,
0009, 0011, 0021).

Pure: events in, allocations and holdings out. The caller (`services/`) gives the events in time order:
by tax date, and disposals by moment. The engine never reads the clock. Each event carries its **tax
date** (`on`, already in the user's time zone, for the holding period); a disposal also carries the
**moment** it happened (`at`), and a user's choice of lots the moment it was made (`identified_at`):
both timezone-aware UTC (ENGINEERING §5.2). A disposal's tax date is within a day of its moment's UTC
date (time zones run from UTC-12 to UTC+14), and disposals come in the order of their moments.

- **Acquisitions** (`buy`, `p2p_buy`, `income`, `inherit`, `gift_in`) each create one lot: its sats, its
  basis (cost plus fees, or the FMV the caller computed) and its date.
- **The 2025 opening allocation** (`OpeningAllocation`; ADR 0008 §6, Rev. Proc. 2024-28 §5.02), dated
  2025-01-01 and before every other 2025 event, replaces an account's lots with the lots the user
  allocated its unattached basis to: each with its sats, basis and date (`Result.replaced` keeps the
  lots it replaced, for the audit trail). Allocating fewer sats than the account holds leaves a
  placeholder of unknown basis for the rest (blocking, T-509); allocating more blocks
  (`OpeningMismatch`). It is late (`Result.opening_late`, a warning, T-508) if the user recorded it after
  their first sale, disposition or transfer of BTC from any account on or after 2025-01-01, or after the
  2025 return's due date if that is earlier (ADR 0041 §5; the due date counts from the start of
  2026-04-15 UTC, the earliest reading). A coin-traced wallet's allocation (ADR 0041 §5: the pool and
  the fragments drawn from it) isn't built yet: it blocks (`Untraced`, `opening_allocation`).
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
    moved in the replay has its own `lot@transfer` id, which can equal a real-run id with other sats and
    basis), and each figure carries its own basis and dates: they are never keys into the `Result`.
    The replay holds the same sats in every account as the real run (checked after every event, for
    the accounts it touched), so the standing method is never short there; it **stops** wherever the
    two would part: when the runs disagree on whether a withdrawal creates a lot for unrecorded sats
    (one may count an account's lots as recorded and the other not), or when the replay can't apply an
    event at all. `Result.replay_stopped` then names the event, and every later late choice is
    judged and figured on the lots as the user's choices left them, its warning marked
    `replayed=False`: a late choice that matches the user's lots but not what the replay would have
    held is then not warned about. Reports must show a stopped replay (ADR 0040). A late choice at the
    very event the replay stops at is still figured in the replay (its state before the event), so
    `replayed=True`.
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
  warning shows the standing method's whole result: the parts it would move, with their bases. A
  choice may also name only the sats that arrive: the fee then takes the standing method's next sats
  besides them (ADR 0043). Those fee lots are part of the identification: the late check judges the
  whole, which sats arrive included, and the replay follows the whole (or falls back, whole).
- **Coin-traced wallets** (`run(..., wallets={account: Wallet(tracking_start)})`, ADR 0041): in a
  self-custody wallet the coins a transaction spends identify the lots that leave (ADR 0008 §2). An
  arrival (an acquisition, or a withdrawal's moved lots) **links** the outputs it arrived in (`coins`);
  its lots, oldest first, fill them in the order given. Links over the sats that arrived block
  (`Untraced`, `bad_link`). A wallet's spends come only as a `WalletTx`: its inputs and outputs from the
  chain, and its one leaving event (a sale, spend or gift, a deposit, or a self-transfer to another
  coin-traced wallet), or none (a consolidation). `tracing.trace` decides which lots each output gets (ADR
  0041 §2):
  - a sale's or spend's lots are its outputs' and the fee's (the fee's basis is in the disposal); a
    gift's are its outputs', and the fee's basis isn't recovered (ADR 0043)
  - a deposit or self-transfer moves its outputs' lots, as a transfer does; the fee's basis passes to
    the lot's fragment on the first leaving output in the canonical order that holds it, else to the
    first leaving output's oldest fragment (ADR 0041 §2.4)
  - a consolidation keeps the fee's basis in the wallet, on the change
  - the change stays on the wallet's new coins, under the same lots.

  A transaction the engine can't trace blocks (`Untraced`, ADR 0041 §3): one tracing blocks, a coin
  whose links don't hold exactly its value (`uncovered`), a coin from another account, a self-transfer
  to an account that isn't coin-traced. Its leaving event is then figured by the account's FIFO order,
  provisionally (its fee's basis following ADR 0043's rule for a whole-wallet FIFO wallet: a sale's
  row, a consolidation's same or oldest remaining lot), and the wallet's later transactions block too
  (`after_untraced`), since its coins no longer match its lots; its coin records are dropped. A
  self-transfer from an account that isn't coin-traced blocks (`mixed_transfer`), figured by
  FIFO provisionally, and a coin-traced wallet it arrives in is untraced from then on. Exchange
  accounts are unchanged.

  **The caller's part** (`services/`, ADR 0041 §6): the engine can't tell an exchange account from a
  self-custody one, so the caller passes **every** self-custody account in `wallets` (whole-wallet
  FIFO, ADR 0041 §5, isn't built: a self-custody account left out is figured by its FIFO order, as an
  exchange account, without a block, except a self-transfer from it, which blocks: `mixed_transfer`).
  It also checks what needs the chain cache or the clock: that a link's output is the account's, that its
  txid is the event's, the two-hour allowance (§1, §4), that every transaction touching a wallet coin is
  confirmed (§5), and that a wallet's tracking start isn't later than when the user entered it (§4).
  The engine needs events in time order and a coin's links before its `Received`; block times aren't
  monotonic, and an allowed link can come up to two hours after its coin's block time, so the caller
  orders chain events by their place in the chain and gives each one a moment no earlier than the last
  (#299 holds the open question of how). The engine checks links over the sats that
  arrived and over the output's value (`Coin.value`, with the links before it), and a coin's value
  when it is spent.
  - **The pool** (ADR 0041 §4) is a wallet's lots on no coin: acquisitions and withdrawals into it that
    link no output, such as a balance from before the wallet was tracked. A coin from outside the
    user's wallets (`Received`, in chain order, after the events that link it) has its part no link
    covers drawn from the pool, oldest first, if it arrived before the wallet's tracking start
    (`Result.draws`); only lots already in the pool count, in the caller's order. A pool
    short of it leaves a **placeholder** of unknown basis on the coin (`Result.placeholders`, blocking,
    T-509). After the tracking start, or with none set, that part blocks (`unrecorded_receipt`,
    `no_tracking_start`). A coin received after the transaction that spent it, or one another wallet's
    links hold, raises; one received into an untraced wallet draws nothing. Lots still in the pool at
    the end are listed (`Result.pool`, untraced wallets left out), for the warning that they are
    probably a duplicate purchase or an unrecorded sale.
  - **A fee is never a taxable event** (ADR 0043): a network fee on a deposit, or an exchange's BTC
    withdrawal fee (`fee_sats` of the sats that leave), is no disposal and has no Form 8949 row. The
    sats that arrive take the transfer's lots first, in the account's FIFO order; the fee takes the
    last sats. A lot that both arrives and pays fee keeps the fee's basis on its moved part; a lot that
    the fee uses up entirely (dust) passes its basis to the transfer's oldest moved part (first in the
    account's FIFO order, whatever its basis), which records it (`carried_from`). From a gift's lot
    (two bases) the lower one passes, and a moved part with two bases takes it on both. A lot of
    unknown basis passes none, and stays blocking.
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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal, localcontext
from typing import Final, Literal

from coinacct.domain.chain import MAX_SATS
from coinacct.domain.money import CONTEXT, add, share, signed_usd, subtract, usd
from coinacct.tax import tracing
from coinacct.tax.errors import EngineError as EngineError  # noqa: PLC0414  (re-exported for callers)

type AcquisitionKind = Literal["buy", "p2p_buy", "income", "inherit", "gift_in"]
type DisposalKind = Literal["sell", "spend", "gift_out"]
type Method = Literal["fifo"]
type TransferKind = Literal["deposit", "withdrawal", "self_transfer"]
# How an allocation's basis was found: the lot's own basis; a gift's donor basis (a gain) or FMV at the
# gift (a loss); between the two, neither; or a gift's donor basis that isn't known (blocking).
type BasisRule = Literal["cost", "donor", "fmv_at_gift", "no_gain_or_loss", "unknown"]

ACQUISITION_KINDS: Final[frozenset[str]] = frozenset({"buy", "p2p_buy", "income", "inherit", "gift_in"})
DISPOSAL_KINDS: Final[frozenset[str]] = frozenset({"sell", "spend", "gift_out"})
METHODS: Final[frozenset[str]] = frozenset({"fifo"})
TRANSFER_KINDS: Final[frozenset[str]] = frozenset({"deposit", "withdrawal", "self_transfer"})
ZERO: Final = Decimal("0.00")
GENESIS: Final = date(2009, 1, 3)  # no bitcoin was acquired before the genesis block
OPENING_DAY: Final = date(2025, 1, 1)  # the 2025 opening allocation's date (Rev. Proc. 2024-28)
RETURN_DUE_2025: Final = datetime(2026, 4, 15, tzinfo=UTC)  # the 2025 return's due date, its earliest moment
RESERVED: Final = ("@", "#")  # the separators in the lot ids the engine builds (lot@transfer, lot@tx#vout)
SPLIT: Final = "#"  # in a per-output moved or split-off lot's id: lot@event#vout
UNRECORDED: Final = "@@unrecorded"  # a created lot's suffix: no event or moved lot id can contain "@@"
PLACEHOLDER: Final = "@@placeholder"  # a placeholder lot's suffix (ADR 0041 §4), after a received coin's id


@dataclass(frozen=True)
class Coin:
    """A link (ADR 0041 §1): `sats` of an arrival event's lots are on the wallet output `outpoint`
    ("txid:vout"), whose value is `value` (from the chain cache)."""

    outpoint: str
    sats: int
    value: int


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
    coins: tuple[Coin, ...] = ()  # into a coin-traced wallet: the outputs its lot arrived in (ADR 0041 §1)


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
    picks: tuple[Pick, ...] | None = None  # None: the source account's standing method
    identified_at: datetime | None = None  # when the user chose `picks`, timezone-aware UTC
    missing_basis: Decimal | None = None  # withdrawal only: the basis of sats the account holds no lots for
    missing_acquired: date | None = None  # withdrawal only: and their acquisition date
    coins: tuple[Coin, ...] = ()  # a withdrawal into a coin-traced wallet: the outputs its lots arrived in


@dataclass(frozen=True)
class Spent:
    """A coin a wallet transaction spends, in input (vin) order: its outpoint ("txid:vout"), its value,
    and its owner among the user's accounts (None: not the user's)."""

    outpoint: str
    value: int
    account: str | None


@dataclass(frozen=True)
class WalletTx:
    """A transaction spending coins of a coin-traced wallet (ADR 0041 §2): its inputs and outputs from
    the chain (each output's `event` names the leaving event it pays), and its one leaving event, if
    any: a sale, spend or gift (`Disposal`), or a deposit or self-transfer (`Transfer`), from this
    account, at this moment, choosing no lots (the coins identify them). The fee is what the inputs
    hold beyond the outputs; a leaving transfer's `fee_sats` must equal it, and a leaving disposal's
    `sats` is what its outputs receive. No leaving event: a consolidation."""

    id: str
    account: str
    on: date
    at: datetime
    txid: str
    inputs: tuple[Spent, ...]
    outputs: tuple[tracing.Output, ...]
    leaving: Disposal | Transfer | None = None
    # further leaving events (two payments, a payment and a deposit): unsupported, so the transaction
    # blocks (ADR 0041 §3), each figured by FIFO provisionally
    also: tuple[Disposal | Transfer, ...] = ()

    @property
    def sats(self) -> int:
        return sum(i.value for i in self.inputs)


@dataclass(frozen=True)
class Received:
    """A coin from outside the user's wallets arriving in a coin-traced wallet (ADR 0041 §4), in chain
    order (block height, then position in the block, then the canonical order). The part of it no link
    covers draws from the wallet's pool if it arrived before the wallet's tracking start, and blocks
    otherwise. Links to it must come before it."""

    id: str
    account: str
    on: date
    at: datetime  # its block time, timezone-aware UTC
    outpoint: str
    value: int

    @property
    def sats(self) -> int:
        return self.value


@dataclass(frozen=True)
class Wallet:
    """A coin-traced self-custody account: the time from which every coin sent to it from outside is
    recorded (ADR 0041 §4), set by the user; None if not set yet: the wallet then blocks (an `Untraced`
    naming the account, ADR 0041 §3), and so does each received coin's part no link covers."""

    tracking_start: datetime | None


@dataclass(frozen=True)
class OpeningLot:
    """A lot of a 2025 opening allocation: the sats the user allocated basis to, that basis (USD) and the
    date the holding period runs from; `always_long` for an inherited lot."""

    sats: int
    basis: Decimal
    acquired: date
    always_long: bool = False


@dataclass(frozen=True)
class OpeningAllocation:
    """The 2025 opening allocation of an account (ADR 0008 §6): its lots on 2025-01-01, the moment the
    user recorded it (`recorded_at`, timezone-aware UTC) and the method they state they used."""

    id: str
    account: str
    on: date
    lots: tuple[OpeningLot, ...]
    recorded_at: datetime
    method: str

    @property
    def sats(self) -> int:
        return sum(lot.sats for lot in self.lots) if isinstance(self.lots, tuple) else 0


type Event = Acquisition | Disposal | Transfer | WalletTx | Received | OpeningAllocation


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
    carried_fee: int = 0  # a wallet disposal's fee sats whose basis this row carries (ADR 0043)
    carried_from: tuple[str, ...] = ()  # the lots those fee sats came from

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
    carried_fee: int = 0  # fee sats whose basis this part carries (ADR 0043: no fee is a disposal)
    carried_from: tuple[str, ...] = ()  # other lots whose all-fee dust parts' basis this part carries


@dataclass(frozen=True)
class LateIdentification:
    """A choice made after its disposal whose lots differ from the standing method's (ADR 0021 §2).
    `standing` is what the standing method would have used, with its figures, shown with the warning
    (ADR 0008 §4): allocations for a sale or spend, gift records (no gain or loss) for a gift given, and
    for a transfer, the parts it would move (no fee is a disposal, ADR 0043)."""

    disposal: str
    identified_at: datetime
    standing: tuple[Allocation, ...] | tuple[GiftGiven, ...] | tuple[Moved, ...]
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
    basis, a lot created for a withdrawal from an account without lots, or a received coin's placeholder
    (ADR 0041 §4). The user enters one; zero is
    a conservative resolution (the legal default for a gift is in Treas. Reg. §1.1015-1(a)(3))."""

    lot: str


@dataclass(frozen=True)
class OpeningMismatch:
    """Blocking: an opening allocation of more sats than its account held on 2025-01-01, not applied."""

    event: str
    held: int
    allocated: int


@dataclass(frozen=True)
class LateOpening:
    """A warning (T-508): an opening allocation recorded after its deadline, the user's first sale,
    disposition or transfer from 2025-01-01 (`first`, None if the due date came first) or the 2025
    return's due date (ADR 0041 §5). The allocation is used all the same."""

    event: str
    recorded_at: datetime
    deadline: datetime
    first: str | None


@dataclass(frozen=True)
class Result:
    allocations: tuple[Allocation, ...]
    gifts: tuple[GiftGiven, ...]
    holdings: tuple[Lot, ...]  # the lots left, in the order they were created or moved in, without empty ones
    late: tuple[LateIdentification, ...]
    blocking: tuple[MissingLots | UnknownBasis | Untraced | OpeningMismatch, ...]
    moves: tuple[Moved, ...] = ()
    created: tuple[Lot, ...] = ()  # lots created for a withdrawal from an account without lots (ADR 0009)
    replay_stopped: str | None = None  # the event the late choices' replay couldn't follow (#238)
    coins: tuple[CoinLot, ...] = ()  # the lots on each coin the coin-traced wallets hold (ADR 0041)
    draws: tuple[Drawn, ...] = ()  # each received coin's draws from its wallet's pool (ADR 0041 §4)
    placeholders: tuple[Lot, ...] = ()  # unknown-basis lots for received coins the pool fell short of
    pool: tuple[PoolLot, ...] = ()  # wallet lots on no coin at the end: probably a duplicate or a sale
    replaced: tuple[Lot, ...] = ()  # the lots the 2025 opening allocations replaced (ADR 0008 §6)
    opening_late: tuple[LateOpening, ...] = ()  # opening allocations recorded late (warnings)


@dataclass(frozen=True)
class CoinLot:
    outpoint: str
    lot: str
    sats: int


type UntracedReason = (
    tracing.BlockReason
    | Literal[
        "after_untraced",
        "uncovered",
        "mixed_transfer",
        "bad_link",
        "unrecorded_receipt",
        "no_tracking_start",
        "opening_allocation",
    ]
)


@dataclass(frozen=True)
class Drawn:
    """`sats` of `lot` from the pool of the wallet a received coin arrived in, onto that coin."""

    received: str
    outpoint: str
    lot: str
    sats: int


@dataclass(frozen=True)
class PoolLot:
    """A lot's sats on no coin at the end, in a coin-traced wallet (ADR 0041 §4)."""

    account: str
    lot: str
    sats: int


@dataclass(frozen=True)
class Untraced:
    """Blocking: a wallet transaction the engine can't trace (ADR 0041 §3), or one in a wallet whose
    coins no longer match its lots after such a transaction (`after_untraced`), or a link it can't
    apply (`bad_link`). Its leaving event is figured by the account's FIFO order meanwhile, so the
    figures are only provisional: reports block."""

    event: str
    reason: UntracedReason
    detail: str


@dataclass(frozen=True)
class _Late:
    """A late choice that differs from the standing method: the engine that judged it (the replay, or
    the real run once the replay has stopped) and the standing method's picks there."""

    engine: _Engine
    picks: tuple[Pick, ...]
    replayed: bool


@dataclass(frozen=True)
class _Fee:
    """Fee sats whose basis joins a moved part: `basis` (both its bases, or `loss` on its loss basis
    where given: a fee of its own lot), from `lots` (none: its own lot's)."""

    basis: Decimal
    sats: int
    lots: tuple[str, ...] = ()
    loss: Decimal | None = None


def _orphans_fee(orphans: Sequence[tuple[Decimal, int, str]]) -> _Fee:
    """All-fee parts' basis (each the lower of a gift's two), joined into one."""
    return _Fee(
        sum((b for b, _, _ in orphans), ZERO),
        sum(n for _, n, _ in orphans),
        tuple(dict.fromkeys(p for _, _, p in orphans)),
    )


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
    wallets: Mapping[str, Wallet] | None = None,
) -> Result:
    """Apply `events` in order. `methods` holds each account's standing method (FIFO if absent);
    `wallets` the coin-traced self-custody accounts (ADR 0041), whose lots follow their coins."""
    wallets = {} if wallets is None else wallets
    if not isinstance(wallets, Mapping):
        raise EngineError("wallets maps account names to Wallets")
    for name, wallet in wallets.items():
        if not isinstance(name, str) or not name or not isinstance(wallet, Wallet):
            raise EngineError("wallets maps account names to Wallets")
        if wallet.tracking_start is not None:
            _check_moment(wallet.tracking_start, f"wallet {name!r}: its tracking start")
    with localcontext(CONTEXT):
        return _Engine(methods or {}, dict(wallets)).run(events)


class _Engine:
    def __init__(
        self,
        methods: Mapping[str, Method],
        wallets: Mapping[str, Wallet] | None = None,
        *,
        replay: bool = False,
    ) -> None:
        wallets = {} if wallets is None else wallets
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
        self._blocking: list[MissingLots | UnknownBasis | Untraced | OpeningMismatch] = []
        self._moves: list[Moved] = []
        self._created: list[Lot] = []
        self._recorded: set[str] = set()  # accounts holding lots the user recorded (not created lots)
        self._openings: dict[str, OpeningAllocation] = {}  # each account's 2025 opening allocation
        self._replaced: list[Lot] = []
        self._keys: dict[str, list[date]] = {}  # account -> its lots' FIFO keys, in step with _open
        # The replay (#238): the same events with every late choice disregarded. Only the real run has
        # one; the replay itself is `replay=True` and records no warnings.
        self._replay = replay
        self._shadow: _Engine | None = None if replay else _Engine(methods, wallets, replay=True)
        # Coin tracing (ADR 0041): each coin-traced wallet's coins, outpoint -> its lots' sats on it, in
        # the order they arrived. A lot's sats on coins never exceed the sats it holds.
        self._wallets = frozenset(wallets)
        self._tracking = {w: wallet.tracking_start for w, wallet in wallets.items()}
        self._received: set[str] = set()  # the received coins' outpoints
        self._draws: list[Drawn] = []
        self._placeholders: list[Lot] = []
        self._coins: dict[str, dict[str, dict[str, int]]] = {w: {} for w in wallets}
        self._untraced: set[str] = set()  # wallets whose coins stopped matching their lots
        self._values: dict[str, int] = {}  # each linked output's value, as its links gave it
        self._spent: set[str] = set()  # the outpoints wallet transactions spent
        self._replay_stopped: str | None = None
        self._arriving: dict[str, dict[str, int]] = {}  # a transfer whose picks named only its arriving sats
        # such a transfer's whole identification (the fee's lots the standing method added, and the
        # arriving sats), recorded by the real run and handed to the replay to follow (ADR 0040, 0043)
        self._identified: dict[str, tuple[tuple[Pick, ...], dict[str, int]]] = {}
        self._given: dict[str, tuple[tuple[Pick, ...], dict[str, int]]] = {}

    def run(self, events: Sequence[Event]) -> Result:
        for wallet in sorted(w for w, start in self._tracking.items() if start is None):  # ADR 0041 §3
            self._blocking.append(Untraced(wallet, "no_tracking_start", f"{wallet!r} has no tracking start"))
        last: date | None = None
        last_moment: datetime | None = None
        in_2025 = False  # an event other than an opening allocation dated 2025-01-01 or later came
        first: tuple[str, datetime] | None = None  # the first sale, disposition or transfer from 2025
        for event in events:
            _check_common(event)
            if event.id in self._seen:
                raise EngineError(f"event {event.id!r} appears twice")
            self._seen.add(event.id)
            if last is not None and event.on < last:
                raise EngineError(f"event {event.id!r} is out of date order")
            last = event.on
            if isinstance(event, OpeningAllocation) and in_2025:
                raise EngineError(f"opening allocation {event.id!r} comes before every other 2025 event")
            in_2025 = in_2025 or (not isinstance(event, OpeningAllocation) and event.on >= OPENING_DAY)
            if not isinstance(event, Acquisition | OpeningAllocation):
                last_moment = _checked_moment(event, last_moment)
                leaving = event.leaving if isinstance(event, WalletTx) else event
                disposes = not isinstance(event, Received) and leaving is not None
                if first is None and disposes and event.on >= OPENING_DAY:
                    first = (event.id, event.at)
            self._apply(event)
            self._follow(event)
        holdings = tuple(_left(o) for o in self._lots.values() if o.sats > 0)
        early = first is not None and first[1] < RETURN_DUE_2025
        deadline, named = (first[1], first[0]) if first is not None and early else (RETURN_DUE_2025, None)
        opening_late = [
            LateOpening(o.id, o.recorded_at, deadline, named)
            for o in self._openings.values()
            if o.recorded_at > deadline
        ]
        return Result(
            tuple(self._allocations),
            tuple(self._gifts),
            holdings,
            tuple(self._late),
            tuple(self._blocking),
            tuple(self._moves),
            tuple(self._created),
            self._replay_stopped,
            tuple(
                CoinLot(outpoint, lot, sats)
                for wallet in sorted(self._coins)
                for outpoint, lots in self._coins[wallet].items()
                for lot, sats in lots.items()
            ),
            tuple(self._draws),
            tuple(self._placeholders),
            tuple(  # an untraced wallet's lots aren't placed: none is listed
                PoolLot(wallet, lot, sats)
                for wallet in sorted(self._wallets - self._untraced)
                for lot, sats in self._pool(wallet)
            ),
            tuple(self._replaced),
            tuple(opening_late),
        )

    def _follow(self, event: Event) -> None:
        """Apply `event` to the replay too, after the real run accepted it. An event the replay can't
        apply, or one after which the two runs hold different sats in an account it touched (only one
        created a lot for unrecorded sats, say), stops the replay there. Every event is checked, so
        while the replay runs it holds the same sats in every account as the real run."""
        shadow = self._shadow
        identified = self._identified.pop(event.id, None)
        if shadow is None:
            return
        if identified is not None:
            shadow._given[event.id] = identified
        try:
            shadow._apply(event)
        except EngineError:
            self._shadow, self._replay_stopped = None, event.id
            return
        if shadow._untraced != self._untraced:  # the replay traced a wallet the real run didn't, or back
            self._shadow, self._replay_stopped = None, event.id
            return
        accounts = {event.account}
        leaving = (
            event.leaving
            if isinstance(event, WalletTx)
            else None
            if isinstance(event, Received | OpeningAllocation)
            else event
        )
        if isinstance(leaving, Transfer):
            accounts.add(leaving.to)
        if any(shadow._held(a) != self._held(a) for a in accounts):
            self._shadow, self._replay_stopped = None, event.id

    def _apply(self, event: Event) -> None:
        if isinstance(event, Acquisition):
            self._acquire(event)
        elif isinstance(event, OpeningAllocation):
            self._opening(event)
        elif isinstance(event, WalletTx):
            self._wallet_tx(event)
        elif isinstance(event, Received):
            self._receive(event)
        elif isinstance(event, Transfer):
            self._transfer(event)
        else:
            self._dispose(event)

    def _held(self, account: str) -> int:
        return sum(o.sats for o in self._open.get(account, []))

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
        self._link(a.id, a.account, [(a.id, a.sats)], a.coins, a.sats)

    def _opening(self, e: OpeningAllocation) -> None:
        """The 2025 opening allocation (ADR 0008 §6): the account's lots replaced by the allocated ones;
        a shortfall is a placeholder of unknown basis, a surplus blocks and changes nothing."""
        bases = _checked_opening(e)
        if e.account in self._openings:
            raise EngineError(f"opening allocation {e.id!r}: {e.account!r} has one already")
        self._openings[e.account] = e
        if e.account in self._wallets:
            detail = "a coin-traced wallet's opening allocation isn't supported yet (ADR 0041 §5)"
            self._blocking.append(Untraced(e.id, "opening_allocation", detail))
            return
        held = self._held(e.account)
        if e.sats > held:
            self._blocking.append(OpeningMismatch(e.id, held, e.sats))
            return
        for o in self._open.get(e.account, []):
            if o.sats > 0:
                self._replaced.append(_left(o))
                o.sats, o.basis, o.loss_basis = 0, ZERO, None
        for i, (lot, basis) in enumerate(zip(e.lots, bases, strict=True), start=1):
            new = Lot(f"{e.id}@{i}", e.account, lot.acquired, lot.sats, basis, lot.always_long)
            self._insert(e.account, _Open(new, lot.sats, basis, None, False, lot.acquired))
        if held > e.sats:
            lot_id = f"{e.id}{PLACEHOLDER}"
            placeholder = Lot(lot_id, e.account, e.on, held - e.sats, ZERO, False, None, None, True)
            self._insert(e.account, _Open(placeholder, held - e.sats, ZERO, None, False, e.on, created=True))
            self._placeholders.append(placeholder)
            self._blocking.append(UnknownBasis(lot_id))
        self._recorded.add(e.account)

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
            given = self._given.pop(e.id, None)  # the real run's whole identification, fee lots included
            if e.identified_at is not None and e.identified_at <= e.at:
                try:
                    if given is None:
                        return self._checked_picks(e, e.picks), 0, None
                    picks = self._checked_picks(e, given[0])
                    self._arriving[e.id] = dict(given[1])
                    return picks, 0, None
                except EngineError:
                    pass
            picks, missing = self._standing(e.account, e.sats)
            return picks, missing, None
        if e.identified_at is None:
            raise EngineError(f"event {e.id!r}: a chosen set of lots needs the time it was chosen")
        _check_moment(e.identified_at, f"event {e.id!r}: its identification time")
        picks = self._checked_picks(e, e.picks)
        if e.identified_at > e.at:
            judge = self._shadow if self._shadow is not None else self  # these lots once it has stopped
            # the same sats as the real run (_follow checks it), so never short: valid picks cover them
            standing, _ = judge._standing(e.account, e.sats)
            # the same lots means the same coins: a lot id names a purchase and the transfers that
            # moved it (lot@transfer), so equal ids and sats are the same coins in both runs, even when
            # their basis differs by the rounding an earlier late choice left behind, or by how much of
            # the source moved with them. That difference is the earlier choice's, already warned
            # about; the warning shows the replay's own figures (ADR 0040, its "Lot ids" bullet)
            if _merged(picks) != _merged(standing) or self._arrive_otherwise(e, judge, standing):
                return picks, 0, _Late(judge, tuple(standing), judge is not self)
        return picks, 0, None

    def _arrive_otherwise(self, e: Disposal | Transfer, judge: _Engine, standing: list[Pick]) -> bool:
        """Whether a choice naming a transfer's arriving sats has other sats arrive than the standing
        method would, with the same lots in all: the fee's lots are part of the identification, so the
        late check judges the whole (ADR 0043)."""
        named = self._arriving.get(e.id)
        if named is None or not isinstance(e, Transfer):
            return False
        fifo = _fifo_arrivals(e, judge._in_fifo_order(e.account, standing))
        return {lot: n for lot, n in named.items() if n} != {p.lot: n for p, n in fifo if n}

    def _dispose(self, d: Disposal, *, untraced: bool = False) -> None:
        proceeds = _checked_disposal(d)
        if d.account in self._wallets and not untraced:
            raise EngineError(f"disposal {d.id!r}: a coin-traced wallet's spends come as its WalletTx")
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

    def _transfer(self, t: Transfer, *, untraced: bool = False) -> None:
        self._check_plain_transfer(t, untraced=untraced)
        if t.kind == "self_transfer" and not untraced and self._mixed(t):
            return
        self._unrecorded_if_needed(t)
        picks, missing, late = self._choose(t)
        if late is not None and t.identified_at is not None:
            alternative = late.engine._alternative(t, late.picks)
            self._late.append(LateIdentification(t.id, t.identified_at, alternative, late.replayed))
        if missing:  # a shortfall is blocking; what is covered arrives first, the fee after it
            self._blocking.append(MissingLots(t.id, missing, ZERO))
        if not picks:
            return
        linked = bool(t.coins) and t.to in self._wallets  # the fee's basis then goes onto a coin
        moved: list[Moved] = []
        orphans: list[tuple[Decimal, int, str]] = []
        own: list[tuple[Moved, Decimal, Decimal | None, int]] = []  # fee sats of a lot that arrives
        for pick, arrive in self._arrivals(t, picks):
            if arrive and linked and pick.sats > arrive:  # moved apart: the fee's basis follows a coin
                moved.append(self._move(t, pick.lot, arrive, arrive=arrive))
                basis, loss = self._take(pick.lot, pick.sats - arrive)
                own.append((moved[-1], basis, loss, pick.sats - arrive))
            elif arrive:  # the whole part leaves its lot; the fee's basis arrives with the sats that do
                moved.append(self._move(t, pick.lot, pick.sats, arrive=arrive))
            else:  # all fee: its basis (the lower of a gift's two) passes to the oldest moved part
                basis, loss = self._take(pick.lot, pick.sats)
                orphans.append((_lower(basis, loss), pick.sats, pick.lot))
        arrived = sorted(moved, key=lambda m: self._oldest_key(m.into))  # oldest first (ADR 0041 §1)
        delivered = t.sats - t.fee_sats
        if t.coins and self._link(t.id, t.to, [(m.into, m.sats) for m in arrived], t.coins, delivered):
            self._fee_onto_coins(t, moved, own, orphans)  # ADR 0043: linked, the fee follows a coin
            return
        for m, basis, loss, n in own:  # links refused: the fee's basis stays with the moved part
            self._carry(m, _Fee(basis, n, (), loss))
        if orphans:  # a moved part always exists: the fee leaves a sat to arrive
            self._carry(moved[0], _orphans_fee(orphans))  # the oldest moved part: first in FIFO order

    def _fee_onto_coins(
        self,
        t: Transfer,
        moved: list[Moved],
        own: list[tuple[Moved, Decimal, Decimal | None, int]],
        orphans: list[tuple[Decimal, int, str]],
    ) -> None:
        """A linked withdrawal's fee basis (ADR 0043): to the first linked output, in the order given (the
        canonical order), that holds a fragment of the same lot, else to the first output's oldest
        fragment. A fragment whose lot is elsewhere too (another coin, or the pool) becomes a lot of its
        own first, so the basis stays on that coin."""
        held = self._coins[t.to]
        order = [c.outpoint for c in t.coins]
        base: dict[str, Moved] = {m.into: m for m in moved}  # a coin's lot -> the moved part it is from

        def fragment(outpoint: str, lot: str) -> str:
            coin = held[outpoint]
            if self._lots[lot].sats == coin[lot]:  # all of it is on this coin
                return lot
            new = f"{lot}{SPLIT}{outpoint.rsplit(':', 1)[1]}"  # by its vout
            if new not in self._lots:
                self._split_off(t.to, lot, coin.pop(lot), new)
                coin[new] = self._lots[new].sats
                base[new] = base[lot]
            return new

        def oldest() -> str:  # this withdrawal's oldest fragment there, never another event's lot
            first = held[order[0]]
            return fragment(order[0], min((lot for lot in first if lot in base), key=self._oldest_key))

        for m, basis, loss, n in own:
            on = next((op for op in order if m.into in held.get(op, {})), None)
            if on is None:  # none of the lot is linked: the first output's oldest fragment, lower basis
                lot = oldest()
                self._carry(base[lot], _Fee(_lower(basis, loss), n, (m.lot,)), lot)
            else:
                lot = fragment(on, m.into)
                self._carry(base[lot], _Fee(basis, n, (), loss), lot)
        if orphans:
            lot = oldest()
            self._carry(base[lot], _orphans_fee(orphans), lot)

    # Coin-traced wallets (ADR 0041 §1-§3)

    def _link(
        self, event: str, account: str, lots: list[tuple[str, int]], coins: tuple[Coin, ...], delivered: int
    ) -> bool:
        """Put an arrival's lots (oldest first) onto the outputs it names, filling them in the order given
        (the caller's canonical order); whether it did. Links over the sats that arrived, or over an
        output's value, block, and are left out."""
        if not isinstance(coins, tuple):
            raise EngineError(f"event {event!r}: its coins are a tuple of Coins")
        if not coins:
            return False
        if account not in self._wallets:
            raise EngineError(f"event {event!r}: only a coin-traced wallet's outputs are linked")
        self._check_links(event, coins)
        if account in self._untraced:  # its coin records were dropped: they no longer match its lots
            return False
        linked = sum(c.sats for c in coins)
        if linked > delivered:
            detail = f"its links hold {linked} sats, more than the {delivered} that arrived"
            self._blocking.append(Untraced(event, "bad_link", detail))
            return False
        spent = [c.outpoint for c in coins if c.outpoint in self._spent]
        if spent:
            self._blocking.append(Untraced(event, "bad_link", f"{', '.join(spent)} was spent already"))
            return False
        held_now = self._coins[account]
        for c in coins:
            total = sum(held_now.get(c.outpoint, {}).values()) + c.sats
            if total > c.value:  # with the links before it: more lots than the output holds (ADR 0041 §1)
                detail = f"{c.outpoint} would hold {total} sats of lots, more than its {c.value}"
                self._blocking.append(Untraced(event, "bad_link", detail))
                return False
        queue: list[list[str | int]] = [[lot, n] for lot, n in lots]
        held = self._coins[account]
        for c in coins:
            need = c.sats
            on = held.setdefault(c.outpoint, {})
            while need and queue:  # a shortfall (missing lots, blocking) leaves the rest of it unlinked
                lot, n = str(queue[0][0]), int(queue[0][1])
                take = min(n, need)
                on[lot] = on.get(lot, 0) + take
                need -= take
                queue[0][1] = n - take
                if n == take:
                    queue.pop(0)
        return True

    def _receive(self, r: Received) -> None:
        """A coin from outside: the part no link covers draws from the pool, oldest first, if it arrived
        before the wallet's tracking start; a pool short of it leaves a placeholder of unknown basis
        (blocking). After the tracking start, or with none set, that part blocks (ADR 0041 §3, §4)."""
        if r.account not in self._wallets:
            raise EngineError(f"received coin {r.id!r}: {r.account!r} isn't a coin-traced wallet")
        if not isinstance(r.outpoint, str) or ":" not in r.outpoint:
            raise EngineError(f"received coin {r.id!r}: it names its outpoint (txid:vout)")
        if r.outpoint in self._received:
            raise EngineError(f"received coin {r.id!r}: {r.outpoint} was received already")
        self._received.add(r.outpoint)
        if self._values.setdefault(r.outpoint, r.value) != r.value:
            raise EngineError(f"received coin {r.id!r}: its links gave {r.outpoint} another value")
        if r.outpoint in self._spent:  # it came after the transaction that spent it: the caller's order
            raise EngineError(f"received coin {r.id!r}: {r.outpoint} was spent already")
        if any(r.outpoint in self._coins[w] for w in self._coins if w != r.account):
            raise EngineError(f"received coin {r.id!r}: {r.outpoint} is another wallet's coin")
        if r.account in self._untraced:  # its coin records were dropped, and it blocks already
            return
        on = self._coins[r.account].setdefault(r.outpoint, {})
        need = r.value - sum(on.values())
        if need <= 0:  # covered by links (links over its value blocked when they were made)
            return
        start = self._tracking[r.account]
        if start is None:
            self._blocking.append(Untraced(r.id, "no_tracking_start", f"{r.account!r} has no tracking start"))
            return
        if r.at >= start:
            detail = f"{need} sats of {r.outpoint} no link covers, received after the tracking start"
            self._blocking.append(Untraced(r.id, "unrecorded_receipt", detail))
            return
        for lot, free in self._pool(r.account):  # only lots already here: events come in time order
            take = min(free, need)
            on[lot] = on.get(lot, 0) + take
            self._draws.append(Drawn(r.id, r.outpoint, lot, take))
            need -= take
            if not need:
                return
        lot_id = f"{r.id}{PLACEHOLDER}"
        placeholder = Lot(lot_id, r.account, r.on, need, ZERO, False, None, None, True)
        self._insert(r.account, _Open(placeholder, need, ZERO, None, False, r.on, created=True))
        self._placeholders.append(placeholder)
        self._blocking.append(UnknownBasis(lot_id))
        on[lot_id] = need

    def _pool(self, account: str) -> list[tuple[str, int]]:
        """The wallet's lots on no coin, oldest first (by the date each reached the user, then its event
        id, ADR 0041 §2.1), with their sats on no coin."""
        on_coins: dict[str, int] = {}
        for lots in self._coins[account].values():
            for lot, n in lots.items():
                on_coins[lot] = on_coins.get(lot, 0) + n
        free = [
            (o.lot.id, o.sats - on_coins.get(o.lot.id, 0))
            for o in self._open.get(account, [])
            if o.sats > on_coins.get(o.lot.id, 0)
        ]
        return sorted(free, key=lambda entry: self._oldest_key(entry[0]))

    def _check_links(self, event: str, coins: tuple[Coin, ...]) -> None:
        for c in coins:
            if not isinstance(c, Coin) or not isinstance(c.outpoint, str) or ":" not in c.outpoint:
                raise EngineError(f"event {event!r}: a link is a Coin naming an outpoint (txid:vout)")
            _check_sats(c.sats, f"event {event!r}")
            _check_sats(c.value, f"event {event!r}")
            if self._values.setdefault(c.outpoint, c.value) != c.value:
                raise EngineError(f"event {event!r}: {c.outpoint} has another value in an earlier link")
        if len({c.outpoint for c in coins}) != len(coins):
            raise EngineError(f"event {event!r}: an output is linked once per event")
        if len({c.outpoint.rsplit(":", 1)[0] for c in coins}) != 1:  # one arrival, one transaction (§1)
            raise EngineError(f"event {event!r}: its links name the outputs of one transaction")

    def _wallet_tx(self, c: WalletTx) -> None:
        """A transaction from a coin-traced wallet: its coins' lots go to its outputs by ADR 0041 §2
        (`tracing.trace`). A transaction it can't trace blocks (§3); its leaving event is then figured by
        the account's FIFO order, provisionally, and the wallet's later transactions block too."""
        leaving = _checked_wallet_tx(c, self._wallets)
        for e in (() if leaving is None else (leaving,)) + c.also:
            if e.id in self._seen:
                raise EngineError(f"event {e.id!r} appears twice")
            self._seen.add(e.id)
        if c.also:
            ids = ", ".join(repr(e.id) for e in (leaving, *c.also) if e is not None)
            self._blocking.append(Untraced(c.id, "several_leaving_events", f"leaving events {ids}"))
            self._untrace(c, leaving)
            return
        for i in c.inputs:  # a coin's value is the chain's: the same in its links and when it is spent
            if self._values.setdefault(i.outpoint, i.value) != i.value:
                raise EngineError(f"wallet tx {c.id!r}: {i.outpoint} has another value in its links")
        reason = self._untraceable(c, leaving)
        if reason is not None:
            self._blocking.append(Untraced(c.id, *reason))
            self._untrace(c, leaving)
            return
        inputs = tuple(
            tracing.Input(
                i.account,
                i.value,
                tuple(
                    tracing.Fragment(
                        lot, n, datetime.combine(self._lots[lot].fifo, time(), UTC), lot.split("@")[0]
                    )
                    for lot, n in self._coins[c.account].get(i.outpoint, {}).items()
                )
                if i.account == c.account
                else (),
                vin,
            )
            for vin, i in enumerate(c.inputs)
        )
        traced = tracing.trace(tracing.WalletTx(c.txid, c.account, inputs, c.outputs, _leaving(leaving)))
        if isinstance(traced, tracing.Blocked):
            self._blocking.append(Untraced(c.id, traced.reason, traced.detail))
            self._untrace(c, leaving)
            return
        self._apply_traced(c, leaving, traced)

    def _untraceable(
        self, c: WalletTx, leaving: Disposal | Transfer | None
    ) -> tuple[UntracedReason, str] | None:
        """Why the engine can't trace `c` before tracing it, or None."""
        if c.account in self._untraced:
            return "after_untraced", f"an earlier transaction of {c.account!r} couldn't be traced"
        held = self._coins[c.account]
        for vin, i in enumerate(c.inputs):
            if i.account is not None and i.account != c.account:
                return "several_accounts", f"input {vin} is from account {i.account!r}"
            if i.account == c.account:
                on = sum(held.get(i.outpoint, {}).values())  # never above its value: links can't be
                if on < i.value:
                    return "uncovered", f"input {vin} holds {i.value} sats, its links {on}"
        if (
            isinstance(leaving, Transfer)
            and leaving.kind == "self_transfer"
            and leaving.to not in self._wallets
        ):
            return "mixed_transfer", f"{leaving.to!r} isn't coin-traced"
        return None

    def _untrace(self, c: WalletTx, leaving: Disposal | Transfer | None) -> None:
        """The provisional figures for an untraceable transaction: its coins leave the wallet's records,
        and its leaving event takes lots by the account's FIFO order. The wallet's coins no longer match
        its lots, so its later transactions block too."""
        # every wallet the transaction spends from: their coin records no longer match their lots, so
        # they are dropped, and the wallets' later transactions block too
        to = leaving.to if isinstance(leaving, Transfer) and leaving.to in self._wallets else None
        self._spent.update(i.outpoint for i in c.inputs)
        for account in {c.account, to} | {i.account for i in c.inputs if i.account in self._wallets}:
            if account is not None:
                self._untraced.add(account)
                self._coins[account].clear()
        owned = sum(i.value for i in c.inputs if i.account == c.account)
        kept = sum(o.value for o in c.outputs if o.account == c.account)
        out = owned - kept - (0 if leaving is None else leaving.sats)  # what else left the lots
        fee = (
            0 if isinstance(leaving, Transfer) else max(0, min(c.sats - sum(o.value for o in c.outputs), out))
        )
        if c.also:
            fee = 0  # several leaving events: each by FIFO in turn, the fee leaving with what remains
        if isinstance(leaving, Transfer):  # its fee_sats is the fee: the transfer carries it
            self._transfer(leaving, untraced=True)
        elif isinstance(leaving, Disposal):
            self._fifo_disposal(leaving, fee)
        elif fee:
            self._fifo_consolidation(c.account, fee)
        for extra in c.also:
            if isinstance(extra, Transfer):
                self._transfer(extra, untraced=True)
            else:
                self._dispose(extra, untraced=True)
            out -= extra.sats
        if out - fee > 0:  # outputs to no one recorded: their sats leave too, by FIFO
            picks, _ = self._standing(c.account, out - fee)
            for pick in picks:
                self._take(pick.lot, pick.sats)

    def _fifo_disposal(self, d: Disposal, fee: int) -> None:
        """An untraceable sale, spend or gift by the account's FIFO order: its sats, then the fee's,
        whose basis follows ADR 0043 as in a traced one (a whole-wallet FIFO wallet's rule)."""
        picks, missing = self._standing(d.account, d.sats + fee)
        if missing > fee:  # short of lots for the disposal itself: figured as a disposal is
            self._dispose(d, untraced=True)
            return
        leave: dict[str, int] = {}
        fees: dict[str, int] = {}
        left = d.sats
        for pick in picks:
            n = min(pick.sats, left)
            if n:
                leave[pick.lot] = leave.get(pick.lot, 0) + n
            if pick.sats - n:
                fees[pick.lot] = fees.get(pick.lot, 0) + pick.sats - n
            left -= n
        self._traced_disposal(d, leave, fees)

    def _fifo_consolidation(self, account: str, fee: int) -> None:
        """An untraceable consolidation's fee by FIFO: its basis stays with the same lot where any of it
        remains, else joins the wallet's oldest remaining lot (ADR 0043's whole-wallet FIFO rule)."""
        picks, _ = self._standing(account, fee)
        for pick in picks:
            o = self._lots[pick.lot]
            if o.sats > pick.sats:
                o.sats -= pick.sats
                continue
            basis, loss = self._take(pick.lot, pick.sats)
            rest = next((r for r in self._open.get(account, []) if r.sats), None)
            if rest is not None:
                rest.basis = add(rest.basis, _lower(basis, loss))
                if rest.loss_basis is not None:
                    rest.loss_basis = add(rest.loss_basis, _lower(basis, loss))

    def _apply_traced(self, c: WalletTx, leaving: Disposal | Transfer | None, traced: tracing.Traced) -> None:
        self._spent.update(i.outpoint for i in c.inputs)
        held = self._coins[c.account]
        for i in c.inputs:
            held.pop(i.outpoint, None)
        by_vout = dict(traced.outputs)
        out_ids = {o.vout: o for o in c.outputs}
        canonical = sorted(by_vout, key=lambda v: (-out_ids[v].value, out_ids[v].script, v))  # §2.3
        paid = [v for v in canonical if leaving is not None and out_ids[v].event == leaving.id]
        leave = _summed(f for v in paid for f in by_vout[v])
        fee = _summed(traced.fee)
        for o in c.outputs:  # the new coins' values, for the links and spends that name them
            self._values[f"{c.txid}:{o.vout}"] = o.value
        for v in sorted(by_vout):  # the change stays on the wallet's new coins, under the same lots
            if out_ids[v].account == c.account and v not in paid:
                held[f"{c.txid}:{v}"] = _summed(by_vout[v])
        if isinstance(leaving, Disposal):
            self._traced_disposal(leaving, leave, fee)
        elif isinstance(leaving, Transfer):
            parts = {v: _summed(by_vout[v]) for v in paid}
            moved = self._traced_transfer(leaving, parts, traced.carries)
            if leaving.to in self._wallets and leaving.to not in self._untraced:  # its new coins hold them
                for v, lots in parts.items():
                    self._coins[leaving.to][f"{c.txid}:{v}"] = {
                        moved[lot, v].into: n for lot, n in lots.items()
                    }
        else:  # a consolidation: the fee's basis stays in the wallet, on the change (ADR 0043)
            self._consolidated(c, traced.carries)

    def _consolidated(self, c: WalletTx, carries: tuple[tracing.FeeCarry, ...]) -> None:
        """A consolidation's fee basis joins the fragment tracing names on the change. Where that lot is
        anywhere else too (another coin, or the pool), the named fragment becomes a lot of its own
        (`lot@tx#vout`), so the basis stays on that coin (ADR 0041 §2.4). In ADR 0041 §2.2's order: the
        fee's sats take their basis first, then the change fragments split off, then the fee's basis
        joins them."""
        held = self._coins[c.account]
        pending: dict[str, int] = {}  # each lot's fee sats
        for k in carries:
            pending[k.lot] = pending.get(k.lot, 0) + k.sats
        # which named fragments split off: their lot is somewhere else too, the fee's sats aside
        splits = {
            (k.onto, k.vout): self._lots[k.onto].sats - pending.get(k.onto, 0)
            > held[f"{c.txid}:{k.vout}"][k.onto]
            for k in carries
        }
        taken: list[tuple[tracing.FeeCarry, Decimal, Decimal | None]] = []
        for k in carries:  # the fee first
            if k.lot == k.onto and not splits[k.onto, k.vout]:  # its own fragment: the lot keeps the basis
                self._lots[k.lot].sats -= k.sats
                continue
            basis, loss = self._take(k.lot, k.sats)
            taken.append((k, basis, loss))
        lots: dict[tuple[str, int], str] = {}
        for (onto, vout), split in splits.items():  # then the change fragments
            if split:
                coin = held[f"{c.txid}:{vout}"]
                new = f"{onto}@{c.id}{SPLIT}{vout}"
                self._split_off(c.account, onto, coin.pop(onto), new)
                coin[new] = self._lots[new].sats
                lots[onto, vout] = new
        for k, basis, loss in taken:  # then the fee's basis onto them
            entry = self._lots[lots.get((k.onto, k.vout), k.onto)]
            same = k.lot == k.onto  # a part of the fee's own lot: both bases move together
            entry.basis = add(entry.basis, basis if same else _lower(basis, loss))
            if entry.loss_basis is not None:
                entry.loss_basis = add(
                    entry.loss_basis, loss if same and loss is not None else _lower(basis, loss)
                )

    def _split_off(self, account: str, lot_id: str, sats: int, new: str) -> None:
        """`sats` of a lot become a lot of their own in the same account, with its FIFO date (placed after
        the account's other lots of that date), with their shares of its bases."""
        o = self._lots[lot_id]
        basis, loss = self._take(lot_id, sats)
        lot = replace(o.lot, id=new, sats=sats, basis=basis, loss_basis=loss)
        self._insert(account, _Open(lot, sats, basis, loss, o.gift, o.fifo, created=o.created))

    def _traced_disposal(self, d: Disposal, leave: dict[str, int], fee: dict[str, int]) -> None:
        """A sale's or spend's lots are its outputs' and the fee's (their basis is in it, ADR 0043); a
        gift's are its outputs', and the fee's basis isn't recovered."""
        proceeds = _checked_disposal(d)
        if d.kind == "gift_out":
            picks = self._in_fifo_order(d.account, [Pick(lot, n) for lot, n in leave.items()])
            allocations, _ = self._split(d, proceeds, picks, apply=True)
            self._gifts.extend(self._gift(a) for a in allocations)
            for lot, n in fee.items():
                self._take(lot, n)
            return
        # The rows are the outputs' lots, with the proceeds what was received; then the fee's sats leave,
        # their bases set aside. Each fee lot's basis then joins the row for the same lot (both of a
        # gift's bases), else the oldest row, which keeps its own date (the lower basis, on both of the
        # row's bases where it has two): no fee has a row of its own (ADR 0043)
        picks = self._in_fifo_order(d.account, [Pick(lot, n) for lot, n in leave.items()])
        rows, _ = self._split(d, proceeds, picks, apply=True)  # the outputs' sats first, then the fee's
        set_aside = {lot: self._take(lot, n) for lot, n in fee.items()}
        for lot, (basis, loss) in set_aside.items():
            both: tuple[Decimal, Decimal | None]
            i = next((k for k, row in enumerate(rows) if row.lot == lot), None)
            if i is None:  # the oldest row: by when its lot reached the user, then event id (ADR 0043)
                i = min(range(len(rows)), key=lambda k: self._oldest_key(rows[k].lot))
                both = (_lower(basis, loss), None)
            else:
                both = (basis, loss)
            rows[i] = self._with_fee_basis(d, rows[i], both, fee[lot], lot)
        self._allocations.extend(rows)

    def _oldest_key(self, lot: str) -> tuple[date, str, str]:
        """The order lots leave a coin in (tracing's): the date the lot reached the user, its event id,
        its id."""
        return (self._lots[lot].fifo, lot.split("@", maxsplit=1)[0], lot)

    def _with_fee_basis(
        self, d: Disposal, row: Allocation, added: tuple[Decimal, Decimal | None], sats: int, lot: str
    ) -> Allocation:
        """`row` refigured with a fee lot's basis added: `added` is (gain basis, loss basis or None for
        the same amount on both)."""
        taken = row.basis if row.lot_basis is None else row.lot_basis
        gain_add, loss_add = added
        loss = row.lot_loss_basis
        if loss is not None:
            loss = add(loss, gain_add if loss_add is None else loss_add)
        refigured = self._allocation(
            d, self._lots[row.lot].lot, (row.sats, add(taken, gain_add), loss), row.proceeds
        )
        return replace(refigured, carried_fee=row.carried_fee + sats, carried_from=(*row.carried_from, lot))

    def _traced_transfer(
        self, t: Transfer, parts: dict[int, dict[str, int]], carries: tuple[tracing.FeeCarry, ...]
    ) -> dict[tuple[str, int], Moved]:
        """Move each leaving output's lots to `t.to`: one moved lot per lot, or, for a lot on several
        leaving outputs, one per output (`lot@transfer#vout`), so that a fee's basis stays on the output
        tracing names. The outputs' sats move first, then the fee's: a moved part takes the basis of its
        lot's fee sats tracing puts on it (both of a gift's bases); a lot all
        of whose sats here went to the fee passes its basis to the moved part tracing names (ADR 0041
        §2.4)."""
        own = {(k.lot, k.vout): k.sats for k in carries if k.onto == k.lot}
        spans: dict[str, list[int]] = {}
        for v, lots in parts.items():
            for lot in lots:
                spans.setdefault(lot, []).append(v)
        moved: dict[tuple[str, int], Moved] = {}
        for pick in self._in_fifo_order(t.account, [Pick(lot, 0) for lot in spans]):
            for v in spans[pick.lot]:
                n = parts[v][pick.lot]
                into = f"{pick.lot}@{t.id}" if len(spans[pick.lot]) == 1 else f"{pick.lot}@{t.id}{SPLIT}{v}"
                moved[pick.lot, v] = self._move(t, pick.lot, n, arrive=n, into=into)
        for (lot, v), n in own.items():  # then the fee's sats (ADR 0041 §2.2's order), their basis on v
            basis, loss = self._take(lot, n)
            self._carry(moved[lot, v], _Fee(basis, n, (), loss))
        onto: dict[tuple[str, int], list[tuple[Decimal, int, str]]] = {}
        for k in carries:
            if k.onto != k.lot:
                basis, loss = self._take(k.lot, k.sats)
                onto.setdefault((k.onto, k.vout), []).append((_lower(basis, loss), k.sats, k.lot))
        for key, parts_ in onto.items():
            self._carry(moved[key], _orphans_fee(parts_))
        return moved

    def _mixed(self, t: Transfer) -> bool:
        """A self-transfer from an account that isn't coin-traced: its lots follow the coins it spends
        (ADR 0041), so it blocks, figured by FIFO provisionally; whether it was figured here."""
        detail = f"a self-transfer from {t.account!r}, which isn't coin-traced"
        self._blocking.append(Untraced(t.id, "mixed_transfer", detail))
        if t.to in self._wallets:  # the coins it arrives in don't hold the lots it moves by FIFO
            self._untraced.add(t.to)
            self._coins[t.to].clear()
        if t.picks is None:
            return False
        self._transfer(replace(t, picks=None, identified_at=None), untraced=True)  # whatever it names
        return True

    def _check_plain_transfer(self, t: Transfer, *, untraced: bool) -> None:
        _checked_transfer(t)
        if t.account in self._wallets and not untraced:
            raise EngineError(f"transfer {t.id!r}: a coin-traced wallet's spends come as its WalletTx")
        if t.kind == "deposit" and t.to in self._wallets:
            raise EngineError(f"transfer {t.id!r}: a deposit goes to an exchange account, not a wallet")
        if t.coins and t.kind != "withdrawal":
            raise EngineError(f"transfer {t.id!r}: only a withdrawal links the outputs its lots arrive in")

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

    def _arrivals(self, t: Transfer, picks: Sequence[Pick]) -> list[tuple[Pick, int]]:
        """Each pick, in the account's FIFO order, with the sats of it that arrive. Where the user named
        only the arriving sats, those arrive and the fee is the rest; otherwise the arriving sats take the
        lots first in FIFO order and the fee the last sats (ADR 0043)."""
        ordered = self._in_fifo_order(t.account, picks)
        named = self._arriving.pop(t.id, None)
        if named is None:
            return _fifo_arrivals(t, ordered)
        return [(p, named.get(p.lot, 0)) for p in ordered]

    def _in_fifo_order(self, account: str, picks: Sequence[Pick]) -> list[Pick]:
        """`picks` in the account's FIFO order, so which sats arrive and where a fee's basis goes depend only
        on which lots it uses, not on the order they were named in."""
        place = {o.lot.id: k for k, o in enumerate(self._open.get(account, []))}
        return sorted(picks, key=lambda p: place[p.lot])

    def _alternative(self, t: Transfer, picks: Sequence[Pick]) -> tuple[Moved, ...]:
        """What `picks` would give, nothing used up: the parts that would arrive, with their bases, as the
        real run would move them (ADR 0008 §4, ADR 0043)."""
        out: list[Moved] = []
        orphans: list[tuple[Decimal, int, str]] = []
        for pick, arrive in _fifo_arrivals(t, self._in_fifo_order(t.account, picks)):
            o = self._lots[pick.lot]
            basis = share(o.basis, pick.sats, o.sats)
            if arrive:
                into = f"{pick.lot}@{t.id}"
                out.append(
                    Moved(t.id, pick.lot, into, t.to, arrive, basis, o.lot.acquired, pick.sats - arrive)
                )
            else:
                loss = None if o.loss_basis is None else share(o.loss_basis, pick.sats, o.sats)
                orphans.append((_lower(basis, loss), pick.sats, pick.lot))
        if orphans:
            first = out[0]
            out[0] = Moved(
                first.transfer,
                first.lot,
                first.into,
                first.to,
                first.sats,
                add(first.basis, sum((b for b, _, _ in orphans), ZERO)),
                first.acquired,
                first.carried_fee + sum(n for _, n, _ in orphans),
                tuple(lot for _, _, lot in orphans),
            )
        return tuple(out)

    def _move(
        self, t: Transfer, lot_id: str, sats: int, *, arrive: int | None = None, into: str | None = None
    ) -> Moved:
        """Move `sats` of a lot to `t.to` (`arrive` of them arrive: the rest was a carried fee), as lot
        `into` (by default `lot@transfer`)."""
        o = self._lots[lot_id]
        into = f"{lot_id}@{t.id}" if into is None else into
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

    def _carry(self, moved: Moved, fee: _Fee, onto: str | None = None) -> None:
        """Add fee sats' basis to a moved part (ADR 0043): to its lot, or to `onto`, the lot a fragment of
        it became on one coin. A gift's moved part takes it on both its bases."""
        entry = self._lots[moved.into if onto is None else onto]
        entry.basis = add(entry.basis, fee.basis)
        if entry.loss_basis is not None:
            entry.loss_basis = add(entry.loss_basis, fee.basis if fee.loss is None else fee.loss)
        i = next(k for k in range(len(self._moves) - 1, -1, -1) if self._moves[k].into == moved.into)
        record = self._moves[i]
        self._moves[i] = replace(
            record,
            basis=add(record.basis, fee.basis),
            carried_fee=record.carried_fee + fee.sats,
            carried_from=record.carried_from + fee.lots,
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
        chosen_sats = sum(wanted.values())
        fee = d.fee_sats if isinstance(d, Transfer) else 0
        if (
            isinstance(d, Transfer) and chosen_sats == d.sats - fee and fee
        ):  # the arriving sats named: the fee takes the standing
            return self._with_fee(d, wanted)  # method's next sats, part of the identification (ADR 0043)
        if chosen_sats != d.sats:
            raise EngineError(f"event {d.id!r}: the chosen lots don't add up to its sats")
        return [Pick(lot, sats) for lot, sats in wanted.items()]

    def _with_fee(self, t: Transfer, wanted: dict[str, int]) -> list[Pick]:
        """`wanted` (the arriving sats' lots) and, for the fee, the account's standing method (FIFO) over
        what those leave in it. The fee's lots are part of the identification, so the late check judges
        the whole (ADR 0008 §4, ADR 0043)."""
        need = t.fee_sats
        picks = dict(wanted)
        for o in self._open.get(t.account, []):
            if not need:
                break
            take = min(o.sats - picks.get(o.lot.id, 0), need)
            if take > 0:
                picks[o.lot.id] = picks.get(o.lot.id, 0) + take
                need -= take
        if need:
            raise EngineError(
                f"event {t.id!r}: the account holds too few sats for the fee besides the chosen lots"
            )
        self._arriving[t.id] = dict(wanted)  # these arrive; the rest of the picks is the fee
        chosen = [Pick(lot, sats) for lot, sats in picks.items()]
        self._identified[t.id] = (tuple(chosen), dict(wanted))
        return chosen


def _checked_disposal(d: Disposal) -> Decimal:
    """Check a disposal's own fields; its proceeds."""
    if d.kind not in DISPOSAL_KINDS:
        raise EngineError(f"disposal {d.id!r}: unknown kind {d.kind!r}")
    proceeds = _signed_usd(d.proceeds, d.id)
    if d.kind == "gift_out" and proceeds != 0:
        raise EngineError(f"disposal {d.id!r}: a gift given has no proceeds")
    return proceeds


def _checked_wallet_tx(c: WalletTx, wallets: frozenset[str]) -> Disposal | Transfer | None:
    """Check a wallet transaction's own fields (tracing checks its inputs' and outputs' values); its
    leaving event."""
    if c.account not in wallets:
        raise EngineError(f"wallet tx {c.id!r}: {c.account!r} isn't a coin-traced wallet")
    if not isinstance(c.txid, str) or not c.txid or ":" in c.txid:
        raise EngineError(f"wallet tx {c.id!r}: it names its txid")
    if not isinstance(c.inputs, tuple) or not c.inputs or not isinstance(c.outputs, tuple):
        raise EngineError(f"wallet tx {c.id!r}: its inputs and outputs are tuples, with at least one input")
    for i in c.inputs:
        if not isinstance(i, Spent) or not isinstance(i.outpoint, str) or ":" not in i.outpoint:
            raise EngineError(f"wallet tx {c.id!r}: an input is a Spent naming its outpoint (txid:vout)")
        if i.account is not None and (not isinstance(i.account, str) or not i.account):
            raise EngineError(f"wallet tx {c.id!r}: an input's account is a name or None")
        if not (type(i.value) is int and i.value == 0):  # a zero-value coin carries no lots (tracing)
            _check_sats(i.value, f"wallet tx {c.id!r}")
    if len({i.outpoint for i in c.inputs}) != len(c.inputs):
        raise EngineError(f"wallet tx {c.id!r}: a coin is spent once")
    tracing.check_outputs(c.txid, c.account, c.outputs, c.sats)  # here too: it can block before tracing
    leaving = _checked_leaving(c, wallets)
    named = {o.event for o in c.outputs if o.event is not None}
    if named != ({e.id for e in c.also} | (set() if leaving is None else {leaving.id})):
        raise EngineError(f"wallet tx {c.id!r}: its outputs pay its leaving events, and only those")
    return leaving


def _checked_leaving(c: WalletTx, wallets: frozenset[str]) -> Disposal | Transfer | None:
    if not isinstance(c.also, tuple) or (c.also and c.leaving is None):
        raise EngineError(f"wallet tx {c.id!r}: further leaving events are a tuple, after its leaving event")
    for extra in c.also:
        _checked_event(c, extra, wallets, fee=None)
    if c.leaving is None:
        return None
    return _checked_event(
        c, c.leaving, wallets, fee=None if c.also else c.sats - sum(o.value for o in c.outputs)
    )


def _checked_event(
    c: WalletTx, leaving: object, wallets: frozenset[str], *, fee: int | None
) -> Disposal | Transfer:
    """One of a wallet transaction's leaving events, checked; `fee` the transaction's, which a leaving
    transfer's `fee_sats` must equal (None: not checked, with several leaving events)."""
    if not isinstance(leaving, Disposal | Transfer):
        raise EngineError(f"wallet tx {c.id!r}: its leaving event is a Disposal or a Transfer")
    _check_common(leaving)
    if (leaving.account, leaving.on, leaving.at) != (c.account, c.on, c.at):
        raise EngineError(f"wallet tx {c.id!r}: its leaving event has its account, date and moment")
    if leaving.picks is not None or leaving.identified_at is not None:
        raise EngineError(f"wallet tx {c.id!r}: the coins it spends identify the lots; it chooses none")
    if isinstance(leaving, Transfer):
        _checked_transfer(leaving)
        if leaving.kind not in ("deposit", "self_transfer") or leaving.coins:
            raise EngineError(f"wallet tx {c.id!r}: a wallet's transfer is a deposit or a self-transfer")
        if leaving.kind == "deposit" and leaving.to in wallets:
            raise EngineError(f"wallet tx {c.id!r}: a deposit goes to an exchange account, not a wallet")
        if fee is not None and leaving.fee_sats != fee:
            raise EngineError(f"wallet tx {c.id!r}: its transfer's fee_sats is the transaction's fee, {fee}")
    else:
        _checked_disposal(leaving)
    return leaving


def _leaving(leaving: Disposal | Transfer | None) -> tracing.Leaving | None:
    if leaving is None:
        return None
    if isinstance(leaving, Disposal):
        return tracing.Leaving(leaving.id, leaving.kind, leaving.sats)
    kind: tracing.LeavingKind = "deposit" if leaving.kind == "deposit" else "transfer"
    return tracing.Leaving(leaving.id, kind, leaving.sats - leaving.fee_sats, leaving.to)


def _summed(fragments: Iterable[tracing.Fragment]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in fragments:
        out[f.lot] = out.get(f.lot, 0) + f.sats
    return out


def _checked_transfer(t: Transfer) -> None:
    """Check a transfer's own fields."""
    if t.kind not in TRANSFER_KINDS:
        raise EngineError(f"transfer {t.id!r}: unknown kind {t.kind!r}")
    if not isinstance(t.to, str) or not t.to or t.to == t.account:
        raise EngineError(f"transfer {t.id!r}: it moves coins to another account")
    if isinstance(t.fee_sats, bool) or not isinstance(t.fee_sats, int) or not 0 <= t.fee_sats < t.sats:
        raise EngineError(f"transfer {t.id!r}: its fee is a whole number of sats below the sats it moves")
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


def _fifo_arrivals(t: Transfer, picks: Sequence[Pick]) -> list[tuple[Pick, int]]:
    """Each pick, in the account's FIFO order, with the sats of it that arrive: the arriving sats take the
    lots first and the fee the last sats (ADR 0043). On a short transfer the picks all arrive, up to the
    sats that arrive, and the fee falls on the missing sats (a blocking shortfall)."""
    arriving = min(sum(p.sats for p in picks), t.sats - t.fee_sats)
    out: list[tuple[Pick, int]] = []
    for p in picks:
        arrive = min(p.sats, arriving)
        arriving -= arrive
        out.append((p, arrive))
    return out


def _lower(basis: Decimal, loss: Decimal | None) -> Decimal:
    """The basis a fee part passes to another lot: the lower of a gift's two, the conservative one."""
    return basis if loss is None else min(basis, loss)


def _merged(picks: Sequence[Pick]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for p in picks:
        merged[p.lot] = merged.get(p.lot, 0) + p.sats
    return merged


def _checked_opening(e: OpeningAllocation) -> list[Decimal]:
    """An opening allocation's checks; its lots' bases."""
    if e.on != OPENING_DAY:
        raise EngineError(f"opening allocation {e.id!r}: it is dated {OPENING_DAY}")
    _check_moment(e.recorded_at, f"opening allocation {e.id!r}: when it was recorded")
    if not isinstance(e.method, str) or not e.method.strip():
        raise EngineError(f"opening allocation {e.id!r}: it states the method used")
    if not isinstance(e.lots, tuple) or not e.lots or not all(isinstance(lot, OpeningLot) for lot in e.lots):
        raise EngineError(f"opening allocation {e.id!r}: its lots are a tuple of OpeningLots")
    bases = []
    for lot in e.lots:
        _check_sats(lot.sats, f"opening allocation {e.id!r}")
        if type(lot.acquired) is not date or not GENESIS <= lot.acquired < OPENING_DAY:
            raise EngineError(f"opening allocation {e.id!r}: a lot's date is from genesis to 2024-12-31")
        if type(lot.always_long) is not bool:
            raise EngineError(f"opening allocation {e.id!r}: always_long is True or False")
        bases.append(_usd(lot.basis, e.id))
    return bases


def _checked_moment(event: Disposal | Transfer | WalletTx | Received, last: datetime | None) -> datetime:
    """An event's moment, checked against its tax date and the last moment; it becomes the last."""
    _check_moment(event.at, f"event {event.id!r}: its moment")
    if abs((event.on - event.at.date()).days) > 1:
        raise EngineError(f"event {event.id!r}: its tax date is more than a day from its moment")
    if last is not None and event.at < last:
        raise EngineError(f"event {event.id!r} is out of time order")
    return event.at


def _left(o: _Open) -> Lot:
    """What is left of an open lot."""
    loss_from = None if o.loss_basis is None else o.lot.loss_from
    return Lot(
        o.lot.id,
        o.lot.account,
        o.lot.acquired,
        o.sats,
        o.basis,
        o.lot.always_long,
        o.loss_basis,
        loss_from,
        o.lot.unknown_basis,
    )


def _check_common(event: Event) -> None:
    if not isinstance(event, Acquisition | Disposal | Transfer | WalletTx | Received | OpeningAllocation):
        raise EngineError(
            "an event is an Acquisition, a Disposal, a Transfer, a WalletTx, a Received or an opening"
        )
    if not (isinstance(event.id, str) and event.id and isinstance(event.account, str) and event.account):
        raise EngineError("an event names its id and its account")
    if any(c in event.id for c in RESERVED):  # the engine builds lot ids with them (lot@transfer)
        raise EngineError(f"event {event.id!r}: an id can't contain {' or '.join(RESERVED)}")
    if type(event.on) is not date:  # a datetime is a date too, but not a tax date
        raise EngineError(f"event {event.id!r}: its tax date must be a date")
    if not isinstance(event, WalletTx | OpeningAllocation):  # their sats are their parts', checked there
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

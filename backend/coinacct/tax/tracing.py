"""Coin tracing in a self-custody wallet: which lots each output of one wallet transaction gets (ADR 0041
§2 and §3, proposed; PLAN §7; T-507, T-508, T-509).

Pure, and in sats only: basis, dates and the fee's tax treatment stay with the engine, which applies what
this module decides. One call traces one transaction that spends coins of one coin-traced wallet account:

- **Lots leave oldest first** across all the coins the transaction spends: by the moment the lot reached
  the user, then by its event id (ADR 0041 §2.1). Parts of one lot on several coins are merged.
- **The leaving outputs take lots first, then the fee, then the change** (§2.2). The fee is the sum of all
  inputs minus the sum of all outputs; the change never bears any of it.
- **The canonical order** fills outputs of the same role one after another, largest first, then by the
  output script's bytes (§2.3). An output's position in the transaction never changes the result.
- **A transfer fee's basis** (a deposit, a transfer to another of the user's wallets, or a consolidation
  with no leaving event), when the fee is carried (`fee_treatment="carry"`, the default), passes, per lot
  the fee consumes, to that lot's fragment on the first destination output in the canonical order that
  holds it, else to the first destination output's oldest fragment (§2.4). The destination is the leaving
  outputs, or the change in a consolidation. With `"dispose"`, or for a sale, spend or gift given, the
  fee's lots are part of the disposal or a small disposal: the engine's call, and nothing is carried.
- **Zero-value outputs** (an OP_RETURN, say) carry no sats and get no lots.
- **Who owns what:** a change output belongs to the wallet's account; a deposit's outputs to one of the
  user's custodial (exchange) accounts, a transfer's to another of the user's self-custody wallets; a
  sale's, spend's or gift's to nobody the user records. An input the user doesn't own carries no lots
  (`fragments=()`). A zero-value coin carries none either.
- **The caller resolves links and the pool first** (ADR 0041 §1, §4): each of the wallet's inputs must
  carry exactly its value in lots. Uncovered sats are a block or a pool draw there, never an input here.
- **Not decided here:** whether the receiving wallet is coin-traced or on whole-wallet FIFO (§3's mixed
  transfers), and whether a fee lot that is a gift's or of unknown basis may carry onto another lot. Both
  need the lots' and accounts' details, so the engine blocks them.

**What blocks** (`Blocked`, until a later ADR adds a rule; §2, §3): an input the user doesn't own
(PayJoin, CoinJoin), an input from another of the user's accounts, more than one leaving event, an
output to someone else (or to another of the user's accounts) that the leaving event doesn't cover, a
leaving output whose owner doesn't fit its event's kind, two identical outputs (same value and script,
whatever their role) that would get different lots or one of which would receive a transfer fee's basis,
a leaving event whose sats differ from its outputs', and a transaction whose sats all go to the fee. A
block names every output (or input, or event) involved, in a fixed order. Inconsistent input (a coin
whose fragments don't add up to its value, outputs worth more than the inputs, a leaving event on no
output, a wrong type) is the caller's error and raises `EngineError`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from coinacct.domain.chain import MAX_SATS
from coinacct.tax.engine import EngineError, FeeTreatment

type LeavingKind = Literal["sell", "spend", "gift_out", "deposit", "transfer"]
type BlockReason = Literal[
    "shared",
    "several_accounts",
    "several_leaving_events",
    "unclassified_output",
    "mismatched_owner",
    "identical_outputs",
    "amount_mismatch",
    "fee_only",
]
type AccountKind = Literal["self_custody", "custodial"]
type _Filled = list[tuple[Output, tuple[Fragment, ...]]]

LEAVING_KINDS: Final[frozenset[str]] = frozenset({"sell", "spend", "gift_out", "deposit", "transfer"})
CARRIED: Final[frozenset[str]] = frozenset({"deposit", "transfer"})


@dataclass(frozen=True)
class Fragment:
    """Part of a lot on a coin: `entered` (UTC) is when the lot reached the user, `event` its event id."""

    lot: str
    sats: int
    entered: datetime
    event: str


@dataclass(frozen=True)
class Input:
    """A coin the transaction spends; `account` is its owner among the user's accounts, or None."""

    account: str | None
    value: int
    fragments: tuple[Fragment, ...]


@dataclass(frozen=True)
class Output:
    """`account`: the user's account that owns it, or None, and `kind` that account's kind. `event`: the
    leaving event it pays, or None."""

    vout: int
    value: int
    script: bytes
    account: str | None = None
    event: str | None = None
    kind: AccountKind | None = None


@dataclass(frozen=True)
class Leaving:
    """The transaction's one leaving event: its id, kind and sats (what its outputs receive, not the fee)."""

    id: str
    kind: LeavingKind
    sats: int


@dataclass(frozen=True)
class WalletTx:
    txid: str
    account: str
    inputs: tuple[Input, ...]
    outputs: tuple[Output, ...]
    leaving: Leaving | None = None


@dataclass(frozen=True)
class FeeCarry:
    """`sats` of `lot` went to the fee; their basis passes to lot `onto`'s fragment on output `vout`."""

    lot: str
    sats: int
    vout: int
    onto: str


@dataclass(frozen=True)
class Traced:
    """Each output's fragments (by vout, zero-value outputs left out), the fee's, and where its basis goes."""

    outputs: tuple[tuple[int, tuple[Fragment, ...]], ...]
    fee: tuple[Fragment, ...]
    carries: tuple[FeeCarry, ...]


@dataclass(frozen=True)
class Blocked:
    txid: str
    reason: BlockReason
    detail: str


def trace(tx: WalletTx, fee_treatment: FeeTreatment = "carry") -> Traced | Blocked:
    """Trace one wallet transaction (ADR 0041 §2), or say why it blocks (§3)."""
    _check(tx, fee_treatment)
    blocked = _unsupported(tx)
    if blocked is not None:
        return blocked
    paid = [o for o in tx.outputs if o.value > 0]
    leaving = sorted((o for o in paid if o.event is not None), key=_canonical)
    change = sorted((o for o in paid if o.event is None), key=_canonical)
    fee = sum(i.value for i in tx.inputs) - sum(o.value for o in tx.outputs)
    stream = _Stream(_merged((f for i in tx.inputs for f in i.fragments), tx.txid))
    filled = [(o, stream.take(o.value)) for o in leaving]
    fee_parts = stream.take(fee)
    filled += [(o, stream.take(o.value)) for o in change]
    if not filled:
        return Blocked(tx.txid, "fee_only", "every sat goes to the fee")
    same = _identical(filled)
    if same is not None:
        return Blocked(tx.txid, "identical_outputs", f"outputs {_list(same)} would get different lots")
    carries: tuple[FeeCarry, ...] = ()
    carried = tx.leaving is None or tx.leaving.kind in CARRIED
    if fee_parts and carried and fee_treatment == "carry":
        destination = filled[: len(leaving)] if leaving else filled
        carries = tuple(_carry(f, destination) for f in fee_parts)
        twins = _twins(filled, {c.vout for c in carries})
        if twins is not None:
            detail = f"outputs {_list(twins)} would get different basis from the fee"
            return Blocked(tx.txid, "identical_outputs", detail)
    return Traced(tuple(sorted((o.vout, parts) for o, parts in filled)), fee_parts, carries)


def _unsupported(tx: WalletTx) -> Blocked | None:
    """The cases ADR 0041 §3 doesn't support yet, found before any lot is taken."""
    return _foreign_inputs(tx) or _unsupported_outputs(tx)


def _foreign_inputs(tx: WalletTx) -> Blocked | None:
    unowned = [n for n, i in enumerate(tx.inputs) if i.account is None]
    if unowned:
        return Blocked(tx.txid, "shared", f"inputs {_list(unowned)} aren't the user's")
    others = sorted({i.account for i in tx.inputs if i.account is not None and i.account != tx.account})
    if others:
        return Blocked(tx.txid, "several_accounts", f"inputs from accounts {', '.join(map(repr, others))}")
    return None


def _unsupported_outputs(tx: WalletTx) -> Blocked | None:
    paid = [o for o in tx.outputs if o.value > 0]
    events = {o.event for o in paid if o.event is not None}
    if len(events) > 1:
        vouts = sorted(o.vout for o in paid if o.event is not None)
        detail = f"events {', '.join(sorted(events))} on outputs {_list(vouts)}"
        return Blocked(tx.txid, "several_leaving_events", detail)
    if events and (tx.leaving is None or events != {tx.leaving.id}):
        raise EngineError(f"tx {tx.txid}: an output names an event that isn't its leaving event")
    stray = sorted(o.vout for o in paid if o.event is None and o.account != tx.account)
    if stray:
        return Blocked(tx.txid, "unclassified_output", f"outputs {_list(stray)} have no recorded event")
    if tx.leaving is None:
        return None
    leaving = [o for o in paid if o.event is not None]
    if not leaving:
        raise EngineError(f"tx {tx.txid}: leaving event {tx.leaving.id!r} pays no output")
    mismatched = sorted(o.vout for o in leaving if not _owner_fits(o, tx.leaving.kind, tx.account))
    if mismatched:
        detail = f"outputs {_list(mismatched)} aren't owned as a {tx.leaving.kind} needs"
        return Blocked(tx.txid, "mismatched_owner", detail)
    paid_out = sum(o.value for o in leaving)
    if paid_out != tx.leaving.sats:
        detail = f"event {tx.leaving.id!r} has {tx.leaving.sats} sats, its outputs {paid_out}"
        return Blocked(tx.txid, "amount_mismatch", detail)
    return None


def _owner_fits(o: Output, kind: str, account: str) -> bool:
    """A deposit pays one of the user's exchange accounts, a transfer another of the user's wallets; a
    disposal pays nobody the user records."""
    if kind == "deposit":
        return o.account is not None and o.account != account and o.kind == "custodial"
    if kind == "transfer":
        return o.account is not None and o.account != account and o.kind == "self_custody"
    return o.account is None


def _list(vouts: Sequence[int]) -> str:
    return ", ".join(map(str, vouts))


def _canonical(o: Output) -> tuple[int, bytes]:
    return (-o.value, o.script)


def _merged(fragments: Iterable[Fragment], txid: str) -> list[Fragment]:
    by_lot: dict[str, Fragment] = {}
    for f in fragments:
        seen = by_lot.get(f.lot)
        if seen is None:
            by_lot[f.lot] = f
        elif (seen.entered, seen.event) != (f.entered, f.event):
            raise EngineError(f"tx {txid}: lot {f.lot!r} has parts with different dates or events")
        else:
            by_lot[f.lot] = Fragment(f.lot, seen.sats + f.sats, f.entered, f.event)
    return sorted(by_lot.values(), key=lambda f: (f.entered, f.event, f.lot))


class _Stream:
    """The spent lots, oldest first; each `take` cuts the next sats off the front."""

    def __init__(self, fragments: list[Fragment]) -> None:
        self._rest = fragments

    def take(self, sats: int) -> tuple[Fragment, ...]:
        parts: list[Fragment] = []
        while sats:
            head = self._rest[0]
            used = min(sats, head.sats)
            parts.append(Fragment(head.lot, used, head.entered, head.event))
            sats -= used
            if used == head.sats:
                self._rest.pop(0)
            else:
                self._rest[0] = Fragment(head.lot, head.sats - used, head.entered, head.event)
        return tuple(parts)


def _groups(filled: _Filled) -> list[_Filled]:
    """Identical outputs (same value and script, whatever their role), in groups of two or more."""
    groups: dict[tuple[int, bytes], _Filled] = {}
    for o, parts in filled:
        groups.setdefault((o.value, o.script), []).append((o, parts))
    return [g for g in groups.values() if len(g) > 1]


def _identical(filled: _Filled) -> list[int] | None:
    """The vouts of every group of identical outputs whose lots differ."""
    vouts = [
        o.vout
        for group in _groups(filled)
        if len({tuple((f.lot, f.sats) for f in parts) for _, parts in group}) > 1
        for o, _ in group
    ]
    return sorted(vouts) or None


def _twins(filled: _Filled, vouts: set[int]) -> list[int] | None:
    """The vouts of a group of identical outputs any of which receives carried fee basis: which one gets
    it would depend on their order in the transaction."""
    hit = [o.vout for group in _groups(filled) if vouts & {o.vout for o, _ in group} for o, _ in group]
    return sorted(hit) or None


def _carry(fee: Fragment, destination: _Filled) -> FeeCarry:
    for o, parts in destination:
        if any(p.lot == fee.lot for p in parts):
            return FeeCarry(fee.lot, fee.sats, o.vout, fee.lot)
    first, parts = destination[0]
    return FeeCarry(fee.lot, fee.sats, first.vout, parts[0].lot)


def _check(tx: object, fee_treatment: object) -> None:
    if not isinstance(tx, WalletTx) or not _name(tx.txid) or not _name(tx.account):
        raise EngineError("trace needs a WalletTx with a txid and an account")
    if not isinstance(fee_treatment, str) or fee_treatment not in ("carry", "dispose"):
        raise EngineError(f"tx {tx.txid}: unknown fee treatment")
    if not isinstance(tx.inputs, tuple) or not isinstance(tx.outputs, tuple) or not tx.inputs:
        raise EngineError(f"tx {tx.txid}: the inputs and outputs must be tuples, with at least one input")
    for i in tx.inputs:
        _check_input(i, tx)
    _check_outputs(tx)
    if tx.leaving is not None:
        if not isinstance(tx.leaving, Leaving) or not _name(tx.leaving.id):
            raise EngineError(f"tx {tx.txid}: the leaving event must be a Leaving with an id")
        if not isinstance(tx.leaving.kind, str) or tx.leaving.kind not in LEAVING_KINDS:
            raise EngineError(f"tx {tx.txid}: unknown leaving kind")
        _sats(tx.leaving.sats, tx.txid)


def _check_input(i: object, tx: WalletTx) -> None:
    if not isinstance(i, Input) or not _optional_name(i.account) or not isinstance(i.fragments, tuple):
        raise EngineError(f"tx {tx.txid}: an input must be an Input, its account a name or None")
    if not (type(i.value) is int and i.value == 0 and not i.fragments):
        _sats(i.value, tx.txid)
    for f in i.fragments:
        if not isinstance(f, Fragment) or not _name(f.lot) or not _name(f.event):
            raise EngineError(f"tx {tx.txid}: a lot fragment must be a Fragment with a lot and an event")
        _sats(f.sats, tx.txid)
        if not isinstance(f.entered, datetime) or f.entered.tzinfo is not UTC:
            raise EngineError(f"tx {tx.txid}: a lot's date must be a timezone-aware UTC datetime")
    if i.account == tx.account and sum(f.sats for f in i.fragments) != i.value:
        raise EngineError(f"tx {tx.txid}: an input's lots don't add up to its value")
    if i.account is None and i.fragments:
        raise EngineError(f"tx {tx.txid}: an input the user doesn't own carries lots")


def _check_outputs(tx: WalletTx) -> None:
    for o in tx.outputs:
        if not isinstance(o, Output) or not _optional_name(o.account) or not _optional_name(o.event):
            raise EngineError(
                f"tx {tx.txid}: an output must be an Output, its account and event names or None"
            )
        if o.kind not in (None, "self_custody", "custodial") or (o.kind is None) != (o.account is None):
            raise EngineError(f"tx {tx.txid}: an output of the user's has its account's kind, and only then")
        if o.value == 0 and type(o.value) is int and o.event is not None:
            raise EngineError(f"tx {tx.txid}: a zero-value output pays no event")
        if isinstance(o.vout, bool) or not isinstance(o.vout, int) or o.vout < 0:
            raise EngineError(f"tx {tx.txid}: a vout must be a whole number of 0 or more")
        if not isinstance(o.script, bytes):
            raise EngineError(f"tx {tx.txid}: an output's script must be bytes")
        if not (type(o.value) is int and o.value == 0):
            _sats(o.value, tx.txid)
    vouts = [o.vout for o in tx.outputs]
    if len(set(vouts)) != len(vouts):
        raise EngineError(f"tx {tx.txid}: two outputs share a vout")
    if sum(o.value for o in tx.outputs) > sum(i.value for i in tx.inputs):
        raise EngineError(f"tx {tx.txid}: the outputs are worth more than the inputs")


def _name(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _optional_name(value: object) -> bool:
    return value is None or _name(value)


def _sats(sats: object, txid: str) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or not 0 < sats <= MAX_SATS:
        raise EngineError(f"tx {txid}: sats must be a positive whole number no larger than the supply")

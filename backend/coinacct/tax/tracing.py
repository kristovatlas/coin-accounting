"""Coin tracing in a self-custody wallet: which lots each output of one wallet transaction gets (ADR 0041
§2 and §3, proposed; PLAN §7; T-507, T-508, T-509).

Pure, and in sats only: basis, dates and the fee's tax treatment stay with the engine, which applies what
this module decides. One call traces one transaction that spends coins of one coin-traced wallet account:

- **Lots leave oldest first** across all the coins the transaction spends: by the moment the lot reached
  the user, then by its event id (ADR 0041 §2.1). Parts of one lot on several coins are merged.
- **The leaving outputs take lots first, then the fee, then the change** (§2.2). The fee is the sum of all
  inputs minus the sum of all outputs; the change never bears any of it.
- **The canonical order** fills outputs of the same role one after another, largest first, then by the
  output script's bytes (§2.3). An output's position in the transaction never changes a figure.
- **A transfer fee's basis** (a deposit, a transfer to another of the user's wallets, or a consolidation
  with no leaving event) passes, per lot the fee consumes, to that lot's fragment on the first
  destination output in the canonical order that holds it, else to the first destination output's oldest
  fragment (§2.4). The destination is the leaving outputs, or the change in a consolidation. For a sale,
  spend or gift given, the fee's lots are part of the disposal or a small disposal: the engine's call.
- **Zero-value outputs** (an OP_RETURN, say) carry no sats and get no lots.

**What blocks** (`Blocked`, until a later ADR adds a rule; §2, §3): an input the user doesn't own
(PayJoin, CoinJoin), an input from another of the user's accounts, more than one leaving event, an
output to someone else (or to another of the user's accounts) that the leaving event doesn't cover, two
identical outputs (same value and script) that would get different lots or one of which would receive a
transfer fee's basis, a leaving event whose sats
differ from its outputs', and a transaction whose sats all go to the fee. Inconsistent input (a coin
whose fragments don't add up to its value, outputs worth more than the inputs, a leaving event on no
output) is the caller's error and raises `EngineError`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from coinacct.domain.chain import MAX_SATS
from coinacct.tax.engine import EngineError

type LeavingKind = Literal["sell", "spend", "gift_out", "deposit", "transfer"]
type BlockReason = Literal[
    "shared",
    "several_accounts",
    "several_leaving_events",
    "unclassified_output",
    "identical_outputs",
    "amount_mismatch",
    "fee_only",
]

CARRIED: frozenset[str] = frozenset({"deposit", "transfer"})


@dataclass(frozen=True)
class Fragment:
    """Part of a lot on a coin: `entered` is when the lot reached the user, `event` the lot's event id."""

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
    """`account`: the user's account that owns it, or None. `event`: the leaving event it pays, or None."""

    vout: int
    value: int
    script: bytes
    account: str | None = None
    event: str | None = None


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


def trace(tx: WalletTx) -> Traced | Blocked:
    """Trace one wallet transaction (ADR 0041 §2), or say why it blocks (§3)."""
    _check(tx)
    blocked = _unsupported(tx)
    if blocked is not None:
        return blocked
    paid = [o for o in tx.outputs if o.value > 0]
    leaving = sorted((o for o in paid if o.event is not None), key=_canonical)
    change = sorted((o for o in paid if o.event is None), key=_canonical)
    fee = sum(i.value for i in tx.inputs) - sum(o.value for o in tx.outputs)
    stream = _Stream(_merged(f for i in tx.inputs for f in i.fragments))
    filled = [(o, stream.take(o.value)) for o in leaving]
    fee_parts = stream.take(fee)
    filled += [(o, stream.take(o.value)) for o in change]
    same = _identical(filled)
    if same is not None:
        detail = f"outputs {', '.join(map(str, same))} would get different lots"
        return Blocked(tx.txid, "identical_outputs", detail)
    carries: tuple[FeeCarry, ...] = ()
    if fee_parts and (tx.leaving is None or tx.leaving.kind in CARRIED):
        destination = filled[: len(leaving)] if leaving else filled
        if not destination:
            return Blocked(tx.txid, "fee_only", "every sat goes to the fee")
        carries = tuple(_carry(f, destination) for f in fee_parts)
        twins = _twins(filled, {c.vout for c in carries})
        if twins is not None:
            detail = f"outputs {', '.join(map(str, twins))} would get different basis from the fee"
            return Blocked(tx.txid, "identical_outputs", detail)
    return Traced(tuple(sorted((o.vout, parts) for o, parts in filled)), fee_parts, carries)


def _unsupported(tx: WalletTx) -> Blocked | None:
    """The cases ADR 0041 §3 doesn't support yet, found before any lot is taken."""
    for i in tx.inputs:
        if i.account is None:
            return Blocked(tx.txid, "shared", "an input the user doesn't own")
        if i.account != tx.account:
            return Blocked(tx.txid, "several_accounts", f"an input from account {i.account!r}")
    paid = [o for o in tx.outputs if o.value > 0]
    events = {o.event for o in paid if o.event is not None}
    if len(events) > 1:
        return Blocked(tx.txid, "several_leaving_events", ", ".join(sorted(events)))
    if events and (tx.leaving is None or events != {tx.leaving.id}):
        raise EngineError(f"tx {tx.txid}: an output names an event that isn't its leaving event")
    stray = [o for o in paid if o.event is None and o.account != tx.account]
    if stray:
        return Blocked(tx.txid, "unclassified_output", f"output {stray[0].vout} has no recorded event")
    if tx.leaving is not None:
        paid_out = sum(o.value for o in paid if o.event is not None)
        if not paid_out:
            raise EngineError(f"tx {tx.txid}: leaving event {tx.leaving.id!r} pays no output")
        if paid_out != tx.leaving.sats:
            detail = f"event {tx.leaving.id!r} has {tx.leaving.sats} sats, its outputs {paid_out}"
            return Blocked(tx.txid, "amount_mismatch", detail)
    return None


def _canonical(o: Output) -> tuple[int, bytes]:
    return (-o.value, o.script)


def _merged(fragments: Iterable[Fragment]) -> list[Fragment]:
    by_lot: dict[str, Fragment] = {}
    for f in fragments:
        seen = by_lot.get(f.lot)
        if seen is None:
            by_lot[f.lot] = f
        elif (seen.entered, seen.event) != (f.entered, f.event):
            raise EngineError(f"lot {f.lot!r} has parts with different dates or events")
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


def _identical(filled: list[tuple[Output, tuple[Fragment, ...]]]) -> tuple[int, ...] | None:
    """The vouts of a group of identical outputs (same value, script and event) whose lots differ."""
    groups: dict[tuple[int, bytes, str | None], list[tuple[Output, tuple[Fragment, ...]]]] = {}
    for o, parts in filled:
        groups.setdefault((o.value, o.script, o.event), []).append((o, parts))
    for group in groups.values():
        if len({tuple((f.lot, f.sats) for f in parts) for _, parts in group}) > 1:
            return tuple(sorted(o.vout for o, _ in group))
    return None


def _twins(filled: list[tuple[Output, tuple[Fragment, ...]]], vouts: set[int]) -> tuple[int, ...] | None:
    """The vouts of a group of identical outputs one of which, but not all, receives carried fee basis."""
    groups: dict[tuple[int, bytes, str | None], list[int]] = {}
    for o, _ in filled:
        groups.setdefault((o.value, o.script, o.event), []).append(o.vout)
    for group in groups.values():
        if len(group) > 1 and vouts & set(group):
            return tuple(sorted(group))
    return None


def _carry(fee: Fragment, destination: list[tuple[Output, tuple[Fragment, ...]]]) -> FeeCarry:
    for o, parts in destination:
        if any(p.lot == fee.lot for p in parts):
            return FeeCarry(fee.lot, fee.sats, o.vout, fee.lot)
    first, parts = destination[0]
    return FeeCarry(fee.lot, fee.sats, first.vout, parts[0].lot)


def _check(tx: WalletTx) -> None:
    if not tx.inputs:
        raise EngineError(f"tx {tx.txid}: no inputs")
    for i in tx.inputs:
        _sats(i.value, tx.txid)
        for f in i.fragments:
            _sats(f.sats, tx.txid)
        if sum(f.sats for f in i.fragments) != i.value:
            raise EngineError(f"tx {tx.txid}: an input's lots don't add up to its value")
    vouts = [o.vout for o in tx.outputs]
    if len(set(vouts)) != len(vouts):
        raise EngineError(f"tx {tx.txid}: two outputs share a vout")
    for o in tx.outputs:
        if o.value != 0:
            _sats(o.value, tx.txid)
    if sum(o.value for o in tx.outputs) > sum(i.value for i in tx.inputs):
        raise EngineError(f"tx {tx.txid}: the outputs are worth more than the inputs")
    if tx.leaving is not None:
        _sats(tx.leaving.sats, tx.txid)


def _sats(sats: object, txid: str) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or not 0 < sats <= MAX_SATS:
        raise EngineError(f"tx {txid}: sats must be a positive whole number no larger than the supply")

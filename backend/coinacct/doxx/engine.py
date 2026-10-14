"""The doxx rules (ADR 0010; PLAN §5; T-504, T-505, T-511): pure, graph and tag values in, doxx tags out.

A coin's **doxx set** is the set of entities that know the user's identity (`identity`) and can link
the coin to them. Each tag has a confidence, `certain` or `inferred`, and the reason it was given:

- **Paid to K** (`paid_to`): a transaction paying an output K owns doxxes every owned input and every
  owned output (the change) to K, certainly; in a transaction flagged `mixing`, the owned inputs only
  as inferred, since K can't tell which inputs are the user's.
- **Received from K** (`received_from`): a coin the user recorded as coming from K (a withdrawal, a
  salary), certainly. It comes from the recorded event, never from recognising K's addresses.
- **Address reuse** (`address_reuse`): every coin ever paid to an address that holds a coin doxxed to
  K, with that coin's confidence.
- **Forward** (`forward`): a transaction's owned outputs inherit the union of its owned inputs' sets,
  with their confidence; through a `mixing` transaction only as inferred.
- **Backward cluster** (`cluster_backward`): addresses co-spent in a transaction that isn't `mixing`
  form a cluster (the common-input heuristic); a K-doxxed coin at one of them doxxes the coins at the
  others to K, inferred.

The rules run until nothing changes; a tag only ever strengthens (inferred to certain), so they end.
Doxx sets never reach third parties' outputs. The user's overrides (`Override`) persist: one adds a tag
(certain, `manual`), which then propagates; one removes a tag, which no rule gives back. Heuristics
only suggest: the caller shows these as "known links", never as "private" or "clean" (T-505), and the
user confirms suggestions elsewhere. Chain-analysis heuristics the app doesn't model are accepted risk
R-3.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

type Confidence = Literal["certain", "inferred"]
type Reason = Literal["paid_to", "received_from", "address_reuse", "forward", "cluster_backward", "manual"]

STRENGTH: Final[dict[str, int]] = {"inferred": 1, "certain": 2}


class DoxxError(ValueError):
    """The graph or tags are invalid input: the caller must not have produced them."""


@dataclass(frozen=True)
class Coin:
    """A transaction's input or output: its outpoint ("txid:vout"), the address (or script hash) it
    pays, whether the user owns it, and the entity that owns it otherwise, if known."""

    outpoint: str
    address: str
    owned: bool
    entity: str | None = None


@dataclass(frozen=True)
class Tx:
    """A transaction of the user's graph, in chain order (a coin's paying transaction before its
    spending one)."""

    txid: str
    inputs: tuple[Coin, ...]
    outputs: tuple[Coin, ...]
    mixing: bool = False  # CoinJoin- or PayJoin-like (the user's flag)


@dataclass(frozen=True)
class Received:
    """A coin the user recorded as coming from `entity` (a withdrawal, a salary)."""

    outpoint: str
    entity: str


@dataclass(frozen=True)
class Override:
    """The user's tag: `present` adds it (certain), otherwise it is removed and stays removed."""

    outpoint: str
    entity: str
    present: bool


@dataclass(frozen=True)
class Tag:
    """`outpoint` is doxxed to `entity`, with `confidence`, by `reason` (from transaction `txid`, where
    a transaction gave it)."""

    outpoint: str
    entity: str
    confidence: Confidence
    reason: Reason
    txid: str | None = None


def doxx(
    txs: Sequence[Tx],
    identity: frozenset[str],
    received: Sequence[Received] = (),
    overrides: Sequence[Override] = (),
) -> tuple[Tag, ...]:
    """Every doxx tag on the user's coins, by outpoint then entity."""
    _check(txs, identity, received, overrides)
    state = _State(txs, overrides)
    for r in received:
        if r.entity in identity:
            state.give(r.outpoint, r.entity, "certain", "received_from", None)
    for o in overrides:
        if o.present:
            state.give(o.outpoint, o.entity, "certain", "manual", None)
    for t in txs:
        _paid_to(state, t, identity)
    clusters = _clusters(txs)
    while state.changed:
        state.changed = False
        for t in txs:
            _forward(state, t)
        _reuse(state)
        _cluster(state, clusters)
    return tuple(
        Tag(outpoint, k, conf, reason, txid)
        for outpoint in sorted(state.tags)
        for k, (conf, reason, txid) in sorted(state.tags[outpoint].items())
    )


def _paid_to(state: _State, t: Tx, identity: frozenset[str]) -> None:
    """Paid to K: K sees every owned input (only inferred in a mixing tx) and every owned output."""
    for k in sorted({o.entity for o in t.outputs if not o.owned and o.entity in identity}):
        for i in t.inputs:
            if i.owned:
                state.give(i.outpoint, k, "inferred" if t.mixing else "certain", "paid_to", t.txid)
        for o in t.outputs:
            if o.owned:
                state.give(o.outpoint, k, "certain", "paid_to", t.txid)


def _forward(state: _State, t: Tx) -> None:
    """Forward: the owned outputs inherit the owned inputs' sets (only inferred through a mixing tx)."""
    inherited: dict[str, Confidence] = {}
    for i in t.inputs:
        if i.owned:
            for k, (conf, _, _) in state.tags.get(i.outpoint, {}).items():
                if k not in inherited or STRENGTH[conf] > STRENGTH[inherited[k]]:
                    inherited[k] = conf
    for o in t.outputs:
        if o.owned:
            for k, conf in sorted(inherited.items()):
                state.give(o.outpoint, k, "inferred" if t.mixing else conf, "forward", t.txid)


def _reuse(state: _State) -> None:
    """Address reuse: every coin at an address takes the tags of every other coin there."""
    for coins in state.by_address.values():
        for c in coins:
            for k, (conf, _, _) in list(state.tags.get(c, {}).items()):
                for other in coins:
                    state.give(other, k, conf, "address_reuse", None)


def _cluster(state: _State, clusters: list[list[str]]) -> None:
    """Backward cluster: a tag at one address of a common-input cluster is inferred at all of them."""
    for members in clusters:
        entities = {k for a in members for c in state.by_address.get(a, ()) for k in state.tags.get(c, {})}
        for k in sorted(entities):
            for a in members:
                for c in state.by_address.get(a, ()):
                    state.give(c, k, "inferred", "cluster_backward", None)


class _State:
    def __init__(self, txs: Sequence[Tx], overrides: Sequence[Override]) -> None:
        self.tags: dict[str, dict[str, tuple[Confidence, Reason, str | None]]] = {}
        self.removed = {(o.outpoint, o.entity) for o in overrides if not o.present}
        self.owned: set[str] = set()
        self.by_address: dict[str, list[str]] = {}
        for t in txs:
            for c in (*t.inputs, *t.outputs):
                if c.owned and c.outpoint not in self.owned:
                    self.owned.add(c.outpoint)
                    self.by_address.setdefault(c.address, []).append(c.outpoint)
        self.changed = True

    def give(self, outpoint: str, entity: str, conf: Confidence, reason: Reason, txid: str | None) -> None:
        """Tag a coin, or strengthen its tag: never weaken one, nor give back one the user removed."""
        if outpoint not in self.owned or (outpoint, entity) in self.removed:
            return
        mine = self.tags.setdefault(outpoint, {})
        held = mine.get(entity)
        if held is None or STRENGTH[conf] > STRENGTH[held[0]]:
            mine[entity] = (conf, reason, txid)
            self.changed = True


def _clusters(txs: Sequence[Tx]) -> list[list[str]]:
    """The common-input clusters of the user's addresses: co-spent in a transaction that isn't mixing."""
    parent: dict[str, str] = {}

    def find(a: str) -> str:
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in txs:
        spent = [i.address for i in t.inputs if i.owned]
        for a in spent:
            find(a)
        if not t.mixing:
            for a in spent[1:]:
                parent[find(a)] = find(spent[0])
    groups: dict[str, list[str]] = {}
    for a in sorted(parent):
        groups.setdefault(find(a), []).append(a)
    return [g for g in groups.values() if len(g) > 1]


def _check(txs: object, identity: object, received: object, overrides: object) -> None:
    if not isinstance(identity, frozenset) or not all(_name(k) for k in identity):
        raise DoxxError("identity is a frozenset of entity names")
    if not isinstance(txs, Sequence) or not all(isinstance(t, Tx) for t in txs):
        raise DoxxError("txs is a sequence of Tx")
    seen: set[str] = set()
    for t in txs:
        if not _name(t.txid) or t.txid in seen or type(t.mixing) is not bool:
            raise DoxxError(f"tx {t.txid!r}: a tx has a unique txid and a mixing flag")
        seen.add(t.txid)
        if not isinstance(t.inputs, tuple) or not isinstance(t.outputs, tuple):
            raise DoxxError(f"tx {t.txid!r}: its inputs and outputs are tuples")
        _check_coins(t.txid, (*t.inputs, *t.outputs))
    for r in _items(received, Received, "received"):
        if not _name(r.outpoint) or not _name(r.entity):
            raise DoxxError("a received coin names its outpoint and entity")
    for o in _items(overrides, Override, "overrides"):
        if not _name(o.outpoint) or not _name(o.entity) or type(o.present) is not bool:
            raise DoxxError("an override names its outpoint and entity, and whether it is present")


def _check_coins(txid: str, coins: Iterable[object]) -> None:
    for c in coins:
        if not isinstance(c, Coin) or not _name(c.outpoint) or ":" not in c.outpoint or not _name(c.address):
            raise DoxxError(f"tx {txid!r}: a coin is a Coin with its outpoint and address")
        if type(c.owned) is not bool or (c.entity is not None and not _name(c.entity)):
            raise DoxxError(f"tx {txid!r}: a coin says whether it is owned, and its entity is a name or None")
        if c.owned and c.entity is not None:
            raise DoxxError(f"tx {txid!r}: a coin of the user's has no other entity")


def _items[T](values: object, kind: type[T], name: str) -> list[T]:
    if not isinstance(values, Sequence) or not all(isinstance(v, kind) for v in values):
        raise DoxxError(f"{name} is a sequence of {kind.__name__}")
    return list(values)


def _name(value: object) -> bool:
    return isinstance(value, str) and value != ""

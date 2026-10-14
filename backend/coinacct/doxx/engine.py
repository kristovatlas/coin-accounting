"""The doxx rules (ADR 0010; PLAN §5; T-504, T-505, T-511): pure, graph and tag values in, doxx tags out.

A coin's **doxx set** is the set of entities that know the user's identity (`identity`) and can link
the coin to them. Each tag has a confidence, `certain` or `inferred`, the reason it was given (PLAN
§2's `doxx_tag.reason`), the transaction and coin it came from, and whether it rests on a heuristic:

- **Paid to K** (`paid_to`, `change_of`): a transaction paying an output K owns doxxes every owned
  input (`paid_to`) and every owned output, the change (`change_of`), to K, certainly; in a transaction
  flagged `mixing`, the owned inputs only as inferred, since K can't tell which inputs are the user's.
- **Received from K** (`received_from`): a coin the user recorded as coming from K (a withdrawal, a
  salary), certainly. It comes from the recorded event, never from recognising K's addresses.
- **Address reuse** (`address_reuse`): every coin ever paid to an address that holds a coin doxxed to
  K. The rule is certain (ADR 0010), so the coin takes its source's confidence: certain from a certain
  tag, inferred from an inferred one.
- **Forward** (`change_of`): a transaction's owned outputs inherit the union of its owned inputs'
  sets, with their confidence; through a `mixing` transaction only as inferred (ADR 0010).
- **Backward cluster** (`cluster_backward`): addresses co-spent in a transaction that isn't `mixing`
  form a cluster (the common-input heuristic); a K-doxxed coin at one of them doxxes the coins at the
  others to K, inferred. The cluster is the one the user's whole history shows (ADR 0010's "earlier"
  co-spends are an open question for the owner: a co-spend after the doxxing counts too).

A rule never makes a link more certain than the link it starts from: reading ADR 0010's "certain" for
reuse and forward as the rule's own confidence keeps a guess (a cluster, a mixing tx) from turning
into a certain link elsewhere. Whether ADR 0010 means that is a question for the owner. A tag that
rests on the cluster heuristic, directly or through other rules, is marked `heuristic`, so the user can
see it rests on a guess and confirm or reject it (T-504). Heuristics only suggest: the caller shows these
as "known links", never as "private" or "clean" (T-505). Chain-analysis heuristics the app doesn't
model are accepted risk R-3.

The rules run until nothing changes. A tag only strengthens (inferred to certain, or a heuristic tag to
one that isn't), so they end. Of two equal tags the earlier reason (manual, received_from, paid_to,
change_of, cluster_backward, address_reuse), then the lowest txid and coin, wins, so the result doesn't
depend on the order of the input. Doxx sets never reach third parties' outputs. The user's overrides
persist: one adds a tag (certain, `manual`), which then propagates; one removes a tag, which no rule
gives back. A recorded receipt or an override the engine can't apply (its coin isn't one of the user's,
its entity doesn't know the user's identity) is returned as `unresolved`, and a tag a removal suppressed
as `suppressed`, so the caller can show both (T-511: no certain link is lost without a word).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

type Confidence = Literal["certain", "inferred"]
type Reason = Literal["paid_to", "change_of", "received_from", "address_reuse", "cluster_backward", "manual"]

STRENGTH: Final[dict[str, int]] = {"inferred": 1, "certain": 2}
REASON_ORDER: Final[dict[str, int]] = {
    "manual": 0,
    "received_from": 1,
    "paid_to": 2,
    "change_of": 3,
    "cluster_backward": 4,  # before reuse: a coin at a clustered address names the cluster, its origin
    "address_reuse": 5,
}


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
    """A transaction of the user's graph: its outputs' outpoints are "txid:index"."""

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
    """`outpoint` is doxxed to `entity`, with `confidence`, by `reason`: from transaction `txid` and coin
    `source` where a rule gave it; `heuristic` when it rests on the common-input cluster."""

    outpoint: str
    entity: str
    confidence: Confidence
    reason: Reason
    txid: str | None = None
    source: str | None = None
    heuristic: bool = False


@dataclass(frozen=True)
class Unresolved:
    """A recorded receipt or an override the engine couldn't apply, and why."""

    kind: Literal["received", "override"]
    outpoint: str
    entity: str
    why: Literal["not_owned", "not_identity"]


@dataclass(frozen=True)
class Doxx:
    tags: tuple[Tag, ...]
    unresolved: tuple[Unresolved, ...] = ()
    suppressed: tuple[Tag, ...] = ()  # tags the user's removals kept off


def doxx(
    txs: Sequence[Tx],
    identity: frozenset[str],
    received: Sequence[Received] = (),
    overrides: Sequence[Override] = (),
) -> Doxx:
    """Every doxx tag on the user's coins, by outpoint (txid, then vout) then entity."""
    _check(txs, identity, received, overrides)
    state = _State(txs, overrides)
    unresolved: list[Unresolved] = []
    seeds: list[tuple[Literal["received", "override"], str, str, Reason]] = [
        ("received", r.outpoint, r.entity, "received_from") for r in received
    ]
    seeds += [("override", o.outpoint, o.entity, "manual") for o in overrides if o.present]
    for kind, outpoint, entity, reason in seeds:
        if outpoint not in state.owned:
            unresolved.append(Unresolved(kind, outpoint, entity, "not_owned"))
        elif entity not in identity:
            unresolved.append(Unresolved(kind, outpoint, entity, "not_identity"))
        else:
            state.give(outpoint, entity, ("certain", reason, None, None, False))
    for t in sorted(txs, key=lambda t: t.txid):
        _paid_to(state, t, identity)
    clusters = _clusters(txs)
    while state.changed:
        state.changed = False
        for t in sorted(txs, key=lambda t: t.txid):
            _forward(state, t)
        _reuse(state)
        _cluster(state, clusters)
    return Doxx(
        _tags(state.tags),
        tuple(sorted(unresolved, key=lambda u: (u.kind, _key(u.outpoint), u.entity))),
        _tags(state.suppressed),
    )


type _Value = tuple[
    Confidence, Reason, str | None, str | None, bool
]  # confidence, reason, txid, source, heuristic


def _tags(tags: dict[str, dict[str, _Value]]) -> tuple[Tag, ...]:
    return tuple(
        Tag(outpoint, k, *tags[outpoint][k])
        for outpoint in sorted(tags, key=_key)
        for k in sorted(tags[outpoint])
    )


def _paid_to(state: _State, t: Tx, identity: frozenset[str]) -> None:
    """Paid to K: K sees every owned input (only inferred in a mixing tx) and every owned output."""
    paid = {o.entity for o in t.outputs if not o.owned and o.entity is not None and o.entity in identity}
    for k in sorted(paid):
        for i in t.inputs:
            if i.owned:
                state.give(
                    i.outpoint, k, ("inferred" if t.mixing else "certain", "paid_to", t.txid, None, False)
                )
        for o in t.outputs:
            if o.owned:
                state.give(o.outpoint, k, ("certain", "change_of", t.txid, None, False))


def _forward(state: _State, t: Tx) -> None:
    """Forward: the owned outputs inherit the owned inputs' sets, with their confidence; through a mixing
    tx only as inferred. A heuristic input tag stays heuristic."""
    inherited: dict[str, _Value] = {}  # entity -> the strongest input tag (its coin as the source)
    for i in sorted((i for i in t.inputs if i.owned), key=lambda i: _key(i.outpoint)):
        for k, value in state.tags.get(i.outpoint, {}).items():
            conf: Confidence = "inferred" if t.mixing else value[0]
            candidate: _Value = (conf, "change_of", t.txid, i.outpoint, value[4])
            if k not in inherited or _stronger(candidate, inherited[k]):
                inherited[k] = candidate
    for o in t.outputs:
        if o.owned:
            for k, value in sorted(inherited.items()):
                state.give(o.outpoint, k, value)


def _reuse(state: _State) -> None:
    """Address reuse: every coin at an address is doxxed to every entity another coin there holds, with
    that coin's confidence (from the strongest other holder; linear in the coins there)."""
    for coins in state.by_address.values():
        holders: dict[str, list[tuple[int, bool, tuple[str, int], str]]] = {}  # entity -> 2 best holders
        for c in coins:
            for k, value in state.tags.get(c, {}).items():
                mine = (-STRENGTH[value[0]], value[4], _key(c), c)
                holders[k] = sorted([*holders.get(k, []), mine])[:2]
        for k, best in sorted(holders.items()):
            for c in coins:
                other = next((h for h in best if h[3] != c), None)
                if other is not None:
                    conf: Confidence = "certain" if other[0] == -STRENGTH["certain"] else "inferred"
                    state.give(c, k, (conf, "address_reuse", None, other[3], other[1]))


def _cluster(state: _State, clusters: list[tuple[list[str], dict[str, str]]]) -> None:
    """Backward cluster: a tag at one address of a common-input cluster is inferred, as a heuristic, at
    all of them; the tag names the co-spend transaction that put the address in the cluster."""
    for members, joined_by in clusters:
        entities: dict[str, str] = {}  # entity -> a coin holding it
        for a in members:
            for c in sorted(state.by_address.get(a, ()), key=_key):
                for k in state.tags.get(c, {}):
                    entities.setdefault(k, c)
        for k, source in sorted(entities.items()):
            for a in members:
                for c in state.by_address.get(a, ()):
                    state.give(c, k, ("inferred", "cluster_backward", joined_by[a], source, True))


class _State:
    def __init__(self, txs: Sequence[Tx], overrides: Sequence[Override]) -> None:
        self.tags: dict[str, dict[str, _Value]] = {}
        self.suppressed: dict[str, dict[str, _Value]] = {}
        self.removed = {(o.outpoint, o.entity) for o in overrides if not o.present}
        self.owned: set[str] = set()
        self.by_address: dict[str, list[str]] = {}
        for t in txs:
            for c in (*t.inputs, *t.outputs):
                if c.owned and c.outpoint not in self.owned:
                    self.owned.add(c.outpoint)
                    self.by_address.setdefault(c.address, []).append(c.outpoint)
        self.changed = True

    def give(self, outpoint: str, entity: str, value: _Value) -> None:
        """Tag a coin, or strengthen its tag; never weaken one. A tag the user removed is kept apart."""
        if outpoint not in self.owned:
            return
        into = self.suppressed if (outpoint, entity) in self.removed else self.tags
        mine = into.setdefault(outpoint, {})
        held = mine.get(entity)
        if held is None or _stronger(value, held):
            mine[entity] = value
            self.changed = True


def _stronger(new: _Value, old: _Value) -> bool:
    """Stronger confidence, then not heuristic, then the earlier rule, lower txid and lower source."""

    def rank(v: _Value) -> tuple[int, int, int, str, str]:
        return (-STRENGTH[v[0]], int(v[4]), REASON_ORDER[v[1]], v[2] or "", v[3] or "")

    return rank(new) < rank(old)


def _clusters(txs: Sequence[Tx]) -> list[tuple[list[str], dict[str, str]]]:
    """The common-input clusters of the user's addresses (co-spent in a transaction that isn't mixing),
    each with the transaction that first joined each address to another (by txid order)."""
    parent: dict[str, str] = {}
    joined_by: dict[str, str] = {}

    def find(a: str) -> str:
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in sorted(txs, key=lambda t: t.txid):
        spent = sorted({i.address for i in t.inputs if i.owned})
        for a in spent:
            find(a)
        if not t.mixing and len(spent) > 1:
            for a in spent:
                joined_by.setdefault(a, t.txid)
            for a in spent[1:]:
                parent[find(a)] = find(spent[0])
    groups: dict[str, list[str]] = {}
    for a in sorted(parent):
        groups.setdefault(find(a), []).append(a)
    return [(g, {a: joined_by[a] for a in g}) for g in groups.values() if len(g) > 1]


def _key(outpoint: str) -> tuple[str, int]:
    txid, _, vout = outpoint.rpartition(":")
    return (txid, int(vout)) if vout.isdigit() else (outpoint, -1)


def _check(txs: object, identity: object, received: object, overrides: object) -> None:
    if not isinstance(identity, frozenset) or not all(_name(k) for k in identity):
        raise DoxxError("identity is a frozenset of entity names")
    if not isinstance(txs, Sequence) or not all(isinstance(t, Tx) for t in txs):
        raise DoxxError("txs is a sequence of Tx")
    _check_txs(txs)
    for r in _items(received, Received, "received"):
        if not _name(r.outpoint) or not _name(r.entity):
            raise DoxxError("a received coin names its outpoint and entity")
    pairs: dict[tuple[str, str], bool] = {}
    for o in _items(overrides, Override, "overrides"):
        if not _name(o.outpoint) or not _name(o.entity) or type(o.present) is not bool:
            raise DoxxError("an override names its outpoint and entity, and whether it is present")
        if pairs.setdefault((o.outpoint, o.entity), o.present) != o.present:
            raise DoxxError(f"override {o.outpoint!r}/{o.entity!r}: both added and removed")


def _check_txs(txs: Sequence[Tx]) -> None:
    seen: set[str] = set()
    coins: dict[str, tuple[str, bool, str | None]] = {}
    outputs: set[str] = set()
    for t in txs:
        if not _name(t.txid) or ":" in t.txid or t.txid in seen or type(t.mixing) is not bool:
            raise DoxxError(f"tx {t.txid!r}: a tx has a unique txid and a mixing flag")
        seen.add(t.txid)
        if not isinstance(t.inputs, tuple) or not isinstance(t.outputs, tuple):
            raise DoxxError(f"tx {t.txid!r}: its inputs and outputs are tuples")
        _check_coins(t.txid, (*t.inputs, *t.outputs))
        for o in t.outputs:
            if o.outpoint.rpartition(":")[0] != t.txid or o.outpoint in outputs:
                raise DoxxError(f"tx {t.txid!r}: an output's outpoint is its txid:index, once")
            outputs.add(o.outpoint)
        for c in (*t.inputs, *t.outputs):  # the same coin is the same coin wherever it appears
            if coins.setdefault(c.outpoint, (c.address, c.owned, c.entity)) != (c.address, c.owned, c.entity):
                raise DoxxError(
                    f"coin {c.outpoint!r}: its address, owner and entity differ between sightings"
                )


def _check_coins(txid: str, coins: Iterable[object]) -> None:
    for c in coins:
        if (
            not isinstance(c, Coin)
            or not _name(c.outpoint)
            or not _key(c.outpoint)[1] >= 0
            or not _name(c.address)
        ):
            raise DoxxError(f"tx {txid!r}: a coin is a Coin with its outpoint (txid:vout) and address")
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

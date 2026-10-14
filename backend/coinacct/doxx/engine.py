"""The doxx rules (ADR 0010; PLAN §5; T-504, T-505, T-511): pure, graph and tag values in, doxx tags out.

A coin's **doxx set** is the set of entities that know the user's identity (`identity`) and can link
the coin to them. Each tag has a confidence, `certain` or `inferred`, the reason it was given (PLAN
§2's `doxx_tag.reason`), the transaction and coin it came from, and whether it rests on a heuristic:

- **Paid to K** (`paid_to`, `change_of`): a transaction the user spends in that pays an output K owns
  doxxes every owned input (`paid_to`) and every owned output, the change (`change_of`), to K,
  certainly; in a transaction flagged `mixing`, the owned inputs only as inferred, since K can't tell
  which inputs are the user's. One the user spends nothing in (a withdrawal from K, someone's batch
  payout) links nothing by this rule.
- **Received from K** (`received_from`): a coin the user recorded as coming from K (a withdrawal, a
  salary), certainly. It comes from the recorded event, never from recognising K's addresses: a
  transaction spending a coin of K's that pays the user, with no such event, is only suggested
  (`Doxx.suggested`), for the user to record or dismiss (T-511).
- **Address reuse** (`address_reuse`): every coin ever paid to an address that holds a coin doxxed to
  K. The rule is certain (ADR 0010), so the coin takes its source's confidence: certain from a certain
  tag, inferred from an inferred one.
- **Forward** (`change_of`): a transaction's owned outputs inherit the union of its owned inputs'
  sets, with their confidence; through a `mixing` transaction only as inferred (ADR 0010).
- **Backward cluster** (`cluster_backward`): addresses co-spent in a transaction that isn't `mixing`
  form a cluster (the common-input heuristic); a K-doxxed coin at one of them doxxes the coins at the
  others to K, inferred, hop by hop: each tag names the co-spend of its hop and the coin it was spent
  with, so its transactions lead back to the K-doxxed coin. The cluster is the one the user's whole
  history shows (ADR 0010's "earlier" co-spends are an open question for the owner: a co-spend after
  the doxxing counts too).

A rule never makes a link more certain than the link it starts from: reading ADR 0010's "certain" for
reuse and forward as the rule's own confidence keeps a guess (a cluster, a mixing tx) from turning
into a certain link elsewhere. Whether ADR 0010 means that is a question for the owner. A tag that
rests on the cluster heuristic, directly or through other rules, is marked `heuristic`, so the user can
see it rests on a guess and confirm or reject it (T-504). Heuristics only suggest: the caller shows these
as "known links", never as "private" or "clean" (T-505). Chain-analysis heuristics the app doesn't
model are accepted risk R-3.

The rules run until nothing changes, transactions in spend order (a transaction after those whose
outputs it spends, then by txid), so a tag crosses a chain of spends in one pass. A tag only strengthens
(inferred to certain, or a heuristic tag to one that isn't), so they end. Of two equally strong tags the
earlier reason (manual, received_from, paid_to, change_of, cluster_backward, address_reuse) wins, and of
two with the same reason the first given stays; each rule takes its source from the strongest holder
(lowest coin first), so the result doesn't depend on the order of the input, and no tag names its own
coin as its source. Doxx sets never reach third parties' outputs. The user's overrides persist: one adds
a tag (certain, `manual`), which then propagates; one removes a tag, which no rule gives back. A
recorded receipt or an override the engine can't apply (its coin isn't one of the user's, its entity
doesn't know the user's identity) is returned as `unresolved`, and every tag the removals kept off, on
the coin named or on the coins it would have reached, as `suppressed`, so the caller can show both
(T-511: no certain link is lost without a word).
"""

from __future__ import annotations

import heapq
from collections import deque
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
    """A transaction's input or output: its outpoint ("txid:vout"), the address it pays, whether the user
    owns it, and the entity that owns it otherwise, if known. `address` is one canonical key per script
    for every coin (the caller's: the script hash, say); two forms of one script would split its reuse
    and its cluster. Every coin at an address has the same owner."""

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
class Suggestion:
    """A receipt the graph shows and no `Received` records: `txid` spends a coin of `entity`'s (an input
    the caller tagged) and pays the user's `outpoint`. Never a tag (ADR 0010: a receipt comes from a
    recorded event); the caller asks the user to record it or dismiss it (T-511)."""

    outpoint: str
    entity: str
    txid: str


@dataclass(frozen=True)
class Doxx:
    tags: tuple[Tag, ...]
    unresolved: tuple[Unresolved, ...] = ()
    suppressed: tuple[Tag, ...] = ()  # tags the user's removals kept off, there and downstream
    suggested: tuple[Suggestion, ...] = ()  # receipts from identity-knowing parties no event records


def doxx(
    txs: Sequence[Tx],
    identity: frozenset[str],
    received: Sequence[Received] = (),
    overrides: Sequence[Override] = (),
) -> Doxx:
    """Every doxx tag on the user's coins, by outpoint (txid, then vout) then entity."""
    _check(txs, identity, received, overrides)
    order = _spend_order(txs)
    owned = {c.outpoint for t in txs for c in (*t.inputs, *t.outputs) if c.owned}
    unresolved: set[Unresolved] = set()
    seeds: list[tuple[str, str, Reason]] = []
    facts: list[tuple[Literal["received", "override"], str, str, Reason | None]] = [
        ("received", r.outpoint, r.entity, "received_from") for r in received
    ]
    facts += [("override", o.outpoint, o.entity, "manual" if o.present else None) for o in overrides]
    for kind, outpoint, entity, reason in facts:
        if outpoint not in owned:
            unresolved.add(Unresolved(kind, outpoint, entity, "not_owned"))
        elif entity not in identity:
            unresolved.add(Unresolved(kind, outpoint, entity, "not_identity"))
        elif reason is not None:
            seeds.append((outpoint, entity, reason))
    removed = frozenset((o.outpoint, o.entity) for o in overrides if not o.present)
    tags = _fixpoint(order, identity, seeds, removed)
    suppressed: dict[str, dict[str, _Value]] = {}
    if removed:  # what the removals kept off: every tag the run without them has, and this one lacks
        for outpoint, held in _fixpoint(order, identity, seeds, frozenset()).items():
            for k, value in held.items():
                kept = tags.get(outpoint, {}).get(k)
                if kept is None or _stronger(value, kept):
                    suppressed.setdefault(outpoint, {})[k] = value
    recorded = {(r.outpoint, r.entity) for r in received}
    suggested = sorted(
        {
            Suggestion(o.outpoint, i.entity, t.txid)
            for t in order
            for i in t.inputs
            if not i.owned and i.entity in identity
            for o in t.outputs
            if o.owned and (o.outpoint, i.entity) not in recorded
        },
        key=lambda g: (_key(g.outpoint), g.entity, g.txid),
    )
    return Doxx(
        _tags(tags),
        tuple(sorted(unresolved, key=lambda u: (u.kind, _key(u.outpoint), u.entity, u.why))),
        _tags(suppressed),
        tuple(suggested),
    )


def _fixpoint(
    order: list[Tx],
    identity: frozenset[str],
    seeds: list[tuple[str, str, Reason]],
    removed: frozenset[tuple[str, str]],
) -> dict[str, dict[str, _Value]]:
    state = _State(order, removed)
    for outpoint, entity, reason in seeds:
        state.give(outpoint, entity, ("certain", reason, None, None, False))
    for t in order:
        _paid_to(state, t, identity)
    clusters = _clusters(order)
    while state.changed:
        state.changed = False
        for t in order:
            _forward(state, t)
        _reuse(state)
        _cluster(state, clusters)
    return state.tags


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
    """Paid to K: when the user spends in a transaction paying K, K sees every owned input (only
    inferred in a mixing tx) and every owned output, the change. A transaction the user spends nothing
    in (a withdrawal from K, someone's batch payout) links nothing here: a receipt is recorded."""
    if not any(i.owned for i in t.inputs):
        return
    paid = {o.entity for o in t.outputs if not o.owned and o.entity is not None and o.entity in identity}
    for k in sorted(paid):
        for i in t.inputs:
            if i.owned:
                conf: Confidence = "inferred" if t.mixing else "certain"
                state.give(i.outpoint, k, (conf, "paid_to", t.txid, None, False))
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
    that coin's confidence, from the strongest other holder (linear in the coins there). The tag names the
    transaction that created the source coin."""
    for coins in state.by_address.values():
        holders: dict[str, list[tuple[int, bool, tuple[str, int], str]]] = {}  # entity -> 2 best
        for c in coins:
            for k, value in state.tags.get(c, {}).items():
                mine = (-STRENGTH[value[0]], value[4], _key(c), c)
                holders[k] = sorted([*holders.get(k, []), mine])[:2]
        for k, best in sorted(holders.items()):
            for c in coins:
                other = next((h for h in best if h[3] != c), None)
                if other is not None:
                    conf: Confidence = "certain" if other[0] == -STRENGTH["certain"] else "inferred"
                    source = other[3]
                    state.give(c, k, (conf, "address_reuse", source.rpartition(":")[0], source, other[1]))


def _cluster(state: _State, clusters: list[_Cluster]) -> None:
    """Backward cluster: a tag at one address of a common-input cluster is inferred, as a heuristic, at
    the cluster's other addresses, hop by hop from the strongest holder's: each tag names the co-spend
    transaction of its hop and the coin it was co-spent with, which holds the tag the hop came from (the
    holder's other coins get it by reuse). So a tag's transactions lead back to the holder."""
    for members, edges in clusters:
        best: dict[str, tuple[int, bool, tuple[str, int], str]] = {}  # entity -> its best holder
        for a in members:
            for c in state.by_address.get(a, ()):
                for k, value in state.tags.get(c, {}).items():
                    mine = (-STRENGTH[value[0]], value[4], _key(c), c)
                    if k not in best or mine < best[k]:
                        best[k] = mine
        for k, holder in sorted(best.items()):
            root = state.address_of[holder[3]]
            reached = {root}
            queue = deque([root])
            while queue:
                here = queue.popleft()
                for txid, there, coin_here, _ in edges[here]:
                    if there in reached:
                        continue
                    reached.add(there)
                    queue.append(there)
                    for c in state.by_address[there]:
                        state.give(c, k, ("inferred", "cluster_backward", txid, coin_here, True))


class _State:
    def __init__(self, txs: Sequence[Tx], removed: frozenset[tuple[str, str]]) -> None:
        self.tags: dict[str, dict[str, _Value]] = {}
        self.removed = removed
        self.owned: set[str] = set()
        self.by_address: dict[str, list[str]] = {}
        self.address_of: dict[str, str] = {}
        for t in txs:
            for c in (*t.inputs, *t.outputs):
                if c.owned and c.outpoint not in self.owned:
                    self.owned.add(c.outpoint)
                    self.by_address.setdefault(c.address, []).append(c.outpoint)
                    self.address_of[c.outpoint] = c.address
        self.changed = True

    def give(self, outpoint: str, entity: str, value: _Value) -> None:
        """Tag a coin, or strengthen its tag; never weaken one, and never give one the user removed."""
        if outpoint not in self.owned or (outpoint, entity) in self.removed:
            return
        mine = self.tags.setdefault(outpoint, {})
        held = mine.get(entity)
        if held is None or _stronger(value, held):
            mine[entity] = value
            self.changed = True


def _stronger(new: _Value, old: _Value) -> bool:
    """Stronger confidence, then not heuristic, then the earlier rule; an equal tag doesn't replace."""

    def rank(v: _Value) -> tuple[int, int, int]:
        return (-STRENGTH[v[0]], int(v[4]), REASON_ORDER[v[1]])

    return rank(new) < rank(old)


type _Edge = tuple[str, str, str, str]  # txid, the other address, the coin here and the coin there
type _Cluster = tuple[list[str], dict[str, list[_Edge]]]


def _clusters(txs: Sequence[Tx]) -> list[_Cluster]:
    """The common-input clusters of the user's addresses (co-spent in a transaction that isn't mixing),
    with their co-spend edges: each transaction joins its lowest address to each other one it spends
    from, through the lowest coin it spends at each. Edges sort by txid, then address."""
    parent: dict[str, str] = {}
    edges: dict[str, list[_Edge]] = {}

    def find(a: str) -> str:
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in txs:
        if t.mixing:
            continue
        at: dict[str, str] = {}
        for i in sorted((i for i in t.inputs if i.owned), key=lambda i: _key(i.outpoint)):
            at.setdefault(i.address, i.outpoint)
        hub, *others = sorted(at) or [""]
        for a in others:
            edges.setdefault(hub, []).append((t.txid, a, at[hub], at[a]))
            edges.setdefault(a, []).append((t.txid, hub, at[a], at[hub]))
            parent[find(a)] = find(hub)
    for listed in edges.values():
        listed.sort()
    groups: dict[str, list[str]] = {}
    for a in sorted(parent):
        groups.setdefault(find(a), []).append(a)
    return [(g, {a: edges[a] for a in g}) for g in groups.values() if len(g) > 1]


def _key(outpoint: str) -> tuple[str, int]:
    """An outpoint's sort key: its txid, then its vout as a number (-1 if it isn't txid:vout)."""
    vout = _vout(outpoint)
    return (outpoint.rpartition(":")[0], vout) if vout is not None else (outpoint, -1)


def _vout(outpoint: str) -> int | None:
    """The vout of a canonical "txid:vout" (ASCII digits, no leading zero), else None."""
    txid, sep, vout = outpoint.rpartition(":")
    if not sep or not txid or not (vout.isascii() and vout.isdigit()) or (len(vout) > 1 and vout[0] == "0"):
        return None
    if len(vout) > 10:  # a vout is a 32-bit number; a longer one isn't, and int() has a digit limit
        return None
    return int(vout)


def _spend_order(txs: Sequence[Tx]) -> list[Tx]:
    """The transactions, each after those whose outputs it spends, then by txid."""
    by_id = {t.txid: t for t in txs}
    waiting = {t.txid: {i.outpoint.rpartition(":")[0] for i in t.inputs} & by_id.keys() for t in txs}
    spenders: dict[str, list[str]] = {}
    for txid, parents in waiting.items():
        for parent in parents:
            spenders.setdefault(parent, []).append(txid)
    ready = [txid for txid, parents in waiting.items() if not parents]
    heapq.heapify(ready)
    order: list[Tx] = []
    while ready:
        txid = heapq.heappop(ready)
        order.append(by_id[txid])
        for child in spenders.get(txid, ()):
            waiting[child].discard(txid)
            if not waiting[child]:
                heapq.heappush(ready, child)
    if len(order) != len(txs):
        raise DoxxError("the transactions spend each other's outputs in a cycle")
    return order


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
        for index, o in enumerate(t.outputs):
            if o.outpoint != f"{t.txid}:{index}":
                raise DoxxError(
                    f"tx {t.txid!r}: its outputs are every output, each at its index (txid:index)"
                )
            outputs.add(o.outpoint)
        for c in (*t.inputs, *t.outputs):  # the same coin is the same coin wherever it appears
            if coins.setdefault(c.outpoint, (c.address, c.owned, c.entity)) != (c.address, c.owned, c.entity):
                raise DoxxError(
                    f"coin {c.outpoint!r}: its address, owner and entity differ between sightings"
                )
    _check_owners(txs)
    spent: set[str] = set()
    for t in txs:
        for i in t.inputs:
            if i.outpoint in spent:
                raise DoxxError(f"coin {i.outpoint!r} is spent by two transactions")
            spent.add(i.outpoint)
            if i.outpoint.rpartition(":")[0] in seen and i.outpoint not in outputs:
                raise DoxxError(f"tx {t.txid!r}: input {i.outpoint!r} names an output its transaction lacks")


def _check_coins(txid: str, coins: Iterable[object]) -> None:
    for c in coins:
        if (
            not isinstance(c, Coin)
            or not _name(c.outpoint)
            or _vout(c.outpoint) is None
            or not _name(c.address)
        ):
            raise DoxxError(f"tx {txid!r}: a coin is a Coin with its outpoint (txid:vout) and address")
        if type(c.owned) is not bool or (c.entity is not None and not _name(c.entity)):
            raise DoxxError(f"tx {txid!r}: a coin says whether it is owned, and its entity is a name or None")
        if c.owned and c.entity is not None:
            raise DoxxError(f"tx {txid!r}: a coin of the user's has no other entity")


def _check_owners(txs: Sequence[Tx]) -> None:
    owners: dict[str, tuple[bool, str | None]] = {}
    for c in _all(txs):
        if owners.setdefault(c.address, (c.owned, c.entity)) != (c.owned, c.entity):
            raise DoxxError(f"address {c.address!r}: its coins name different owners")


def _all(txs: Sequence[Tx]) -> Iterable[Coin]:
    for t in txs:
        yield from t.inputs
        yield from t.outputs


def _items[T](values: object, kind: type[T], name: str) -> list[T]:
    if not isinstance(values, Sequence) or not all(isinstance(v, kind) for v in values):
        raise DoxxError(f"{name} is a sequence of {kind.__name__}")
    return list(values)


def _name(value: object) -> bool:
    return isinstance(value, str) and value != ""

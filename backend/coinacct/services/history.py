"""Address, UTXO and transaction history from the chain cache (PLAN §3: "address/UTXO/tx history
views"; M2).

Everything here is read from what the chain jobs have cached (`storage.chain_cache`): the confirmed
receive and spend events of each script. Nothing calls the node, so the views work offline too
(architecture §3). Unconfirmed (mempool) activity isn't included: it is ephemeral, and shown
elsewhere (T-506).

**What a balance is as of** (T-207, T-210):
- `as_of` is the last tip a sync finished (`chain_state.last_tip`). Events above it, written while a
  catch-up is under way (`catching_up`), are left out, so every figure is as of `as_of` exactly.
- Each address also says how far its own history has been scanned, `scanned_to`: the highest stop
  height among the coverage of the scan subjects that hold its script, from its start height
  (`services.imports.subjects`). None means it hasn't been scanned yet, so its zero balance is
  "unknown", not "empty". It is complete when `scanned_to` reaches `as_of`.
- A script's UTXOs are its receives that none of its spends consumed. If two receives share an
  outpoint (the duplicate coinbase txids before BIP30, T-208), the later one counts, and the earlier
  one, unspendable, counts nowhere.

A UTXO is `complete` when its address is scanned to `as_of`; otherwise a spend of it may not have
been found yet, and the UI says so.

**Whose:** `addresses()` lists every address in the user DB, with its owner, so the UI can separate
the user's own from those tagged to others; `utxos()` lists only the user's own (an address in one of
their tax accounts), so a total of them is never someone else's coins.

Reads use a reader of their own, in one snapshot (architecture §3), opened and closed per call; a
busy or unusable DB is `imports.Busy` or a fixed `ImportRefused`, as for the import routes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from coinacct.domain.chain import Outpoint
from coinacct.services.imports import db_errors, descriptor_subject, subjects
from coinacct.storage import accounts, chain_cache
from coinacct.storage.chain_cache import Activity, reference_tip, scan_target
from coinacct.storage.chain_state import Tip, last_tip
from coinacct.storage.db import Connection


@dataclass(frozen=True, slots=True)
class Utxo:
    txid: str
    vout: int
    sats: int
    script_hex: str
    address: str | None
    height: int
    complete: bool  # its address's history is scanned to `as_of`: no spend of it can be missing


@dataclass(frozen=True, slots=True)
class AddressSummary:
    script_hex: str
    address: str | None
    entity_id: int
    tax_account_id: int | None
    label: str
    balance: int  # sats in its unspent outputs, as of `as_of`
    utxos: int
    received: int  # sats ever received, as of `as_of`
    transactions: int  # distinct transactions that paid or spent it
    last_height: int | None  # the block of its latest activity
    scanned_to: int | None  # how far its history has been scanned; None: not yet


@dataclass(frozen=True, slots=True)
class Event:
    kind: str  # "receive" or "spend"
    txid: str
    n: int  # the output index (receive) or the input index (spend)
    sats: int
    height: int
    blockhash: str
    prevout: Outpoint | None  # for a spend, the output it spent


@dataclass(frozen=True, slots=True)
class Addresses:
    as_of: Tip | None  # the last tip a sync finished; None before the first one
    catching_up: bool  # a sync towards a newer tip is under way
    addresses: tuple[AddressSummary, ...]


def _settled(events: Iterable[Activity], as_of: Tip | None) -> list[Activity]:
    """The events up to `as_of` (none before a sync has finished)."""
    return [] if as_of is None else [e for e in events if e.height <= as_of.height]


def _receives(events: Iterable[Activity]) -> dict[Outpoint, Activity]:
    """Each outpoint's receive; of two with the same outpoint (pre-BIP30 duplicate coinbases, T-208),
    the later, the earlier being unspendable."""
    found: dict[Outpoint, Activity] = {}
    for e in events:
        if e.kind == "receive":
            outpoint = Outpoint(e.txid, e.n)
            if outpoint not in found or e.height > found[outpoint].height:
                found[outpoint] = e
    return found


def _unspent(events: list[Activity]) -> dict[Outpoint, Activity]:
    """The receives no spend in `events` consumed. Spends of one script name its own outputs."""
    spent = {e.prevout for e in events if e.kind == "spend"}
    return {o: e for o, e in _receives(events).items() if o not in spent}


def _scanned_to(conn: Connection) -> dict[str, int]:
    """Each script's scanned height: the highest stop height among the subjects that hold it, counting
    a subject only if its coverage starts at or before the script's own start height."""
    starts = {a.script_hex: a.start_height for a in accounts.addresses(conn)}
    holders: dict[str, list[str]] = {}
    for d in accounts.descriptors(conn):
        for _, script in accounts.descriptor_scripts(conn, d.id):
            holders.setdefault(script, []).append(descriptor_subject(d))
    for scan in subjects(conn):
        if scan.subject.startswith("addrs:"):
            for obj in scan.scanobjects:
                holders.setdefault(str(obj)[len("raw(") : -1], []).append(scan.subject)
    found: dict[str, int] = {}
    for script, names in holders.items():
        for name in names:
            covered = chain_cache.coverage(conn, name)
            if covered is not None and covered.start_height <= starts.get(script, 0):
                found[script] = max(found.get(script, -1), covered.stop_height)
    return found


class History:
    """What the API's history routes call; reads only."""

    def __init__(self, open_reader: Callable[[], Connection]) -> None:
        self._open_reader = open_reader

    def _read[T](self, read: Callable[[Connection], T]) -> T:
        with db_errors():
            reader = self._open_reader()
            try:
                reader.execute("BEGIN")  # one snapshot (a deferred transaction: no write lock under WAL)
                try:
                    return read(reader)
                finally:
                    if reader.in_transaction:
                        reader.execute("COMMIT")
            finally:
                reader.close()

    def addresses(self, tax_account_id: int | None = None) -> Addresses:
        """Every address in the user DB (or one tax account's), with what the cache knows of it."""

        def read(conn: Connection) -> Addresses:
            as_of = last_tip(conn)
            scanned = _scanned_to(conn)
            summaries: list[AddressSummary] = []
            for a in accounts.addresses(conn):
                if tax_account_id is not None and a.tax_account_id != tax_account_id:
                    continue
                events = _settled(chain_cache.activity_for(conn, a.script_hex), as_of)
                unspent = _unspent(events)
                summaries.append(
                    AddressSummary(
                        a.script_hex,
                        a.text,
                        a.entity_id,
                        a.tax_account_id,
                        a.label,
                        sum(e.sats for e in unspent.values()),
                        len(unspent),
                        sum(e.sats for e in _receives(events).values()),
                        len({(e.txid, e.blockhash) for e in events}),  # a tx is (txid, block): T-208
                        max((e.height for e in events), default=None),
                        scanned.get(a.script_hex),
                    )
                )
            catching_up = scan_target(conn) is not None and reference_tip(conn) != as_of
            return Addresses(as_of, catching_up, tuple(summaries))

        return self._read(read)

    def utxos(self, tax_account_id: int | None = None) -> list[Utxo]:
        """The unspent outputs of the user's own addresses (or one tax account's), oldest first, as of
        the last finished sync."""

        def read(conn: Connection) -> list[Utxo]:
            as_of = last_tip(conn)
            scanned = _scanned_to(conn)
            found: list[Utxo] = []
            for a in accounts.addresses(conn):
                if a.tax_account_id is None or (
                    tax_account_id is not None and a.tax_account_id != tax_account_id
                ):
                    continue  # not the user's own (another owner's), or another account
                events = _settled(chain_cache.activity_for(conn, a.script_hex), as_of)
                to = scanned.get(a.script_hex)
                complete = as_of is not None and to is not None and to >= as_of.height
                for outpoint, e in _unspent(events).items():
                    found.append(
                        Utxo(outpoint.txid, outpoint.vout, e.sats, a.script_hex, a.text, e.height, complete)
                    )
            return sorted(found, key=lambda u: (u.height, u.txid, u.vout))

        return self._read(read)

    def events(self, script_hex: str) -> list[Event] | None:
        """One address's receive and spend events by height, as of the last finished sync; None if it
        isn't in the user DB."""

        def read(conn: Connection) -> list[Event] | None:
            if not any(a.script_hex == script_hex for a in accounts.addresses(conn)):
                return None
            return [
                Event(e.kind, e.txid, e.n, e.sats, e.height, e.blockhash, e.prevout)
                for e in _settled(chain_cache.activity_for(conn, script_hex), last_tip(conn))
            ]

        return self._read(read)

"""Address, UTXO and transaction history from the chain cache (PLAN §3: "address/UTXO/tx history
views"; M2).

Everything here is read from what the chain jobs have cached (`storage.chain_cache`): the confirmed
receive and spend events of each owned script, as of the last processed tip (`as_of`). Nothing calls
the node, so the views work offline too (architecture §3). A script's UTXOs are its receives no
spend has consumed; its balance is their sum. Unconfirmed (mempool) activity isn't included: it is
ephemeral and shown elsewhere (T-506).

Reads use a reader of their own, in one snapshot (architecture §3), opened and closed per call.
Coverage matters: a script whose scan hasn't reached the tip yet shows only what was found so far,
so each summary carries the height the cache was complete to, `as_of`, for the UI to say so.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from coinacct.domain.chain import Outpoint
from coinacct.storage import accounts, chain_cache
from coinacct.storage.chain_cache import Activity
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


@dataclass(frozen=True, slots=True)
class AddressSummary:
    script_hex: str
    address: str | None
    entity_id: int
    tax_account_id: int | None
    label: str
    balance: int  # sats in its unspent outputs
    utxos: int
    received: int  # sats ever received
    transactions: int  # distinct transactions that paid or spent it
    last_height: int | None  # the block of its latest activity


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
    as_of: Tip | None  # the last tip the chain jobs processed; None before the first sync
    addresses: tuple[AddressSummary, ...]


def _unspent(events: Iterable[Activity]) -> dict[Outpoint, Activity]:
    """The receives no spend in `events` consumed. Spends of one script name its own outputs."""
    events = list(events)
    spent = {e.prevout for e in events if e.kind == "spend"}
    return {
        Outpoint(e.txid, e.n): e for e in events if e.kind == "receive" and Outpoint(e.txid, e.n) not in spent
    }


def _summary(address: accounts.Address, events: list[Activity]) -> AddressSummary:
    unspent = _unspent(events)
    return AddressSummary(
        address.script_hex,
        address.text,
        address.entity_id,
        address.tax_account_id,
        address.label,
        sum(e.sats for e in unspent.values()),
        len(unspent),
        sum(e.sats for e in events if e.kind == "receive"),
        len({e.txid for e in events}),
        max((e.height for e in events), default=None),
    )


class History:
    """What the API's history routes call; reads only."""

    def __init__(self, open_reader: Callable[[], Connection]) -> None:
        self._open_reader = open_reader

    def _read[T](self, read: Callable[[Connection], T]) -> T:
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
            chosen = [
                a
                for a in accounts.addresses(conn)
                if tax_account_id is None or a.tax_account_id == tax_account_id
            ]
            summaries = tuple(_summary(a, chain_cache.activity_for(conn, a.script_hex)) for a in chosen)
            return Addresses(last_tip(conn), summaries)

        return self._read(read)

    def utxos(self, tax_account_id: int | None = None) -> list[Utxo]:
        """The unspent outputs of every owned address (or one tax account's), oldest first."""

        def read(conn: Connection) -> list[Utxo]:
            found: list[Utxo] = []
            for a in accounts.addresses(conn):
                if tax_account_id is not None and a.tax_account_id != tax_account_id:
                    continue
                for outpoint, e in _unspent(chain_cache.activity_for(conn, a.script_hex)).items():
                    found.append(Utxo(outpoint.txid, outpoint.vout, e.sats, a.script_hex, a.text, e.height))
            return sorted(found, key=lambda u: (u.height, u.txid, u.vout))

        return self._read(read)

    def events(self, script_hex: str) -> list[Event] | None:
        """The receive and spend events of one address, by height; None if it isn't in the user DB."""

        def read(conn: Connection) -> list[Event] | None:
            if not any(a.script_hex == script_hex for a in accounts.addresses(conn)):
                return None
            return [
                Event(e.kind, e.txid, e.n, e.sats, e.height, e.blockhash, e.prevout)
                for e in chain_cache.activity_for(conn, script_hex)
            ]

        return self._read(read)

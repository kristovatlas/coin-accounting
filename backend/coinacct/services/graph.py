"""Graph expansion: a transaction's inputs and outputs, and what spent an output (PLAN §4, M3;
architecture §8.3; THREAT_MODEL T-205, T-207, T-208, T-210).

The SPA's graph grows one step at a time: from an output forward to the transaction that spent it,
or from a transaction backward through its inputs to the transactions that created them. Each step
is one call here.

- **A transaction** (`tx`): from the chain cache when it holds it; otherwise, online, from the node
  (`chain.txs.fetch_tx`, with the block hash for a confirmed transaction: BIP30, T-208). An
  unconfirmed transaction gets its spent outputs from its parents (`fill_prevouts`) and is never
  cached: the mempool is ephemeral (T-207).
- **What spent an output** (`spender`): an unspendable output (OP_RETURN) is terminal without asking
  the node. Otherwise the cache answers when it holds a confirmed spend, or an "unspent" snapshot at
  the current reference tip; online, the node does (`chain.spenders`), and a mempool spend is shown
  as unconfirmed, never cached.
- **Caching** only ever happens at the app's reference tip (`storage.chain_cache`), and only when
  the node's tip didn't move while it was asked: a block's height is then that tip's height minus
  the transaction's confirmations, plus one. Otherwise the answer is shown but not cached; a later
  call asks again. The cache never holds what the scan protocol didn't vouch for (T-207, T-210).
- **Whose:** every input and output says which entity and tax account its script belongs to, if the
  user DB knows it, so the graph can colour the user's own coins.

Offline, only what the cache holds can be shown; anything else is `OfflineError` (T-203). Errors map
as for the import routes: a busy DB is `Busy`, a node that went away is `OfflineError`, and node
text never reaches a message (T-403).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from coinacct.chain import reorg, spenders, txs
from coinacct.chain.spenders import SpendState
from coinacct.chain.txs import ChainRpc, NodeError
from coinacct.domain.chain import Outpoint, Tx, TxOut
from coinacct.services.imports import ImportRefused, OfflineError, db_errors
from coinacct.storage import accounts, chain_cache
from coinacct.storage.chain_cache import SpentBy, StaleTipError, reference_tip
from coinacct.storage.chain_state import Tip
from coinacct.storage.db import Connection


class NotFound(LookupError):
    """No such transaction or output, or not in the active chain (the message never repeats ids)."""


@dataclass(frozen=True, slots=True)
class Owner:
    entity_id: int
    tax_account_id: int | None
    label: str


@dataclass(frozen=True, slots=True)
class OutputView:
    n: int
    sats: int
    script_hex: str
    address: str | None
    unspendable: bool
    owner: Owner | None


@dataclass(frozen=True, slots=True)
class InputView:
    prevout: Outpoint | None  # None for a coinbase
    sats: int | None  # the spent output's value, when known
    script_hex: str | None
    address: str | None
    owner: Owner | None


@dataclass(frozen=True, slots=True)
class TxView:
    txid: str
    blockhash: str | None  # None while unconfirmed
    confirmations: int
    block_time: int | None
    inputs: tuple[InputView, ...]
    outputs: tuple[OutputView, ...]


@dataclass(frozen=True, slots=True)
class SpendView:
    state: str  # "unspendable", "unspent", "spent_unconfirmed" or "spent"
    spending_txid: str | None
    blockhash: str | None  # the spender's block, for "spent"
    as_of: Tip | None  # for "unspent": the tip it was true at (a snapshot, T-207)


@contextmanager
def _node_errors() -> Iterator[None]:
    """The node's refusals as the API's: a node that went away is offline; a transaction or block it
    doesn't have is `NotFound`; a reply it couldn't read, or one over a budget, is refused. Node text
    never reaches a message (T-403)."""
    try:
        yield
    except (txs.TxNotFoundError, txs.StaleBlockError):
        raise NotFound("the node has no such transaction in its active chain") from None
    except txs.BudgetExceededError:
        raise ImportRefused("this transaction is too large to expand here (T-205)") from None
    except (txs.MalformedTxError, reorg.MalformedHeaderError):
        raise ImportRefused("the node's answer couldn't be read") from None
    except (reorg.NodeSyncingError, reorg.TipMovedError):
        raise OfflineError("the node is busy syncing its chain; try again in a moment") from None
    except NodeError:
        raise OfflineError("the node didn't answer; check that it's running and try again") from None


class Graph:
    """What the API's graph routes call. `rpc` is None offline: then only the cache answers."""

    def __init__(
        self, writer: Connection, open_reader: Callable[[], Connection], rpc: ChainRpc | None
    ) -> None:
        self._writer, self._open_reader, self._rpc = writer, open_reader, rpc

    def _read[T](self, read: Callable[[Connection], T]) -> T:
        with db_errors():
            reader = self._open_reader()
            try:
                reader.execute("BEGIN")  # one snapshot
                try:
                    return read(reader)
                finally:
                    if reader.in_transaction:
                        reader.execute("COMMIT")
            finally:
                reader.close()

    def _online(self) -> ChainRpc:
        if self._rpc is None:
            raise OfflineError("this part of the graph isn't cached, and the app is offline (T-203)")
        return self._rpc

    def _cache[T](self, write: Callable[[Connection], T]) -> T | None:
        """A cache write at the tip it was computed against; None if the tip has moved since (the
        answer is still shown, just not cached)."""
        with db_errors():
            try:
                return write(self._writer)
            except StaleTipError:
                return None

    def _fetch(self, txid: str, blockhash: str | None) -> Tx:
        """The transaction from the cache, or from the node (and cached if confirmed, at a still tip)."""
        if blockhash is not None:
            cached = self._read(lambda r: chain_cache.get_tx(r, txid, blockhash))
            if cached is not None:
                return cached
        rpc = self._online()
        with _node_errors():
            before = reorg.node_tip(rpc)
            tx = txs.fetch_tx(rpc, txid, blockhash)
            if not tx.confirmed:
                return txs.fill_prevouts(rpc, tx)  # the mempool is never cached (T-207)
            after = reorg.node_tip(rpc)
        if before == after and tx.blockhash is not None:
            height = after.height - tx.confirmations + 1
            self._cache(lambda w: chain_cache.put_tx(w, tx, height, after))
        return tx

    def tx(self, txid: str, blockhash: str | None) -> TxView:
        """One transaction, with its inputs' and outputs' owners."""
        found = self._fetch(txid, blockhash)
        owners = self._read(_owners)
        return _view(found, owners)

    def spender(self, txid: str, blockhash: str | None, n: int) -> SpendView:
        """What spent output `n` of the transaction: from the cache, or from the node."""
        found = self._fetch(txid, blockhash)
        if not 0 <= n < len(found.outputs):
            raise NotFound("the transaction has no such output")
        outpoint = Outpoint(found.txid, n)
        if found.outputs[n].unspendable:
            return SpendView(SpendState.UNSPENDABLE.value, None, None, None)

        def cached(r: Connection) -> SpendView | None:
            spent = chain_cache.get_spender(r, outpoint)
            if spent is not None:
                return SpendView(SpendState.SPENT.value, spent.txid, spent.blockhash, None)
            at, tip = chain_cache.unspent_at(r, outpoint), reference_tip(r)
            if at is not None and at == tip:
                return SpendView(SpendState.UNSPENT.value, None, None, at)
            return None

        known = self._read(cached)
        if known is not None:
            return known
        rpc = self._online()
        with _node_errors():
            before = reorg.node_tip(rpc)
            spend = spenders.spend_of(rpc, found, n)
            spending = (
                txs.fetch_tx(rpc, spend.spending_txid, spend.blockhash)
                if spend.state is SpendState.SPENT and spend.spending_txid is not None
                else None
            )
            after = reorg.node_tip(rpc)
        still = before == after
        if spend.state is SpendState.SPENT and spending is not None and spend.blockhash is not None:
            if still:
                height = after.height - spending.confirmations + 1
                by = SpentBy(spending.txid, spend.blockhash, height)
                self._cache(lambda w: chain_cache.put_spender(w, outpoint, by, after))
            return SpendView(spend.state.value, spend.spending_txid, spend.blockhash, None)
        if spend.state is SpendState.UNSPENT:
            if still:
                self._cache(lambda w: chain_cache.put_unspent(w, outpoint, after))
            return SpendView(spend.state.value, None, None, after)
        return SpendView(spend.state.value, spend.spending_txid, spend.blockhash, None)


def _owners(conn: Connection) -> dict[str, Owner]:
    return {a.script_hex: Owner(a.entity_id, a.tax_account_id, a.label) for a in accounts.addresses(conn)}


def _output(n: int, out: TxOut, owners: dict[str, Owner]) -> OutputView:
    return OutputView(n, out.sats, out.script_hex, out.address, out.unspendable, owners.get(out.script_hex))


def _view(tx: Tx, owners: dict[str, Owner]) -> TxView:
    inputs = tuple(
        InputView(
            i.prevout,
            None if i.spent is None else i.spent.sats,
            None if i.spent is None else i.spent.script_hex,
            None if i.spent is None else i.spent.address,
            None if i.spent is None else owners.get(i.spent.script_hex),
        )
        for i in tx.inputs
    )
    outputs = tuple(_output(n, o, owners) for n, o in enumerate(tx.outputs))
    return TxView(tx.txid, tx.blockhash, tx.confirmations, tx.block_time, inputs, outputs)

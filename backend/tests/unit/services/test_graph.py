"""Graph expansion (PLAN §4, M3; architecture §8.3): one transaction, and what spent one output, from
the chain cache or the node. The node side is the `chain/` functions the service calls, replaced here
by fakes that return domain objects; `integration/services/test_graph_regtest.py` runs it against a
real node. Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import reorg, spenders, txs
from coinacct.chain.spenders import Spend, SpendState
from coinacct.domain.chain import Outpoint, Tx, TxIn, TxOut
from coinacct.services.graph import TIP_RETRIES, Graph, NotFound
from coinacct.services.imports import ImportRefused, OfflineError
from coinacct.storage import accounts as ac
from coinacct.storage import chain_cache as cc
from coinacct.storage import tags
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import Tip, record_chain, set_tip
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import open_db, open_reader

TIP = Tip("ee" * 32, 500)
OTHER_TIP = Tip("dd" * 32, 501)
EARLIER_TIP = Tip("cc" * 32, 499)  # the node's tip just before it moved on to TIP
MINE = "0014" + "aa" * 20
THEIRS = "0014" + "bb" * 20
OP_RETURN = "6a04deadbeef"


def h(n: int) -> str:
    return f"{n:064x}"


def tx(
    n: int,
    *,
    block: int | None = 490,
    confirmations: int = 11,
    outputs: tuple[TxOut, ...] = (),
    spends: Outpoint | None = None,
) -> Tx:
    """Transaction `n`, spending `spends` (by default output 0 of transaction n+1000, paid to THEIRS),
    confirmed in block `block` (its hash is h(block)) unless `block` is None."""
    spent = TxOut(70_000, THEIRS, "witness_v0_keyhash")
    outs = outputs or (
        TxOut(50_000, MINE, "witness_v0_keyhash", "bcrt1qmine"),
        TxOut(19_000, THEIRS, "witness_v0_keyhash"),
    )
    return Tx(
        h(n),
        None if block is None else h(block),
        0 if block is None else confirmations,
        None if block is None else 1_700_000_000,
        (TxIn(spends or Outpoint(h(n + 1000), 0), 0xFFFFFFFD, spent),),
        outs,
    )


class FakeChain:
    """Stands in for `chain.reorg.node_tip`, `chain.txs.fetch_tx`/`fill_prevouts` and
    `chain.spenders.spend_of`, and records each call."""

    def __init__(self) -> None:
        self.tips: list[Tip] = [TIP]
        self.txs: dict[tuple[str, str | None], Tx] = {}
        self.spends: dict[Outpoint, Spend] = {}
        self.calls: list[str] = []
        self.raise_on_fetch: Exception | None = None
        self.stale: set[str] = set()  # txids whose block has left the active chain
        self.missing: set[str] = set()  # txids the node doesn't have

    def node_tip(self, rpc: Any) -> Tip:
        self.calls.append("node_tip")
        return self.tips.pop(0) if len(self.tips) > 1 else self.tips[0]

    def fetch_tx(self, rpc: Any, txid: str, blockhash: str | None = None) -> Tx:
        self.calls.append(f"fetch_tx {txid[-4:]}")
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        if txid in self.stale:
            raise txs.StaleBlockError("that block is no longer in the active chain")
        if txid in self.missing:
            raise txs.TxNotFoundError("the node doesn't have this transaction")
        return self.txs[(txid, blockhash)]

    def fill_prevouts(self, rpc: Any, t: Tx) -> Tx:
        self.calls.append("fill_prevouts")
        return t

    def spend_of(self, rpc: Any, t: Tx, n: int) -> Spend:
        self.calls.append(f"spend_of {n}")
        return self.spends[Outpoint(t.txid, n)]


@pytest.fixture
def data(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


@pytest.fixture
def conn(data: DataDir) -> Iterator[sqlite3.Connection]:
    c = open_db(data)
    record_chain(c, "regtest")
    set_tip(c, TIP)
    wallet = ac.add_tax_account(c, "Cold storage", "self_custody")
    ac.add_addresses(
        c, [(MINE, "bcrt1qmine")], entity_id=ME, tax_account_id=wallet, source="manual", label="savings"
    )
    yield c
    c.close()


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> FakeChain:
    fake = FakeChain()
    monkeypatch.setattr(reorg, "node_tip", fake.node_tip)
    monkeypatch.setattr(txs, "fetch_tx", fake.fetch_tx)
    monkeypatch.setattr(txs, "fill_prevouts", fake.fill_prevouts)
    monkeypatch.setattr(spenders, "spend_of", fake.spend_of)
    return fake


def online(data: DataDir, conn: sqlite3.Connection) -> Graph:
    return Graph(conn, lambda: open_reader(data), rpc=object())  # type: ignore[arg-type]


def offline(data: DataDir, conn: sqlite3.Connection) -> Graph:
    return Graph(conn, lambda: open_reader(data), rpc=None)


# --- one transaction -------------------------------------------------------------------------------------


def test_a_confirmed_transaction_is_fetched_cached_and_owned(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    chain.txs[(h(1), h(490))] = tx(1)
    view = online(data, conn).tx(h(1), h(490))
    assert view.txid == h(1) and view.blockhash == h(490) and view.confirmations == 11
    mine, theirs = view.outputs
    assert mine.owner is not None and mine.owner.entity_id == ME and mine.owner.label == "savings"
    assert theirs.owner is None
    assert view.inputs[0].prevout == Outpoint(h(1001), 0) and view.inputs[0].sats == 70_000
    cached = cc.get_tx(conn, h(1), h(490))  # height = 500 - 11 + 1 = 490
    assert cached is not None and cached.confirmations == 11


def test_a_transaction_carries_the_users_mixing_flag(data: DataDir, conn: sqlite3.Connection) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    assert offline(data, conn).tx(h(1), h(490)).mixing is None  # nobody has set one
    tags.set_mixing(conn, h(1), True, at="2026-10-08T12:00:00+00:00")
    assert offline(data, conn).tx(h(1), h(490)).mixing is True
    tags.set_mixing(conn, h(1), False, at="2026-10-08T12:05:00+00:00")
    assert offline(data, conn).tx(h(1), h(490)).mixing is False


def test_a_cached_transaction_needs_no_node_even_offline(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    view = offline(data, conn).tx(h(1), h(490))
    assert view.outputs[0].owner is not None
    assert chain.calls == []


def test_offline_an_uncached_transaction_is_refused_before_any_call_t203(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    with pytest.raises(OfflineError):
        offline(data, conn).tx(h(1), h(490))
    assert chain.calls == []


def test_a_tip_that_moved_during_the_fetch_isnt_cached_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    # The node moved from 499 onto the app's tip (500) while the transaction was read: its 10
    # confirmations count from 499, so "500 - 10 + 1" would cache it a block too high.
    chain.tips = [EARLIER_TIP, TIP]
    chain.txs[(h(1), h(490))] = tx(1, confirmations=10)
    assert online(data, conn).tx(h(1), h(490)).txid == h(1)
    assert cc.get_tx(conn, h(1), h(490)) is None


def test_a_node_ahead_of_the_apps_tip_isnt_cached_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    chain.tips = [OTHER_TIP]  # the node is at 501; the cache's reference tip is still 500
    chain.txs[(h(1), h(490))] = tx(1, confirmations=12)
    assert online(data, conn).tx(h(1), h(490)).txid == h(1)
    assert cc.get_tx(conn, h(1), h(490)) is None


def test_an_unconfirmed_transaction_gets_its_prevouts_and_is_never_cached_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    chain.txs[(h(1), None)] = tx(1, block=None)
    view = online(data, conn).tx(h(1), None)
    assert view.blockhash is None and view.confirmations == 0
    assert "fill_prevouts" in chain.calls
    assert conn.execute("SELECT COUNT(*) FROM tx_cache").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (txs.TxNotFoundError("x"), NotFound),
        (txs.StaleBlockError("x"), NotFound),
        (txs.BudgetExceededError("x"), ImportRefused),
        (txs.MalformedTxError("x"), ImportRefused),
        (txs.NodeError("node text that names " + h(9)), OfflineError),
    ],
)
def test_node_failures_map_without_repeating_node_text_t403(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain, error: Exception, raised: type[Exception]
) -> None:
    chain.raise_on_fetch = error
    with pytest.raises(raised) as caught:
        online(data, conn).tx(h(1), h(490))
    assert h(9) not in str(caught.value) and "node text" not in str(caught.value)


# --- what spent an output --------------------------------------------------------------------------------


def test_an_unspendable_output_is_terminal_without_asking_the_node(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1, outputs=(TxOut(0, OP_RETURN, "nulldata"),)), 490, TIP)
    spend = online(data, conn).spender(h(1), h(490), 0)
    assert spend.state == "unspendable"
    assert chain.calls == []


def test_an_output_the_transaction_doesnt_have_is_not_found(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    with pytest.raises(NotFound):
        online(data, conn).spender(h(1), h(490), 2)


def test_a_confirmed_spend_is_cached_with_its_block_height(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT, h(2), h(495))
    chain.txs[(h(2), h(495))] = tx(2, block=495, confirmations=6, spends=Outpoint(h(1), 0))
    spend = online(data, conn).spender(h(1), h(490), 0)
    assert (spend.state, spend.spending_txid, spend.blockhash) == ("spent", h(2), h(495))
    assert cc.get_spender(conn, Outpoint(h(1), 0)) == cc.SpentBy(h(2), h(495), 495)
    chain.calls.clear()  # now from the cache, even offline
    assert offline(data, conn).spender(h(1), h(490), 0).spending_txid == h(2)
    assert chain.calls == []


def test_unspent_is_a_snapshot_at_the_tip_and_stale_once_it_moves_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.UNSPENT)
    spend = online(data, conn).spender(h(1), h(490), 0)
    assert spend.state == "unspent" and spend.as_of == TIP
    assert cc.unspent_at(conn, Outpoint(h(1), 0)) == TIP
    assert (
        offline(data, conn).spender(h(1), h(490), 0).as_of == TIP
    )  # offline, the snapshot holds at this tip
    set_tip(conn, OTHER_TIP)  # a new block: the snapshot is stale and isn't trusted
    with pytest.raises(OfflineError):
        offline(data, conn).spender(h(1), h(490), 0)


def test_a_mempool_spend_is_shown_as_unconfirmed_and_never_cached_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT_UNCONFIRMED, h(3))
    chain.txs[(h(3), None)] = tx(3, block=None, spends=Outpoint(h(1), 0))
    spend = online(data, conn).spender(h(1), h(490), 0)
    assert (spend.state, spend.spending_txid, spend.blockhash) == ("spent_unconfirmed", h(3), None)
    assert cc.get_spender(conn, Outpoint(h(1), 0)) is None
    assert cc.unspent_at(conn, Outpoint(h(1), 0)) is None


def test_a_spend_found_while_the_tip_moved_is_asked_again_and_cached_from_the_stable_read_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.tips = [EARLIER_TIP, TIP]  # moved onto the app's tip during the first lookup, then held
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT, h(2), h(495))
    chain.txs[(h(2), h(495))] = tx(2, block=495, confirmations=6, spends=Outpoint(h(1), 0))
    assert online(data, conn).spender(h(1), h(490), 0).state == "spent"
    assert chain.calls.count("spend_of 0") == 2
    assert cc.get_spender(conn, Outpoint(h(1), 0)) == cc.SpentBy(h(2), h(495), 495)


def test_an_unspent_answer_is_never_labelled_with_a_tip_it_wasnt_read_at_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.tips = [Tip(f"{i:02x}" * 32, 500 + i) for i in range(1, 20)]  # a new block between every read
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.UNSPENT)
    with pytest.raises(OfflineError):
        online(data, conn).spender(h(1), h(490), 0)
    assert cc.unspent_at(conn, Outpoint(h(1), 0)) is None


def test_the_earlier_bip30_duplicate_coinbase_is_unspendable_before_any_cache_is_read_t208(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    [(dup, earlier)] = list(spenders.BIP30_OVERWRITTEN.items())[:1]
    coinbase = Tx(
        dup, earlier, 11, 1_231_000_000, (TxIn(None, 0xFFFFFFFF),), (TxOut(50 * 10**8, MINE, "pubkey"),)
    )
    cc.put_tx(conn, coinbase, 490, TIP)
    cc.put_tx(conn, tx(2, block=495, confirmations=6, spends=Outpoint(dup, 0)), 495, TIP)
    # rows for the later copy, keyed by outpoint only: a confirmed spend
    cc.put_spender(conn, Outpoint(dup, 0), cc.SpentBy(h(2), h(495), 495), TIP)
    graph = offline(data, conn)  # offline, the cached spend would otherwise be the answer
    assert graph.spender(dup, earlier, 0).state == "unspendable"
    assert graph.tx(dup, earlier).outputs[0].unspendable
    assert chain.calls == []  # decided from the cached coinbase and the BIP30 list alone


def test_online_a_cached_unspent_snapshot_never_hides_a_mempool_spend(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    cc.put_unspent(conn, Outpoint(h(1), 0), TIP)  # "unspent" at this tip, from an earlier lookup
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT_UNCONFIRMED, h(3))
    chain.txs[(h(3), None)] = tx(3, block=None, spends=Outpoint(h(1), 0))
    spend = online(data, conn).spender(h(1), h(490), 0)
    assert (spend.state, spend.spending_txid) == ("spent_unconfirmed", h(3))


def test_an_unconfirmed_transactions_outputs_are_never_cached_t207(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    chain.txs[(h(1), None)] = tx(1, block=None)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.UNSPENT)
    assert online(data, conn).spender(h(1), None, 0).state == "unspent"
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 0
    assert "fill_prevouts" not in chain.calls  # a spender lookup needs only the outputs (T-205)


def test_a_transaction_named_in_a_stale_block_is_not_found(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    chain.txs[(h(1), None)] = tx(1, block=480, confirmations=0)  # txindex names a block no longer active
    with pytest.raises(NotFound):
        online(data, conn).tx(h(1), None)
    assert conn.execute("SELECT COUNT(*) FROM tx_cache").fetchone()[0] == 0


def test_a_spenders_block_that_left_the_chain_mid_lookup_means_try_again(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT, h(2), h(495))
    chain.stale.add(h(2))  # the spender's block was reorged away after the node named it
    with pytest.raises(OfflineError):
        online(data, conn).spender(h(1), h(490), 0)
    assert cc.get_spender(conn, Outpoint(h(1), 0)) is None


def test_a_spender_that_doesnt_spend_the_output_is_refused_and_not_cached(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT, h(2), h(495))
    chain.txs[(h(2), h(495))] = tx(2, block=495, confirmations=6)  # spends something else
    with pytest.raises(ImportRefused):
        online(data, conn).spender(h(1), h(490), 0)
    assert cc.get_spender(conn, Outpoint(h(1), 0)) is None


def test_a_mempool_spender_that_doesnt_spend_the_output_is_refused(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT_UNCONFIRMED, h(3))
    chain.txs[(h(3), None)] = tx(3, block=None)  # spends something else
    with pytest.raises(ImportRefused):
        online(data, conn).spender(h(1), h(490), 0)


def test_a_mempool_spender_that_keeps_disappearing_means_try_again(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT_UNCONFIRMED, h(3))
    chain.missing.add(h(3))  # replaced or evicted each time it is fetched
    with pytest.raises(OfflineError):
        online(data, conn).spender(h(1), h(490), 0)
    assert chain.calls.count("spend_of 0") == TIP_RETRIES


def test_a_confirmed_spender_missing_from_its_active_block_is_refused(
    data: DataDir, conn: sqlite3.Connection, chain: FakeChain
) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT, h(2), h(495))
    chain.missing.add(h(2))  # the node names a spender its own block doesn't hold
    with pytest.raises(ImportRefused):
        online(data, conn).spender(h(1), h(490), 0)
    assert chain.calls.count("spend_of 0") == 1  # a contradiction, not a race: no retry

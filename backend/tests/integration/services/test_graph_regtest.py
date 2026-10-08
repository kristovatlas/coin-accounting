"""Graph expansion against a real regtest node (PLAN §4, M3; architecture §8.3): a transaction read
through the app's own RPC user, its spend found forward, the spending transaction cached at its true
height, and an unspent output snapshotted at the tip. The node's wallet is used only as the harness
user, to make the transactions. Synthetic regtest data only."""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.domain.chain import Outpoint
from coinacct.services import chain_sync
from coinacct.services.graph import Graph
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import open_db, open_reader
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client

# A fixed regtest address no wallet here owns (the BIP173 test vector), so its outputs stay unspent.
OUTSIDE = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-graph"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        n.mine(101, n.admin("getnewaddress"))
        yield n


@pytest.fixture
def data(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


@pytest.fixture
def conn(data: DataDir) -> Iterator[sqlite3.Connection]:
    c = open_db(data)
    record_chain(c, "regtest")
    yield c
    c.close()


def test_a_spend_is_found_forward_and_cached_at_its_block_height(
    node: RegtestNode, conn: sqlite3.Connection, data: DataDir
) -> None:
    rpc = app_client(node)
    # A payment to the wallet itself, then a spend of exactly that output.
    own = node.admin("getnewaddress")
    first = node.admin("sendtoaddress", [own, "1.0"])
    node.mine(1)
    raw = node.admin("getrawtransaction", [first, True])
    n = next(o["n"] for o in raw["vout"] if o["scriptPubKey"].get("address") == own)
    spend_raw = node.admin("createrawtransaction", [[{"txid": first, "vout": n}], [{OUTSIDE: "0.999"}]])
    signed = node.admin("signrawtransactionwithwallet", [spend_raw])["hex"]
    second = node.admin("sendrawtransaction", [signed])
    [spent_in] = node.mine(1)
    chain_sync.at_startup(rpc, conn)  # the app's reference tip is now the node's tip

    graph = Graph(conn, functools.partial(open_reader, data), rpc)
    view = graph.tx(first, raw["blockhash"])
    assert view.txid == first and view.blockhash == raw["blockhash"] and view.outputs[n].sats == 100_000_000
    assert cc.get_tx(conn, first, raw["blockhash"]) is not None  # cached at the tip it was read at

    spend = graph.spender(first, raw["blockhash"], n)
    assert (spend.state, spend.spending_txid, spend.blockhash) == ("spent", second, spent_in)
    by = cc.get_spender(conn, Outpoint(first, n))
    assert by is not None and by.height == node.admin("getblockheader", [spent_in])["height"]

    [out] = [o for o in graph.tx(second, spent_in).outputs if o.address == OUTSIDE]
    unspent = graph.spender(second, spent_in, out.n)
    assert unspent.state == "unspent" and unspent.as_of is not None
    assert unspent.as_of.blockhash == node.admin("getbestblockhash")

"""The chain sync service against a real regtest node, as the app's whitelisted user (architecture
§8.1, §8.4; THREAT_MODEL T-207, §7 "a regtest reorg invalidates the affected cache rows").

The harness user forces the reorg with `invalidateblock`; the app never calls it. Synthetic regtest
data only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.chain.scans import Scan
from coinacct.services import chain_sync
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import Tip, last_tip, record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client

# A fixed regtest address (BIP173 test vector) to mine the replacement branch to.
OTHER = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-sync"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        n.mine(101, n.admin("getnewaddress"))
        yield n


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def test_start_up_then_syncs_follow_the_chain_through_a_reorg_t207(
    node: RegtestNode, conn: sqlite3.Connection
) -> None:
    rpc = app_client(node)
    watched = node.admin("getnewaddress")
    subject = Scan(f"addr({watched})", (f"addr({watched})",))
    spk = node.admin("getaddressinfo", [watched])["scriptPubKey"]

    assert chain_sync.at_startup(rpc, conn) is None  # §8.1: nothing left running; a first catch-up
    first = chain_sync.sync(rpc, conn, [subject])
    assert first.complete and last_tip(conn) == first.target

    paid = node.admin("sendtoaddress", [watched, "0.3"])
    [block] = node.mine(1)
    second = chain_sync.sync(rpc, conn, [subject])
    assert second.complete and [e.txid for e in cc.activity_for(conn, spk)] == [paid]

    # Replace that block with two others. The payment goes back to the mempool and is mined again,
    # in a block of the new branch.
    node.admin("invalidateblock", [block])
    node.mine(2, OTHER)
    reorged = chain_sync.sync(rpc, conn, [subject])
    assert reorged.complete and reorged.change is not None and reorged.change.invalidated is not None
    assert paid in reorged.invalidated_txids  # its old block was orphaned: flagged for review
    assert last_tip(conn) == Tip(node.admin("getbestblockhash"), node.admin("getblockcount"))
    [event] = cc.activity_for(conn, spk)
    assert event.txid == paid and event.blockhash != block  # found again, in its new block
    assert node.admin("getblockheader", [event.blockhash])["confirmations"] >= 1

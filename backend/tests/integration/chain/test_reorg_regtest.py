"""Fork-point reorg handling against a real regtest node, as the app's whitelisted user (PLAN §1
"Reorgs and new blocks"; THREAT_MODEL T-207, §7 "a regtest reorg invalidates the affected cache rows").

The harness user forces the reorg with `invalidateblock`; the app never calls it. Synthetic regtest
data only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.chain.reorg import TipChange, catch_up
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import Tip, last_tip, record_chain, set_tip
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client

# Another fixed regtest address (BIP173 test vector), so the replacement blocks differ from the old.
OTHER = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-reorg")) as n:
        n.mine(10)
        yield n


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def activity(node: RegtestNode, height: int, n: int) -> cc.Activity:
    blockhash = node.admin("getblockhash", [height])
    return cc.Activity("receive", "51", f"{n:064x}", 0, 1, blockhash, height)


def test_a_reorg_while_the_app_was_away_invalidates_above_the_fork_t207(
    node: RegtestNode, conn: sqlite3.Connection
) -> None:
    rpc = app_client(node)
    first = catch_up(rpc, conn)
    assert first is not None and first.invalidated is None
    top = first.new.height
    kept, gone = activity(node, top - 2, 1), activity(node, top, 2)
    cc.put_activity(conn, [kept, gone], first.new)

    # Replace the newest two blocks with three others, then catch up once, as after a restart.
    node.admin("invalidateblock", [node.admin("getblockhash", [top - 1])])
    node.mine(3, OTHER)
    change = catch_up(rpc, conn)

    assert change is not None and change.invalidated is not None
    assert change.invalidated.fork == Tip(node.admin("getblockhash", [top - 2]), top - 2)
    assert change.invalidated.txids == {gone.txid}
    assert change.new == Tip(node.admin("getbestblockhash"), top + 1)
    assert cc.activity_for(conn, "51") == [kept]
    assert cc.scan_target(conn) == change.new
    assert catch_up(rpc, conn) == TipChange(None, change.new, None)  # still pending: reported again
    cc.complete_scan_target(conn, change.new)
    assert last_tip(conn) == change.new
    assert catch_up(rpc, conn) is None  # caught up


def test_new_blocks_without_a_reorg_keep_the_cache(node: RegtestNode, conn: sqlite3.Connection) -> None:
    rpc = app_client(node)
    first = catch_up(rpc, conn)
    assert first is not None
    row = activity(node, first.new.height, 3)
    cc.put_activity(conn, [row], first.new)
    node.mine(2)
    change = catch_up(rpc, conn)
    assert change is not None and change.invalidated is None and change.new.height == first.new.height + 2
    assert cc.activity_for(conn, "51") == [row]


def test_a_recorded_tip_the_node_doesnt_know_invalidates_everything_t207(
    node: RegtestNode, conn: sqlite3.Connection
) -> None:
    stranger = Tip("ab" * 32, 5)  # e.g. after the node was resynced, or swapped for another one
    set_tip(conn, stranger)
    cc.put_activity(conn, [cc.Activity("receive", "51", f"{4:064x}", 0, 1, "cd" * 32, 3)], stranger)
    change = catch_up(app_client(node), conn)
    assert change is not None and change.invalidated is not None
    assert change.invalidated.fork == Tip(node.admin("getblockhash", [0]), 0)
    assert cc.activity_for(conn, "51") == []
    assert change.new == Tip(node.admin("getbestblockhash"), node.admin("getblockcount"))
    assert cc.scan_target(conn) == change.new and last_tip(conn) == change.invalidated.fork

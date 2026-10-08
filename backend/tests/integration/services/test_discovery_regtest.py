"""A ranged descriptor's window grows until it has found the wallet's used addresses, against a real
regtest node as the app's whitelisted user (PLAN §1, §3; T-207, T-210).

The node's wallet is used only as the harness user, to make a public descriptor and pay its addresses;
the app never uses a wallet (ADR 0004). Synthetic regtest data only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.services import chain_sync, discovery, imports
from coinacct.storage import accounts as ac
from coinacct.storage import chain_cache as cc
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client
from tests.integration.services.test_imports_regtest import receive_descriptor


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-discovery"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        yield n


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def test_the_window_grows_past_the_first_gap_to_the_wallets_used_addresses(
    node: RegtestNode, conn: sqlite3.Connection
) -> None:
    rpc = app_client(node)
    receive = [node.admin("getnewaddress", ["", "bech32"]) for _ in range(5)]  # indexes 0..4
    node.mine(101, receive[0])
    node.admin("sendtoaddress", [receive[1], "0.5"])
    paid = node.admin("sendtoaddress", [receive[4], "0.25"])  # past the first window, 0..2
    node.mine(1, receive[0])

    preview = imports.preview_descriptor(rpc, conn, receive_descriptor(node, "wpkh"), gap_limit=3)
    account = ac.add_tax_account(conn, "Wallet", "self_custody")
    d = imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=account)
    assert ac.descriptors(conn)[0].range_end == 2

    chain_sync.at_startup(rpc, conn)
    for _ in range(5):  # sync, grow, until the gap limit of unused indexes is found
        result = chain_sync.sync(rpc, conn, imports.subjects(conn))
        assert result.complete
        if not discovery.extend_windows(rpc, conn):
            break
    else:
        pytest.fail("the window never stopped growing")

    [desc] = ac.descriptors(conn)
    assert desc.highest_used == 4 and desc.range_end == 11  # 0..2, then 0..5, then 0..11
    scripts = dict(ac.descriptor_scripts(conn, d))
    assert scripts[4] == node.admin("getaddressinfo", [receive[4]])["scriptPubKey"]
    assert [e.txid for e in cc.activity_for(conn, scripts[4]) if e.kind == "receive"] == [paid]

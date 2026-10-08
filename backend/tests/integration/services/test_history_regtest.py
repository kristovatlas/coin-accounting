"""History after a real sync (PLAN §3): an imported address paid twice on a regtest node shows both
outputs as UTXOs, its balance and its events, read back through readers. The node's wallet is used
only as the harness user, to pay the address (one the wallet doesn't own). Synthetic regtest data only.
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.services import chain_sync, imports
from coinacct.services.history import History
from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import open_db, open_reader
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client

# A fixed regtest address no wallet here owns (the BIP173 test vector), so its outputs stay unspent.
OUTSIDE = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-history"), extra_args=["-fallbackfee=0.0001"]) as n:
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


def test_an_imported_address_shows_its_utxos_and_balance_after_a_sync(
    node: RegtestNode, conn: sqlite3.Connection, data: DataDir
) -> None:
    rpc = app_client(node)
    # Not the wallet's own address: its coin selection would spend one payment to make the other.
    address = OUTSIDE
    first = node.admin("sendtoaddress", [address, "0.3"])
    second = node.admin("sendtoaddress", [address, "0.2"])
    node.mine(1)

    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    preview = imports.preview_addresses(conn, address)
    imports.import_addresses(conn, preview, entity_id=ME, tax_account_id=wallet)
    chain_sync.at_startup(rpc, conn)
    assert chain_sync.sync(rpc, conn, imports.subjects(conn)).complete

    history = History(functools.partial(open_reader, data))
    [summary] = history.addresses().addresses
    assert (summary.address, summary.balance, summary.utxos, summary.transactions) == (
        address,
        50_000_000,
        2,
        2,
    )
    utxos = history.utxos(wallet)
    assert {u.txid for u in utxos} == {first, second} and sum(u.sats for u in utxos) == 50_000_000
    events = history.events(summary.script_hex)
    assert events is not None and {e.txid for e in events if e.kind == "receive"} == {first, second}

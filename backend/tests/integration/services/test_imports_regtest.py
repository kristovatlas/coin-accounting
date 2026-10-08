"""Importing a public descriptor against a real regtest node, as the app's whitelisted user (PLAN §3;
ADR 0019; THREAT_MODEL T-701, T-703).

The node's wallet is used only as the harness user, to make a public descriptor and the addresses to
compare with; the app never uses a wallet (ADR 0004). Synthetic regtest data only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.services import imports
from coinacct.services.imports import ImportRefused
from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-imports")) as n:
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


def receive_descriptor(node: RegtestNode, kind: str) -> str:
    """The harness wallet's public receive descriptor of one kind (wpkh, tr), without its checksum."""
    descriptors = node.admin("listdescriptors")["descriptors"]
    [desc] = [d["desc"] for d in descriptors if d["desc"].startswith(kind + "(") and not d["internal"]]
    return str(desc).split("#")[0]


@pytest.mark.parametrize(("kind", "address_type"), [("wpkh", "bech32"), ("tr", "bech32m")])
def test_a_wallets_public_descriptor_derives_the_wallets_own_addresses_t701(
    node: RegtestNode, conn: sqlite3.Connection, kind: str, address_type: str
) -> None:
    text = receive_descriptor(node, kind)
    preview = imports.preview_descriptor(app_client(node), conn, text, gap_limit=5)
    wallet_addresses = [node.admin("getnewaddress", ["", address_type]) for _ in range(5)]
    assert [address for _, _, address in preview.derived] == wallet_addresses
    for _, script, address in preview.derived:
        assert node.admin("getaddressinfo", [address])["scriptPubKey"] == script
    account = ac.add_tax_account(conn, f"Wallet {kind}", "self_custody")
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=account)
    [subject] = imports.subjects(conn)
    assert subject.scanobjects == ({"desc": preview.info.text, "range": [0, 4]},)


def test_a_descriptor_the_node_cant_read_is_refused_without_its_text_t403(
    node: RegtestNode, conn: sqlite3.Connection
) -> None:
    broken = "wpkh(tpubnotakey/0/*)"
    with pytest.raises(ImportRefused) as e:
        imports.preview_descriptor(app_client(node), conn, broken)
    assert "tpubnotakey" not in str(e.value)

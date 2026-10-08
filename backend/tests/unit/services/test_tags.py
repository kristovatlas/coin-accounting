"""The tagging service (PLAN §4, M3): tags through `storage.tags`, a sync for a newly tagged address,
and the change log read back. Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.domain.keys import PrivateKeyError
from coinacct.services.imports import Busy, ImportRefused, Imports
from coinacct.services.tags import Tagging
from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import DbBusy, open_db, open_reader

SCRIPT = "0014" + "cc" * 20
# BIP173's P2WPKH test program in regtest form, and its script (public test data).
REGTEST_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
REGTEST_SCRIPT = "0014751e76e8199196d454941c45d1b3a323f1433bd6"
TXID = "ab" * 32
AT = "2026-10-08T12:00:00+00:00"


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


class Syncs:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def syncs() -> Syncs:
    return Syncs()


@pytest.fixture
def tagging(data: DataDir, conn: sqlite3.Connection, syncs: Syncs) -> Tagging:
    imports = Imports(conn, lambda: open_reader(data), None)
    imports.jobs_started(syncs)
    return Tagging(conn, lambda: open_reader(data), imports, now=lambda: AT)


def test_a_newly_tagged_address_is_scanned_and_a_retag_isnt_rescanned(
    tagging: Tagging, conn: sqlite3.Connection, syncs: Syncs
) -> None:
    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    assert tagging.tag_address(
        SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[]
    )
    assert syncs.count == 1
    assert not tagging.tag_address(
        SCRIPT, text=None, entity_id=ME, tax_account_id=wallet, label="", client_ids=[]
    )
    assert syncs.count == 1  # the address was already scanned, whoever owns it
    [first, second] = tagging.changes("address_tag", SCRIPT)
    assert first.at == AT and second.before == first.after


def test_a_refused_tag_is_a_fixed_message_that_doesnt_repeat_the_input(tagging: Tagging) -> None:
    with pytest.raises(ImportRefused) as caught:
        tagging.tag_address(SCRIPT, text=None, entity_id=ME, tax_account_id=None, label="", client_ids=[])
    assert SCRIPT not in str(caught.value)


def test_a_private_key_in_a_label_is_refused_t703(tagging: Tagging, conn: sqlite3.Connection) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    wif_shaped = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")[:52]
    with pytest.raises(PrivateKeyError):
        tagging.tag_address(
            SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label=wif_shaped, client_ids=[]
        )


def test_the_mixing_flag_and_its_history(tagging: Tagging) -> None:
    tagging.set_mixing(TXID, True)
    [change] = tagging.changes("tx_flag", TXID)
    assert change.kind == "tx_flag" and change.after == {"mixing": True, "source": "user"}


def test_a_busy_db_is_busy(tagging: Tagging, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*args: object, **kwargs: object) -> None:
        raise DbBusy("the user DB is busy")

    monkeypatch.setattr("coinacct.storage.tags.set_mixing", busy)
    with pytest.raises(Busy):
        tagging.set_mixing(TXID, True)


def test_an_address_text_is_parsed_and_stored_in_its_normal_form(
    tagging: Tagging, conn: sqlite3.Connection
) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    tagging.tag_address(
        REGTEST_SCRIPT,
        text=REGTEST_ADDRESS.upper(),
        entity_id=exchange,
        tax_account_id=None,
        label="",
        client_ids=[],
    )
    assert ac.addresses(conn)[0].text == REGTEST_ADDRESS


@pytest.mark.parametrize(
    ("script", "text", "reason"),
    [
        (SCRIPT, REGTEST_ADDRESS, "doesn't pay to that script"),
        (REGTEST_SCRIPT, "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "isn't an address of this chain"),
        (REGTEST_SCRIPT, "not an address", "isn't an address of this chain"),
    ],
)
def test_an_address_text_that_isnt_the_scripts_is_refused_t701(
    tagging: Tagging, conn: sqlite3.Connection, script: str, text: str, reason: str
) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    with pytest.raises(ImportRefused, match=reason) as caught:
        tagging.tag_address(
            script, text=text, entity_id=exchange, tax_account_id=None, label="", client_ids=[]
        )
    assert text not in str(caught.value)
    assert ac.addresses(conn) == []


def test_a_private_key_as_address_text_gets_the_key_refusal_t703(
    tagging: Tagging, conn: sqlite3.Connection
) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    wif_shaped = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")[:52]
    with pytest.raises(PrivateKeyError):
        tagging.tag_address(
            SCRIPT, text=wif_shaped, entity_id=exchange, tax_account_id=None, label="", client_ids=[]
        )


def test_without_a_recorded_chain_an_address_text_is_refused(data: DataDir, syncs: Syncs) -> None:
    c = open_db(data)  # no chain recorded yet
    try:
        imports = Imports(c, lambda: open_reader(data), None)
        tagging = Tagging(c, lambda: open_reader(data), imports, now=lambda: AT)
        exchange = ac.add_entity(c, "Some exchange", "exchange")
        with pytest.raises(ImportRefused, match="no chain is recorded") as caught:
            tagging.tag_address(
                REGTEST_SCRIPT,
                text=REGTEST_ADDRESS,
                entity_id=exchange,
                tax_account_id=None,
                label="",
                client_ids=[],
            )
        assert REGTEST_ADDRESS not in str(caught.value) and ac.addresses(c) == []
    finally:
        c.close()

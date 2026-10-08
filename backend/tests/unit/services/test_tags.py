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
    [first, second] = tagging.changes(SCRIPT)
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
    [change] = tagging.changes(TXID)
    assert change.kind == "tx_flag" and change.after == {"mixing": True, "source": "user"}


def test_a_busy_db_is_busy(tagging: Tagging, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*args: object, **kwargs: object) -> None:
        raise DbBusy("the user DB is busy")

    monkeypatch.setattr("coinacct.storage.tags.set_mixing", busy)
    with pytest.raises(Busy):
        tagging.set_mixing(TXID, True)

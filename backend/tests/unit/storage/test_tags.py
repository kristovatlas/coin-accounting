"""Tags from the graph and the append-only change log (PLAN §2, §4; THREAT_MODEL T-408, T-703).
Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.domain.keys import PrivateKeyError
from coinacct.storage import accounts as ac
from coinacct.storage import tags
from coinacct.storage.accounts import ME, AccountsError
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db

AT = "2026-10-08T12:00:00+00:00"
LATER = "2026-10-08T12:05:00+00:00"
SCRIPT = "0014" + "cc" * 20
TXID = "ab" * 32
# Shaped like a WIF private key (a Base58 alphabet slice), not a real key.
WIF_SHAPED = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")[:52]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


@pytest.fixture
def wallet(conn: sqlite3.Connection) -> int:
    return ac.add_tax_account(conn, "Cold storage", "self_custody")


@pytest.fixture
def exchange(conn: sqlite3.Connection) -> int:
    return ac.add_entity(conn, "Some exchange", "exchange")


def test_a_new_address_is_added_and_logged_t408(conn: sqlite3.Connection, exchange: int) -> None:
    assert tags.tag_address(
        conn,
        SCRIPT,
        text="bcrt1qx",
        entity_id=exchange,
        tax_account_id=None,
        label="deposit",
        client_ids=[],
        at=AT,
    )
    [a] = ac.addresses(conn)
    assert (a.script_hex, a.entity_id, a.tax_account_id, a.label, a.source) == (
        SCRIPT,
        exchange,
        None,
        "deposit",
        "manual",
    )
    [log] = tags.changes(conn, SCRIPT)
    assert (log.kind, log.before, log.at) == ("address_tag", None, AT)
    assert log.after == {"entity_id": exchange, "tax_account_id": None, "label": "deposit", "client_ids": []}


def test_retagging_logs_before_and_after_and_a_no_op_logs_nothing_t408(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    client = ac.add_client(conn, "Phone", "mobile")
    tags.tag_address(
        conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    assert not tags.tag_address(
        conn,
        SCRIPT,
        text=None,
        entity_id=ME,
        tax_account_id=wallet,
        label="mine",
        client_ids=[client],
        at=LATER,
    )
    [a] = ac.addresses(conn)
    assert (a.entity_id, a.tax_account_id, a.label, a.client_ids) == (ME, wallet, "mine", (client,))
    tags.tag_address(  # the same again: nothing changes, nothing is logged
        conn,
        SCRIPT,
        text=None,
        entity_id=ME,
        tax_account_id=wallet,
        label="mine",
        client_ids=[client],
        at=LATER,
    )
    first, second = tags.changes(conn, SCRIPT)
    assert second.before == first.after and second.at == LATER
    assert second.after == {
        "entity_id": ME,
        "tax_account_id": wallet,
        "label": "mine",
        "client_ids": [client],
    }


@pytest.mark.parametrize("statement", ["UPDATE change_log SET kind = 'tx_flag'", "DELETE FROM change_log"])
def test_the_change_log_is_append_only_t408(conn: sqlite3.Connection, exchange: int, statement: str) -> None:
    tags.tag_address(
        conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)
    assert len(tags.changes(conn)) == 1


def test_the_schemas_ownership_rules_hold_and_nothing_is_logged_on_a_refusal(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    with pytest.raises(AccountsError):  # the user's address needs a self-custody account
        tags.tag_address(
            conn, SCRIPT, text=None, entity_id=ME, tax_account_id=None, label="", client_ids=[], at=AT
        )
    with pytest.raises(AccountsError):  # someone else's address has none
        tags.tag_address(
            conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=wallet, label="", client_ids=[], at=AT
        )
    with pytest.raises(AccountsError):  # an unknown client
        tags.tag_address(
            conn,
            SCRIPT,
            text=None,
            entity_id=exchange,
            tax_account_id=None,
            label="",
            client_ids=[999],
            at=AT,
        )
    assert ac.addresses(conn) == [] and tags.changes(conn) == []


def test_a_descriptors_address_changes_owner_only_with_its_descriptor(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    ac.add_descriptor(
        conn,
        "wpkh(tpub/0/*)#abcdefgh",
        [(0, SCRIPT, "bcrt1qx")],
        entity_id=ME,
        tax_account_id=wallet,
        gap_limit=20,
    )
    with pytest.raises(AccountsError):
        tags.tag_address(
            conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
        )
    # its label can still change
    tags.tag_address(
        conn, SCRIPT, text=None, entity_id=ME, tax_account_id=wallet, label="change", client_ids=[], at=AT
    )
    assert ac.addresses(conn)[0].label == "change"


def test_a_private_key_in_a_tag_never_reaches_the_db_t703(conn: sqlite3.Connection, exchange: int) -> None:
    with pytest.raises(PrivateKeyError):
        tags.tag_address(
            conn,
            SCRIPT,
            text=None,
            entity_id=exchange,
            tax_account_id=None,
            label=WIF_SHAPED,
            client_ids=[],
            at=AT,
        )
    assert ac.addresses(conn) == [] and tags.changes(conn) == []


def test_the_mixing_flag_is_set_cleared_and_logged(conn: sqlite3.Connection) -> None:
    assert tags.mixing(conn, TXID) is None
    tags.set_mixing(conn, TXID, True, at=AT)
    tags.set_mixing(conn, TXID, True, at=AT)  # no change: not logged
    tags.set_mixing(conn, TXID, False, at=LATER)
    assert tags.mixing(conn, TXID) is False
    first, second = tags.changes(conn, TXID)
    assert (first.before, first.after) == (None, {"mixing": True, "source": "user"})
    assert (second.before, second.after) == (
        {"mixing": True, "source": "user"},
        {"mixing": False, "source": "user"},
    )
    with pytest.raises(ValueError):
        tags.set_mixing(conn, "not a txid", True, at=AT)

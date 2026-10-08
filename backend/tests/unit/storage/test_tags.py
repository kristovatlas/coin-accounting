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
from coinacct.storage.db import DbError, open_db
from coinacct.storage.migrations import STEPS

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
    [log] = tags.changes(conn, ("address_tag", SCRIPT))
    assert (log.kind, log.before, log.at) == ("address_tag", None, AT)
    assert log.after == {
        "entity_id": exchange,
        "tax_account_id": None,
        "label": "deposit",
        "text": "bcrt1qx",
        "source": "manual",
        "client_ids": [],
    }


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
    first, second = tags.changes(conn, ("address_tag", SCRIPT))
    assert second.before == first.after and second.at == LATER
    assert second.after == {
        "entity_id": ME,
        "tax_account_id": wallet,
        "label": "mine",
        "text": None,
        "source": "manual",
        "client_ids": [client],
    }


def test_a_given_address_text_replaces_the_stored_one_and_none_keeps_it_t408(
    conn: sqlite3.Connection, exchange: int
) -> None:
    def tag(text: str | None) -> None:
        tags.tag_address(
            conn, SCRIPT, text=text, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
        )

    tag(None)
    tag("bcrt1qx")  # filled in, and logged
    tag("bcrt1qy")  # replaced, and logged
    tag(None)  # kept: not a change
    assert ac.addresses(conn)[0].text == "bcrt1qy"
    texts = [
        (c.before and c.before["text"], c.after["text"]) for c in tags.changes(conn, ("address_tag", SCRIPT))
    ]
    assert texts == [(None, None), (None, "bcrt1qx"), ("bcrt1qx", "bcrt1qy")]


def test_a_text_stored_before_the_check_can_be_corrected_t408(
    conn: sqlite3.Connection, exchange: int
) -> None:
    # what #207's route could store: any text, unchecked
    conn.execute(
        "INSERT INTO address (script_hex, text, entity_id, label, source)"
        " VALUES (?, 'not it', ?, '', 'manual')",
        (SCRIPT, exchange),
    )
    tags.tag_address(
        conn, SCRIPT, text="bcrt1qx", entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    assert ac.addresses(conn)[0].text == "bcrt1qx"
    [change] = tags.changes(conn, ("address_tag", SCRIPT))
    assert change.before is not None and (change.before["text"], change.after["text"]) == (
        "not it",
        "bcrt1qx",
    )


def test_the_history_of_a_script_and_a_txid_that_look_alike_stays_apart(
    conn: sqlite3.Connection, exchange: int
) -> None:
    tags.tag_address(
        conn, TXID, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    tags.set_mixing(conn, TXID, True, at=AT)
    assert [c.kind for c in tags.changes(conn, ("address_tag", TXID))] == ["address_tag"]
    assert [c.kind for c in tags.changes(conn, ("tx_flag", TXID))] == ["tx_flag"]


@pytest.mark.parametrize("bad_id", [-1, 0])
def test_no_change_log_id_is_below_1_so_appends_keep_working_t408(
    conn: sqlite3.Connection, exchange: int, bad_id: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT INTO change_log (id, at, kind, subject, after) VALUES (?, ?, 'tx_flag', 'ab', '{}')",
            (bad_id, AT),
        )
    tags.tag_address(
        conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    assert [c.id for c in tags.changes(conn)] == [1]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE change_log SET kind = 'tx_flag'",
        "DELETE FROM change_log",
        # REPLACE deletes the old row without firing delete triggers (recursive_triggers is off)
        "INSERT OR REPLACE INTO change_log (id, at, kind, subject, after)"
        " VALUES (1, '2026-10-08T12:00:00+00:00', 'tx_flag', 'ab', '{}')",
        "REPLACE INTO change_log (id, at, kind, subject, after)"
        " VALUES (1, '2026-10-08T12:00:00+00:00', 'tx_flag', 'ab', '{}')",
    ],
)
def test_the_change_log_is_append_only_t408(conn: sqlite3.Connection, exchange: int, statement: str) -> None:
    tags.tag_address(
        conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)
    [log] = tags.changes(conn)
    assert log.kind == "address_tag"


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
    first, second = tags.changes(conn, ("tx_flag", TXID))
    assert (first.before, first.after) == (None, {"mixing": True, "source": "user"})
    assert (second.before, second.after) == (
        {"mixing": True, "source": "user"},
        {"mixing": False, "source": "user"},
    )
    with pytest.raises(ValueError):
        tags.set_mixing(conn, "not a txid", True, at=AT)


@pytest.mark.parametrize("bad_id", [-1, 0])
def test_a_db_with_an_id_below_1_from_before_m0008_is_refused_at_the_upgrade_t408(
    tmp_path: Path, bad_id: int
) -> None:
    d = tmp_path / "old"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:7]))  # schema 7: m0007's guard, no m0008 yet
    try:
        old.execute(  # what only an edit outside the app could write
            "INSERT INTO change_log (id, at, kind, subject, after) VALUES (?, ?, 'tx_flag', ?, '{}')",
            (bad_id, AT, TXID),
        )
    finally:
        old.close()
    with pytest.raises(DbError, match="SQLITE_CONSTRAINT_CHECK"):  # the step's CHECK refused it
        open_db(data)
    old = open_db(data, steps=list(STEPS[:8]))  # the refused step left the DB at version 8
    try:
        assert old.execute("PRAGMA user_version").fetchone() == (8,)
        assert old.execute("SELECT id FROM change_log").fetchall() == [(bad_id,)]
    finally:
        old.close()


def test_a_db_from_schema_7_upgrades_and_keeps_appending_t408(tmp_path: Path) -> None:
    d = tmp_path / "old"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:7]))
    try:
        record_chain(old, "regtest")
        tags.set_mixing(old, TXID, True, at=AT)
    finally:
        old.close()
    conn = open_db(data)
    try:
        tags.set_mixing(conn, TXID, False, at=LATER)
        assert [c.id for c in tags.changes(conn, ("tx_flag", TXID))] == [1, 2]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):  # replacing a row: still refused
            conn.execute(
                "INSERT OR REPLACE INTO change_log (id, at, kind, subject, after)"
                " VALUES (1, ?, 'tx_flag', 'ab', '{}')",
                (AT,),
            )
    finally:
        conn.close()


def test_a_reimport_that_adds_a_wallet_client_is_logged_once_t408(
    conn: sqlite3.Connection, wallet: int
) -> None:
    phone = ac.add_client(conn, "Phone", "mobile")

    def reimport(clients: list[int]) -> None:
        ac.add_addresses(
            conn,
            [(SCRIPT, None)],
            entity_id=ME,
            tax_account_id=wallet,
            source="import",
            client_ids=clients,
            at=AT,
        )

    reimport([])  # new: an import, not an edit
    assert tags.changes(conn) == []
    reimport([phone])  # a known address gains a client: an edit
    reimport([phone])  # nothing new
    [change] = tags.changes(conn, ("address_tag", SCRIPT))
    assert change.at == AT and change.before is not None
    assert (change.before["client_ids"], change.after["client_ids"]) == ([], [phone])

"""What made each change-log row (migration m0010; THREAT_MODEL T-504, T-408; #229). Synthetic data
only."""

from __future__ import annotations

import sqlite3
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.storage import accounts as ac
from coinacct.storage import change_log, tags
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from coinacct.storage.migrations import STEPS, m0010_change_log_origin

AT = "2026-10-08T12:00:00+00:00"
SCRIPT = "0014" + "cc" * 20
TXID = "ab" * 32


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def test_a_tag_and_a_flag_set_by_the_user_are_logged_as_the_users_t504(conn: sqlite3.Connection) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    tags.tag_address(
        conn, SCRIPT, text=None, entity_id=exchange, tax_account_id=None, label="", client_ids=[], at=AT
    )
    tags.set_mixing(conn, TXID, True, at=AT)
    assert [(c.kind, c.origin) for c in tags.changes(conn)] == [("address_tag", "user"), ("tx_flag", "user")]


@pytest.mark.parametrize(
    ("source", "origin"),
    [("import", "import"), ("descriptor", "descriptor"), ("discovery", "discovery"), ("manual", "user")],
)
def test_an_edit_by_an_import_is_logged_with_what_made_it_t504(
    conn: sqlite3.Connection, source: ac.Source, origin: str
) -> None:
    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    phone = ac.add_client(conn, "Phone", "mobile")
    for clients in ([], [phone]):  # the first adds the address; the second edits it
        ac.add_addresses(
            conn,
            [(SCRIPT, None)],
            entity_id=ME,
            tax_account_id=wallet,
            source=source,
            client_ids=clients,
            at=AT,
        )
    [change] = tags.changes(conn)
    assert change.origin == origin


def test_a_row_without_an_origin_is_refused_t408(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="names its origin"):
        conn.execute(
            "INSERT INTO change_log (at, kind, subject, after) VALUES (?, 'tx_flag', ?, '{}')", (AT, TXID)
        )
    with pytest.raises(sqlite3.IntegrityError):  # and only a known origin is accepted
        conn.execute(
            "INSERT INTO change_log (at, kind, subject, after, origin)"
            " VALUES (?, 'tx_flag', ?, '{}', 'robot')",
            (AT, TXID),
        )


def test_rows_from_before_the_origin_was_recorded_read_as_unknown_t504(tmp_path: Path) -> None:
    d = tmp_path / "old"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:9]))  # schema 9: no origin column yet
    try:
        record_chain(old, "regtest")
        old.execute(
            "INSERT INTO change_log (at, kind, subject, after) VALUES (?, 'tx_flag', ?, ?)",
            (AT, TXID, '{"mixing": true, "source": "user"}'),
        )
    finally:
        old.close()
    conn = open_db(data)
    try:
        tags.set_mixing(conn, TXID, False, at=AT)
        assert [c.origin for c in tags.changes(conn, ("tx_flag", TXID))] == [None, "user"]
    finally:
        conn.close()


def test_the_origin_check_lists_exactly_the_origins_the_code_writes() -> None:
    for origin in typing.get_args(change_log.Origin.__value__):
        assert f"'{origin}'" in m0010_change_log_origin.SQL

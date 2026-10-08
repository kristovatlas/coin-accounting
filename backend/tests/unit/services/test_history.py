"""Address, UTXO and transaction history from the chain cache (PLAN §3). Synthetic regtest scripts
and transactions only; `integration/services/test_history_regtest.py` runs it after a real sync."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.domain.chain import Outpoint
from coinacct.services import imports
from coinacct.services.history import History, Utxo
from coinacct.services.imports import Busy, ImportRefused
from coinacct.storage import accounts as ac
from coinacct.storage import chain_cache as cc
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import Tip, record_chain, set_tip
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import DbBusy, DbError, open_db, open_reader
from tests.unit.services.test_imports import TPUB_DESC, Node, p2wpkh

TIP = Tip("ff" * 32, 500)
A = ("0014" + "aa" * 20, "bcrt1qaddressa")  # (script, address text): synthetic, never parsed here
B = ("0014" + "bb" * 20, "bcrt1qaddressb")


def txid(n: int) -> str:
    return f"{n:064x}"


def block(height: int) -> str:
    return f"{height:064x}"


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


@pytest.fixture
def history(data: DataDir, conn: sqlite3.Connection) -> History:
    return History(lambda: open_reader(data))


def receive(script: str, tx: int, n: int, sats: int, height: int) -> cc.Activity:
    return cc.Activity("receive", script, txid(tx), n, sats, block(height), height)


def spend(script: str, tx: int, sats: int, height: int, prev: tuple[int, int]) -> cc.Activity:
    """Input 0 of tx `tx` spends output `prev` of `script`."""
    return cc.Activity(
        "spend", script, txid(tx), 0, sats, block(height), height, Outpoint(txid(prev[0]), prev[1])
    )


@pytest.fixture
def two_wallets(conn: sqlite3.Connection) -> tuple[int, int]:
    cold = ac.add_tax_account(conn, "Cold storage", "self_custody")
    hot = ac.add_tax_account(conn, "Hot wallet", "self_custody")
    ac.add_addresses(conn, [A], entity_id=ME, tax_account_id=cold, source="manual", label="savings")
    ac.add_addresses(conn, [B], entity_id=ME, tax_account_id=hot, source="manual")
    set_tip(conn, TIP)
    cc.put_activity(
        conn,
        [
            receive(A[0], 1, 0, 50_000, 100),
            receive(A[0], 2, 1, 20_000, 200),
            spend(A[0], 3, 50_000, 300, (1, 0)),  # tx 3 spends A's first output …
            receive(B[0], 3, 0, 45_000, 300),  # … and pays B
        ],
        TIP,
    )
    return cold, hot


def test_balances_are_the_unspent_receives(history: History, two_wallets: tuple[int, int]) -> None:
    cold, hot = two_wallets
    found = history.addresses()
    assert found.as_of == TIP
    by_script = {a.script_hex: a for a in found.addresses}
    a, b = by_script[A[0]], by_script[B[0]]
    assert (a.balance, a.utxos, a.received, a.transactions, a.last_height) == (20_000, 1, 70_000, 3, 300)
    assert (b.balance, b.utxos, b.received, b.transactions, b.last_height) == (45_000, 1, 45_000, 1, 300)
    assert (a.label, a.tax_account_id, b.tax_account_id) == ("savings", cold, hot)


def test_one_tax_account_at_a_time(history: History, two_wallets: tuple[int, int]) -> None:
    cold, hot = two_wallets
    assert [a.script_hex for a in history.addresses(cold).addresses] == [A[0]]
    assert history.utxos(hot) == [Utxo(txid(3), 0, 45_000, B[0], B[1], 300, complete=False)]  # not scanned


def test_utxos_are_oldest_first(history: History, two_wallets: tuple[int, int]) -> None:
    assert [(u.txid, u.vout) for u in history.utxos()] == [(txid(2), 1), (txid(3), 0)]


def test_an_address_with_no_activity_yet(history: History, conn: sqlite3.Connection) -> None:
    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    ac.add_addresses(conn, [A], entity_id=ME, tax_account_id=wallet, source="manual")
    [a] = history.addresses().addresses
    assert (a.balance, a.utxos, a.transactions, a.last_height) == (0, 0, 0, None)
    assert history.addresses().as_of is None  # no sync has run


def test_events_of_one_address(history: History, two_wallets: tuple[int, int]) -> None:
    events = history.events(A[0])
    assert events is not None
    assert [(e.kind, e.txid, e.n, e.sats, e.height) for e in events] == [
        ("receive", txid(1), 0, 50_000, 100),
        ("receive", txid(2), 1, 20_000, 200),
        ("spend", txid(3), 0, 50_000, 300),
    ]
    assert events[2].prevout == Outpoint(txid(1), 0)


def test_events_of_an_unknown_script_are_none(history: History, two_wallets: tuple[int, int]) -> None:
    assert history.events("0014" + "cc" * 20) is None


def test_each_read_closes_its_reader(data: DataDir, two_wallets: tuple[int, int]) -> None:
    opened: list[sqlite3.Connection] = []

    def reader() -> sqlite3.Connection:
        conn = open_reader(data)
        opened.append(conn)
        return conn

    history = History(reader)
    history.addresses()
    history.utxos()
    history.events(A[0])
    assert len(opened) == 3
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


# --- What a balance is as of (T-207, T-210) ------------------------------------------------------


def test_an_address_is_scanned_to_its_subjects_coverage(
    history: History, conn: sqlite3.Connection, two_wallets: tuple[int, int]
) -> None:
    assert {a.scanned_to for a in history.addresses().addresses} == {None}  # nothing scanned yet
    for scan in imports.subjects(conn):
        if f"raw({A[0]})" in scan.scanobjects:
            cc.extend_coverage(conn, cc.Coverage(scan.subject, 0, 450, block(450)), TIP)
    by_script = {a.script_hex: a for a in history.addresses().addresses}
    assert by_script[A[0]].scanned_to == 450 and by_script[B[0]].scanned_to is None


def test_events_above_the_last_finished_sync_are_left_out(
    history: History, conn: sqlite3.Connection, two_wallets: tuple[int, int]
) -> None:
    ahead = Tip(block(600), 600)
    cc.set_scan_target(conn, ahead)  # a catch-up towards 600 is under way
    cc.put_activity(conn, [receive(A[0], 9, 0, 1_000, 550)], ahead)
    found = history.addresses()
    assert found.as_of == TIP and found.catching_up
    a = next(x for x in found.addresses if x.script_hex == A[0])
    assert (a.balance, a.last_height) == (20_000, 300)  # the block-550 receive isn't counted yet
    assert txid(9) not in {u.txid for u in history.utxos()}
    events = history.events(A[0])
    assert events is not None and txid(9) not in {e.txid for e in events}


def test_nothing_counts_before_a_sync_has_finished(history: History, conn: sqlite3.Connection) -> None:
    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    ac.add_addresses(conn, [A], entity_id=ME, tax_account_id=wallet, source="manual")
    cc.set_scan_target(conn, TIP)  # the first catch-up, not finished
    cc.put_activity(conn, [receive(A[0], 1, 0, 5_000, 100)], TIP)
    found = history.addresses()
    assert found.as_of is None and found.catching_up and found.addresses[0].balance == 0
    assert history.utxos() == []


def test_utxos_are_only_the_users_own(
    history: History, conn: sqlite3.Connection, two_wallets: tuple[int, int]
) -> None:
    exchange = ac.add_entity(conn, "An exchange", "exchange")
    theirs = ("0014" + "dd" * 20, "bcrt1qexchange")
    ac.add_addresses(conn, [theirs], entity_id=exchange, tax_account_id=None, source="manual")
    cc.put_activity(conn, [receive(theirs[0], 7, 0, 9_000_000, 400)], TIP)
    assert theirs[0] not in {u.script_hex for u in history.utxos()}  # someone else's coins
    listed = {a.script_hex: a for a in history.addresses().addresses}
    assert listed[theirs[0]].entity_id == exchange and listed[theirs[0]].balance == 9_000_000


def test_a_duplicate_coinbase_outpoint_counts_once_the_later_t208(
    history: History, conn: sqlite3.Connection
) -> None:
    wallet = ac.add_tax_account(conn, "Miner", "self_custody")
    ac.add_addresses(conn, [A], entity_id=ME, tax_account_id=wallet, source="manual")
    set_tip(conn, TIP)
    cc.put_activity(conn, [receive(A[0], 5, 0, 5_000, 100), receive(A[0], 5, 0, 5_000, 200)], TIP)
    [a] = history.addresses().addresses
    assert (a.balance, a.utxos, a.received) == (5_000, 1, 5_000)
    assert a.transactions == 2  # two transactions, one txid: each is (txid, block)
    assert [u.height for u in history.utxos()] == [200]


def test_a_busy_or_unusable_db_is_the_import_services_error(data: DataDir) -> None:
    def busy() -> sqlite3.Connection:
        raise DbBusy("the user DB is busy; try again in a moment")

    def replaced() -> sqlite3.Connection:
        raise DbError("/media/veracrypt1/data/db.sqlite was replaced while it was opened (T-401)")

    with pytest.raises(Busy):
        History(busy).addresses()
    with pytest.raises(ImportRefused) as e:
        History(replaced).utxos()
    assert "veracrypt" not in str(e.value)


def test_a_descriptor_that_starts_later_doesnt_count_as_scanning_an_earlier_address(
    history: History, conn: sqlite3.Connection
) -> None:
    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    address, script = p2wpkh(1000)  # TPUB_DESC's index 0 in the fake node
    imports.import_addresses(
        conn, imports.preview_addresses(conn, address), entity_id=ME, tax_account_id=wallet, start_height=0
    )
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=2)
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=100)
    set_tip(conn, TIP)
    for scan in imports.subjects(conn):  # the descriptor is scanned further than the address's bucket
        stop = 450 if scan.subject.startswith("desc:") else 300
        cc.extend_coverage(conn, cc.Coverage(scan.subject, scan.start_height, stop, block(stop)), TIP)
    scanned = {a.script_hex: a.scanned_to for a in history.addresses().addresses}
    # Its own bucket covers it from 0; the descriptor's coverage starts at 100, after its history does.
    assert scanned[script] == 300


def test_a_utxo_is_complete_once_its_address_is_scanned_to_as_of(
    history: History, conn: sqlite3.Connection, two_wallets: tuple[int, int]
) -> None:
    for scan in imports.subjects(conn):
        if f"raw({B[0]})" in scan.scanobjects:
            cc.extend_coverage(conn, cc.Coverage(scan.subject, 0, TIP.height, TIP.blockhash), TIP)
    complete = {u.script_hex: u.complete for u in history.utxos()}
    assert complete == {A[0]: False, B[0]: True}

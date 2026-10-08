"""Address, UTXO and transaction history from the chain cache (PLAN §3). Synthetic regtest scripts
and transactions only; `integration/services/test_history_regtest.py` runs it after a real sync."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.domain.chain import Outpoint
from coinacct.services.history import History, Utxo
from coinacct.storage import accounts as ac
from coinacct.storage import chain_cache as cc
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import Tip, record_chain, set_tip
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import open_db, open_reader

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
    assert history.utxos(hot) == [Utxo(txid(3), 0, 45_000, B[0], B[1], 300)]


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

"""The chain-data cache: rows tied to their blocks, writes tied to the recorded tip, and reorg
invalidation at any depth (PLAN §1 "Caching and chain state"; THREAT_MODEL T-207, T-208)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.domain.chain import Outpoint, Tx, TxIn, TxOut
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_cache import Activity, Coverage, SpentBy, StaleTipError
from coinacct.storage.chain_state import Tip, last_tip, record_chain, set_tip
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import DbError, open_db


def h(n: int) -> str:
    return f"{n:064x}"


def block(height: int, branch: int = 0) -> Tip:
    """Block `height` on chain `branch`: a different branch means a different hash."""
    return Tip(h(branch << 32 | height), height)


TIP = block(100)
PAY = TxOut(5000, "0014" + "11" * 20, "witness_v0_keyhash", "bcrt1qexample")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    set_tip(c, TIP)
    yield c
    c.close()


def tx_in(at: Tip, txid: str = h(7)) -> Tx:
    spent = Outpoint(h(8), 1)
    return Tx(
        txid,
        at.blockhash,
        1,
        1_700_000_000,
        (TxIn(spent, 0xFFFFFFFD, PAY), TxIn(Outpoint(h(9), 0), 0)),
        (PAY, TxOut(0, "6a00", "nulldata")),
    )


def receive(height: int, txid: str = h(20), branch: int = 0) -> Activity:
    return Activity("receive", PAY.script_hex, txid, 0, 5000, block(height, branch).blockhash, height)


def spend(height: int, prevout: Outpoint) -> Activity:
    return Activity("spend", PAY.script_hex, h(21), 0, 5000, block(height).blockhash, height, prevout)


def test_a_cached_transaction_round_trips_with_confirmations_from_the_tip(conn: sqlite3.Connection) -> None:
    tx = tx_in(block(90))
    cc.put_tx(conn, tx, 90, TIP)
    cc.put_tx(conn, tx, 90, TIP)  # again: a no-op
    later = block(110)
    got = cc.get_tx(conn, tx.txid, block(90).blockhash, later)
    assert got is not None and got.confirmations == 21
    assert got.inputs == tx.inputs and got.outputs == tx.outputs and got.block_time == tx.block_time
    assert cc.get_tx(conn, tx.txid, block(91).blockhash, later) is None


def test_both_copies_of_a_duplicate_txid_are_kept_by_block_t208(conn: sqlite3.Connection) -> None:
    coinbase = (TxIn(None, 0xFFFFFFFF),)
    for height in (10, 20):
        at = block(height)
        cc.put_tx(conn, Tx(h(5), at.blockhash, 1, None, coinbase, (PAY,)), height, TIP)
    for height in (10, 20):
        got = cc.get_tx(conn, h(5), block(height).blockhash, TIP)
        assert got is not None and got.confirmations == TIP.height - height + 1 and got.coinbase


def test_only_confirmed_transactions_are_cached(conn: sqlite3.Connection) -> None:
    pending = Tx(h(7), None, 0, None, (TxIn(Outpoint(h(8), 0), 0),), (PAY,))
    with pytest.raises(ValueError, match="confirmed"):
        cc.put_tx(conn, pending, 90, TIP)


def test_a_write_against_a_tip_that_moved_is_refused_t207(conn: sqlite3.Connection) -> None:
    moved = block(100, branch=1)  # same height, another block: the tip changed during the fetch
    out = Outpoint(h(30), 0)
    with pytest.raises(StaleTipError):
        cc.put_tx(conn, tx_in(block(90)), 90, moved)
    with pytest.raises(StaleTipError):
        cc.put_spender(conn, out, SpentBy(h(31), block(95).blockhash, 95), moved)
    with pytest.raises(StaleTipError):
        cc.put_unspent(conn, out, moved)
    with pytest.raises(StaleTipError):
        cc.put_activity(conn, [receive(90)], moved)
    with pytest.raises(StaleTipError):
        cc.extend_coverage(conn, Coverage("raw(00)", 0, 90, block(90).blockhash), moved)
    for table in ("tx_cache", "spender", "snapshot", "activity", "coverage"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608 (fixed names)


def test_no_row_may_lie_above_the_tip_t207(conn: sqlite3.Connection) -> None:
    with pytest.raises(DbError, match="above the tip"):
        cc.put_tx(conn, tx_in(block(101)), 101, TIP)
    with pytest.raises(DbError, match="above the tip"):
        cc.put_spender(conn, Outpoint(h(30), 0), SpentBy(h(31), block(101).blockhash, 101), TIP)
    with pytest.raises(DbError, match="above the tip"):
        cc.put_activity(conn, [receive(90), receive(101, txid=h(22))], TIP)  # all or none
    with pytest.raises(DbError, match="above the tip"):
        cc.extend_coverage(conn, Coverage("raw(00)", 0, 101, block(101).blockhash), TIP)
    assert cc.activity_for(conn, PAY.script_hex) == []


def test_a_spend_replaces_the_unspent_snapshot_and_is_never_contradicted(conn: sqlite3.Connection) -> None:
    out = Outpoint(h(30), 0)
    cc.put_unspent(conn, out, TIP)
    assert cc.unspent_at(conn, out) == TIP
    by = SpentBy(h(31), block(100).blockhash, 100)
    cc.put_spender(conn, out, by, TIP)
    cc.put_spender(conn, out, by, TIP)  # again: a no-op
    assert cc.get_spender(conn, out) == by and cc.unspent_at(conn, out) is None
    with pytest.raises(DbError, match="two confirmed spends"):
        cc.put_spender(conn, out, SpentBy(h(32), block(100).blockhash, 100), TIP)
    with pytest.raises(DbError, match="can't be unspent"):
        cc.put_unspent(conn, out, TIP)


def test_an_unspent_snapshot_is_refreshed_at_the_new_tip(conn: sqlite3.Connection) -> None:
    out = Outpoint(h(30), 0)
    cc.put_unspent(conn, out, TIP)
    newer = block(105)
    set_tip(conn, newer)
    cc.put_unspent(conn, out, newer)
    assert cc.unspent_at(conn, out) == newer


def test_activity_is_recorded_once_and_read_oldest_first(conn: sqlite3.Connection) -> None:
    events = [spend(95, Outpoint(h(20), 0)), receive(90)]
    cc.put_activity(conn, events, TIP)
    cc.put_activity(conn, events, TIP)
    assert cc.activity_for(conn, PAY.script_hex) == [events[1], events[0]]
    assert cc.activity_for(conn, "0014" + "22" * 20) == []


@pytest.mark.parametrize(
    "event",
    [
        Activity("spend", PAY.script_hex, h(21), 0, 1, h(1), 1),  # a spend without its outpoint
        Activity("receive", PAY.script_hex, h(21), 0, 1, h(1), 1, Outpoint(h(20), 0)),
    ],
)
def test_only_a_spend_names_the_output_it_spent(conn: sqlite3.Connection, event: Activity) -> None:
    with pytest.raises(ValueError, match="only a spend"):
        cc.put_activity(conn, [event], TIP)


def test_the_schema_refuses_malformed_rows(conn: sqlite3.Connection) -> None:
    for bad in (
        Activity("receive", "0g", h(21), 0, 1, h(1), 1),
        Activity("receive", PAY.script_hex, "AB" * 32, 0, 1, h(1), 1),
        Activity("receive", PAY.script_hex, h(21), -1, 1, h(1), 1),
        Activity("receive", PAY.script_hex, h(21), 0, 21_000_000 * 100_000_000 + 1, h(1), 1),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            cc.put_activity(conn, [bad], TIP)


def test_coverage_grows_without_gaps_t210(conn: sqlite3.Connection) -> None:
    subject = "wpkh(tpub.../0/*)#range0-999"
    first = cc.extend_coverage(conn, Coverage(subject, 0, 49, block(49).blockhash), TIP)
    assert first == Coverage(subject, 0, 49, block(49).blockhash)
    grown = cc.extend_coverage(conn, Coverage(subject, 50, 99, block(99).blockhash), TIP)
    assert grown == cc.coverage(conn, subject) == Coverage(subject, 0, 99, block(99).blockhash)
    for start in (99, 101):  # an overlap or a gap
        with pytest.raises(DbError, match="without a gap"):
            cc.extend_coverage(conn, Coverage(subject, start, 100, TIP.blockhash), TIP)
    assert cc.coverage(conn, "raw(00)") is None


def test_a_reorg_removes_everything_above_the_fork_at_any_depth_t207(conn: sqlite3.Connection) -> None:
    keep, drop = Outpoint(h(40), 0), Outpoint(h(41), 0)
    cc.put_tx(conn, tx_in(block(50), txid=h(50)), 50, TIP)
    cc.put_tx(conn, tx_in(block(60), txid=h(60)), 60, TIP)
    cc.put_activity(conn, [receive(50, txid=h(51)), spend(70, Outpoint(h(52), 3))], TIP)
    cc.put_spender(conn, keep, SpentBy(h(42), block(55).blockhash, 55), TIP)
    cc.put_spender(conn, drop, SpentBy(h(43), block(80).blockhash, 80), TIP)
    cc.extend_coverage(conn, Coverage("early", 0, 40, block(40).blockhash), TIP)
    cc.extend_coverage(conn, Coverage("spans", 0, 90, block(90).blockhash), TIP)
    cc.extend_coverage(conn, Coverage("late", 60, 90, block(90).blockhash), TIP)
    old_snapshot = Outpoint(h(44), 0)
    set_tip(conn, block(55))
    cc.put_unspent(conn, old_snapshot, block(55))  # computed at a tip below the fork: kept
    set_tip(conn, TIP)
    cc.put_unspent(conn, Outpoint(h(45), 0), TIP)

    fork = block(55)  # 45 blocks deep
    gone = cc.invalidate_above(conn, fork)

    assert gone.fork == fork and last_tip(conn) == fork
    assert gone.txids == {h(60), h(21), h(52), h(41), h(43)}
    assert gone.unspent == {Outpoint(h(45), 0)}
    assert gone.subjects == {"spans", "late"}
    assert cc.get_tx(conn, h(50), block(50).blockhash, fork) is not None
    assert cc.get_tx(conn, h(60), block(60).blockhash, fork) is None
    assert cc.activity_for(conn, PAY.script_hex) == [receive(50, txid=h(51))]
    assert cc.get_spender(conn, keep) is not None and cc.get_spender(conn, drop) is None
    assert cc.unspent_at(conn, old_snapshot) == block(55)
    assert cc.unspent_at(conn, Outpoint(h(45), 0)) is None  # computed on the orphaned branch
    assert cc.coverage(conn, "early") == Coverage("early", 0, 40, block(40).blockhash)
    assert cc.coverage(conn, "spans") == Coverage("spans", 0, 55, fork.blockhash)  # stops at the fork block
    assert cc.coverage(conn, "late") is None
    # The new branch's rows can now be written against the new tip.
    new_tip = block(101, branch=1)
    set_tip(conn, new_tip)
    cc.put_activity(conn, [receive(60, txid=h(61), branch=1)], new_tip)


def test_a_fork_point_above_the_tip_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(DbError, match="at or below"):
        cc.invalidate_above(conn, block(101))
    assert last_tip(conn) == TIP


def test_a_failed_invalidation_changes_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    cc.put_activity(conn, [receive(90)], TIP)

    def fail(_conn: sqlite3.Connection, _tip: Tip) -> None:
        raise DbError("the tip couldn't be recorded")

    monkeypatch.setattr(cc, "set_tip", fail)
    with pytest.raises(DbError, match="couldn't be recorded"):
        cc.invalidate_above(conn, block(55))
    assert cc.activity_for(conn, PAY.script_hex) == [receive(90)] and last_tip(conn) == TIP


@pytest.mark.parametrize(
    "data",
    [
        "{}",
        '{"time":null,"in":[],"out":[],"extra":1}',
        '{"time":1.5,"in":[],"out":[]}',
        '{"time":null,"in":[[null,0,0,null]],"out":[]}',
        '{"time":null,"in":[["zz",0,0,null]],"out":[]}',
        '{"time":null,"in":[[null,null,4294967296,null]],"out":[]}',
        '{"time":null,"in":[],"out":[[-1,"00","x",null]]}',
        '{"time":null,"in":[],"out":[[true,"00","x",null]]}',
        '{"time":null,"in":[],"out":[[1,"0","x",null]]}',
        '{"time":null,"in":[],"out":[[1,"00",7,null]]}',
        '{"time":null,"in":[],"out":[[1,"00","x",7]]}',
        '{"time":null,"in":[],"out":[[1,"00","x"]]}',
    ],
)
def test_a_damaged_cache_row_is_refused_t408(conn: sqlite3.Connection, data: str) -> None:
    conn.execute(
        "INSERT INTO tx_cache (txid, blockhash, height, data) VALUES (?, ?, 90, ?)",
        (h(7), block(90).blockhash, data),
    )
    with pytest.raises(DbError, match="damaged"):
        cc.get_tx(conn, h(7), block(90).blockhash, TIP)


def test_a_cached_row_above_the_tip_it_is_read_at_is_refused(conn: sqlite3.Connection) -> None:
    cc.put_tx(conn, tx_in(block(90)), 90, TIP)
    with pytest.raises(DbError, match="above the tip"):
        cc.get_tx(conn, h(7), block(90).blockhash, block(89))

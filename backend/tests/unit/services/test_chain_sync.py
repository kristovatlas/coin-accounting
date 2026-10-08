"""The chain sync service against a fake node (architecture §8.1, §8.4; THREAT_MODEL T-207, T-210,
T-212)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import reorg, scans
from coinacct.chain.scans import Scan
from coinacct.rpc import RpcCallError
from coinacct.services import chain_sync
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import Tip, last_tip, record_chain, set_tip
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db

SPK = "0014" + "11" * 20


def bh(height: int) -> str:
    return f"{height:064x}"


class Node:
    """A single chain of `tip + 1` blocks; `hits` maps a height to the receives there."""

    def __init__(self, tip: int, hits: dict[int, int] | None = None) -> None:
        self.tip = tip
        self.hits = hits or {}
        self.syncing = False
        self.busy = False
        self.running = False
        self.filter_height = tip
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: str, params: Any = ()) -> Any:  # noqa: PLR0911 - one answer per RPC method
        self.calls.append((method, params))
        if method == "getblockchaininfo":
            return {"initialblockdownload": self.syncing, "blocks": self.tip, "headers": self.tip}
        if method == "getbestblockhash":
            return bh(self.tip)
        if method == "getblockheader":
            h = int(params[0], 16)
            prev = {"previousblockhash": bh(h - 1)} if h else {}
            return {"hash": params[0], "height": h, "confirmations": self.tip - h + 1, **prev}
        if method == "getblockhash":
            return bh(params[0])
        if method == "getindexinfo":
            return {scans.FILTER_INDEX: {"synced": True, "best_block_height": self.filter_height}}
        if method == "scanblocks":
            if params == ["status"]:
                return {"progress": 1, "current_height": 1} if self.running else None
            if params == ["abort"]:
                return self.running
            if self.busy:
                raise RpcCallError(method, -8, "Scan already in progress")
            _, _, start, stop, _ = params
            blocks = [bh(h) for h in sorted(self.hits) if start <= h <= stop]
            return {"from_height": start, "to_height": stop, "relevant_blocks": blocks, "completed": True}
        if method == "getdescriptoractivity":
            events = [
                {
                    "type": "receive",
                    "amount": Decimal("0.1"),
                    "blockhash": bh(h),
                    "height": h,
                    "txid": f"{n + 10**6:064x}",
                    "vout": 0,
                    "output_spk": {"hex": SPK},
                }
                for h, n in sorted(self.hits.items())
                if bh(h) in params[0]
            ]
            return {"activity": events}
        raise AssertionError(method)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


SUBJECT = Scan("s", ("addr(bcrt1qexample)",))


def test_a_sync_extends_every_subject_then_moves_the_last_seen_tip_t207(conn: sqlite3.Connection) -> None:
    node = Node(500, {10: 1, 450: 2})
    result = chain_sync.sync(node, conn, [SUBJECT, Scan("t", ("addr(bcrt1qother)",))])
    assert result.complete and result.target == Tip(bh(500), 500)
    assert last_tip(conn) == Tip(bh(500), 500) and cc.scan_target(conn) is None
    assert cc.coverage(conn, "s") == cc.Coverage("s", 0, 500, bh(500))
    assert [a.height for a in cc.activity_for(conn, SPK)] == [10, 450]
    assert chain_sync.sync(node, conn, [SUBJECT]) == chain_sync.SyncResult(None, None, complete=True)


def test_a_subject_that_must_wait_leaves_the_catch_up_unfinished_t210(conn: sqlite3.Connection) -> None:
    node = Node(500, {10: 1})
    node.busy = True  # another RPC user holds the scan slot
    result = chain_sync.sync(node, conn, [SUBJECT])
    assert not result.complete and result.waiting == ("s",)
    assert last_tip(conn) is None and cc.scan_target(conn) == Tip(bh(500), 500)  # retried next time
    node.busy = False
    again = chain_sync.sync(node, conn, [SUBJECT])
    assert again.complete and last_tip(conn) == Tip(bh(500), 500)


def test_a_lagging_filter_index_makes_the_subject_wait(conn: sqlite3.Connection) -> None:
    node = Node(500)
    node.filter_height = 300
    result = chain_sync.sync(node, conn, [SUBJECT])
    assert result.waiting == ("s",) and not result.complete


def test_a_subject_over_its_budget_is_reported_for_the_user_t205(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 100)
    node = Node(500, {h: h for h in range(10, 30)})
    result = chain_sync.sync(node, conn, [Scan("s", ("addr(bcrt1qexample)",), budget=5)])
    assert result.over_budget == ("s",) and not result.complete


def test_a_stale_range_is_retried_a_few_times(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def always_stale(*_args: Any) -> Any:
        calls.append(1)
        raise scans.StaleScanError("moved")

    monkeypatch.setattr(scans, "extend", always_stale)
    result = chain_sync.sync(Node(500), conn, [SUBJECT])
    assert result.waiting == ("s",) and len(calls) == chain_sync.RANGE_RETRIES


def test_a_scan_left_running_is_recovered_before_the_sync_t212(conn: sqlite3.Connection) -> None:
    node = Node(500)
    node.running = True
    cc.set_scan_marker(conn, "s")
    chain_sync.sync(node, conn, [])  # no subject to scan: only the sync's own recovery can clear it
    assert ("scanblocks", ["abort"]) in node.calls and cc.scan_marker(conn) is None


def test_a_reorg_reports_what_it_invalidated_t207(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = Node(500, {450: 2})
    chain_sync.sync(node, conn, [SUBJECT])
    # Replace blocks above 400 with a branch the node now prefers (another hash at each height).
    orphan = Tip(bh(500), 500)

    class Reorged(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader" and params[0] == orphan.blockhash:
                return {
                    "hash": orphan.blockhash,
                    "height": 500,
                    "confirmations": -1,
                    "previousblockhash": bh(499),
                }
            if method == "getblockheader" and int(params[0], 16) in range(401, 500):
                h = int(params[0], 16)
                return {"hash": params[0], "height": h, "confirmations": -1, "previousblockhash": bh(h - 1)}
            if method in ("getbestblockhash",):
                return f"{7 * 10**9 + 510:064x}"
            if method == "getblockheader" and params[0] == f"{7 * 10**9 + 510:064x}":
                return {"hash": params[0], "height": 510, "confirmations": 1, "previousblockhash": bh(400)}
            return super().call(method, params)

    # The new branch's own headers aren't modelled: the ancestry check is taken as passing.
    monkeypatch.setattr(reorg, "_descends", lambda *a: True)
    result = chain_sync.sync(Reorged(510), conn, [])
    assert result.change is not None and result.change.invalidated is not None
    assert result.change.invalidated.fork == Tip(bh(400), 400)
    assert result.invalidated_txids == {f"{2 + 10**6:064x}"}
    assert cc.activity_for(conn, SPK) == []


def test_start_up_recovers_then_catches_up_t212(conn: sqlite3.Connection) -> None:
    node = Node(500)
    node.running = True
    cc.set_scan_marker(conn, "s")
    assert chain_sync.at_startup(node, conn) is None
    assert cc.scan_marker(conn) is None and cc.scan_target(conn) == Tip(bh(500), 500)


def test_a_syncing_node_at_start_up_means_offline_mode(conn: sqlite3.Connection) -> None:
    node = Node(500)
    node.syncing = True
    reason = chain_sync.at_startup(node, conn)
    assert reason is not None and "syncing" in reason


def test_a_malformed_reply_at_start_up_means_offline_mode(conn: sqlite3.Connection) -> None:
    class Odd(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            return {} if method == "getblockchaininfo" else super().call(method, params)

    reason = chain_sync.at_startup(Odd(500), conn)
    assert reason is not None and "can't read" in reason


def test_a_tip_moving_during_start_up_is_left_to_the_poller(conn: sqlite3.Connection) -> None:
    set_tip(conn, Tip(bh(400), 400))

    class Moving(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader" and params[0] == bh(500):
                return {"hash": bh(500), "height": 500, "confirmations": -1, "previousblockhash": bh(499)}
            return super().call(method, params)

    assert chain_sync.at_startup(Moving(500), conn) is None
    assert last_tip(conn) == Tip(bh(400), 400)

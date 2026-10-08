"""The scan protocol against a real regtest node, as the app's whitelisted user (PLAN §1 "Scan
protocol"; THREAT_MODEL T-210, §7 "scans ... cover the full range").

The node's wallet is used only as the harness user, to make transactions to find; the app never uses
a wallet (ADR 0004). Synthetic regtest data only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import scans
from coinacct.chain.reorg import catch_up
from coinacct.chain.scans import Scan, extend
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import last_tip, record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-scans"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        n.mine(101, n.admin("getnewaddress"))
        yield n


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def test_a_scan_finds_every_receive_and_spend_across_ranges_and_the_tip_window_t210(
    node: RegtestNode, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 40)  # several ranges on a short chain
    watched = node.admin("getnewaddress")
    paid = []
    for amount in ("1.5", "0.25"):
        txid = node.admin("sendtoaddress", [watched, amount])
        [blockhash] = node.mine(1)
        paid.append((txid, blockhash))
        # Keep the wallet from spending the watched output on its own.
        utxo = next(u for u in node.admin("listunspent", [1, 9999999, [watched]]) if u["txid"] == txid)
        node.admin("lockunspent", [False, [{"txid": txid, "vout": utxo["vout"]}]])
        node.mine(60)  # both land below the tip window
    # Spend both watched outputs in one tx, then a last receive inside the tip window.
    node.admin("lockunspent", [True])
    utxos = node.admin("listunspent", [1, 9999999, [watched]])
    assert sorted(u["txid"] for u in utxos) == sorted(t for t, _ in paid)
    inputs = [{"txid": u["txid"], "vout": u["vout"]} for u in utxos]
    to = node.admin("getnewaddress")
    spend_txid = node.admin("sendall", [[to], None, "unset", None, {"inputs": inputs}])["txid"]
    [spend_block] = node.mine(1)
    late = node.admin("sendtoaddress", [watched, "0.1"])
    [late_block] = node.mine(1)

    rpc = app_client(node)
    change = catch_up(rpc, conn)
    assert change is not None
    subject = f"addr({watched})"
    calls: list[tuple[str, Any]] = []

    class Spy:
        def call(self, method: str, params: Any = ()) -> Any:
            calls.append((method, params))
            return rpc.call(method, params)

    reached = extend(Spy(), conn, Scan(subject, (subject,)))
    cc.complete_scan_target(conn, change.new)

    assert reached == cc.Coverage(subject, 0, change.new.height, change.new.blockhash)
    assert last_tip(conn) == change.new
    ranges = [(p[2], p[3]) for m, p in calls if m == "scanblocks"]
    stop = scans.stop_height(rpc, change.new)
    assert ranges == [(a, min(a + 39, stop)) for a in range(0, stop + 1, 40)]  # bounded, gap-free
    window = [p[0] for m, p in calls if m == "getdescriptoractivity"][-1]
    assert len(window) == change.new.height - stop  # the newest blocks, read directly
    events = cc.activity_for(conn, node.admin("getaddressinfo", [watched])["scriptPubKey"])
    receives = [(e.txid, e.blockhash) for e in events if e.kind == "receive"]
    spends = [(e.txid, e.blockhash) for e in events if e.kind == "spend"]
    assert receives == [*paid, (late, late_block)]
    assert spends == [(spend_txid, spend_block)] * 2  # both outputs spent by one tx
    assert {e.sats for e in events if e.kind == "receive"} == {150_000_000, 25_000_000, 10_000_000}
    assert {e.prevout.txid for e in events if e.prevout is not None} == {t for t, _ in paid}
    assert node.admin("getblockheader", [late_block])["height"] > scans.stop_height(rpc, change.new)


def test_a_second_scan_resumes_and_finds_only_new_activity(
    node: RegtestNode, conn: sqlite3.Connection
) -> None:
    rpc = app_client(node)
    watched = node.admin("getnewaddress")
    subject = f"addr({watched})"
    first = catch_up(rpc, conn)
    assert first is not None
    extend(rpc, conn, Scan(subject, (subject,)))
    cc.complete_scan_target(conn, first.new)
    txid = node.admin("sendtoaddress", [watched, "0.3"])
    node.mine(1)
    second = catch_up(rpc, conn)
    assert second is not None and second.invalidated is None
    calls: list[tuple[str, Any]] = []

    class Spy:
        def call(self, method: str, params: Any = ()) -> Any:
            calls.append((method, params))
            return rpc.call(method, params)

    reached = extend(Spy(), conn, Scan(subject, (subject,)))
    assert reached.stop_height == second.new.height
    # It resumed: nothing at or below the first scan's tip was scanned or read again.
    assert not [p for m, p in calls if m == "scanblocks" and p[2] <= first.new.height]
    read = {b for m, p in calls if m == "getdescriptoractivity" for b in p[0]}
    assert read == {second.new.blockhash}
    spk = node.admin("getaddressinfo", [watched])["scriptPubKey"]
    assert [e.txid for e in cc.activity_for(conn, spk)] == [txid]

"""Fork-point reorg handling against a fake node's block tree (PLAN §1 "Reorgs and new blocks";
THREAT_MODEL T-207)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import reorg
from coinacct.chain.reorg import MalformedHeaderError, catch_up, fork_point, node_tip
from coinacct.domain.chain import Outpoint
from coinacct.rpc import RpcCallError
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import Tip, last_tip, record_chain, set_tip
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db


def bh(height: int, branch: int = 0) -> str:
    return f"{branch << 32 | height:064x}"


class FakeNode:
    """Blocks 0..`main` on branch 0 are active; `side` adds an inactive branch 1 forking at
    `fork`, blocks fork+1..side."""

    def __init__(self, main: int, fork: int | None = None, side: int = 0) -> None:
        self.headers: dict[str, dict[str, Any]] = {}
        for h in range(main + 1):
            self.add(bh(h), h, main - h + 1, bh(h - 1) if h else None)
        if fork is not None:
            for h in range(fork + 1, side + 1):
                self.add(bh(h, 1), h, -1, bh(h - 1, 1) if h > fork + 1 else bh(fork))
        self.best = bh(main)
        self.calls: list[tuple[str, Any]] = []

    def add(self, blockhash: str, height: int, confirmations: int, prev: str | None) -> None:
        header: dict[str, Any] = {"hash": blockhash, "height": height, "confirmations": confirmations}
        if prev is not None:
            header["previousblockhash"] = prev
        self.headers[blockhash] = header

    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append((method, params))
        if method == "getbestblockhash":
            return self.best
        if method == "getblockhash":
            return bh(params[0])
        assert method == "getblockheader" and params[1] is True
        if params[0] not in self.headers:
            raise RpcCallError(method, -5, "Block not found")
        return self.headers[params[0]]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def test_an_active_tip_is_its_own_fork_point() -> None:
    node = FakeNode(main=10)
    assert fork_point(node, Tip(bh(7), 7)) == Tip(bh(7), 7)
    assert len(node.calls) == 1


def test_the_walk_finds_a_fork_at_any_depth_t207() -> None:
    node = FakeNode(main=300, fork=40, side=250)  # the recorded tip is 210 blocks past the fork
    assert fork_point(node, Tip(bh(250, 1), 250)) == Tip(bh(40), 40)
    assert len(node.calls) == 250 - 40 + 1


def test_an_unknown_recorded_tip_falls_back_to_genesis_t207() -> None:
    node = FakeNode(main=10)
    assert fork_point(node, Tip(bh(5, 9), 5)) == Tip(bh(0), 0)


def test_the_nodes_tip_must_be_active() -> None:
    node = FakeNode(main=10)
    assert node_tip(node) == Tip(bh(10), 10)
    node.headers[bh(10)]["confirmations"] = -1
    with pytest.raises(MalformedHeaderError, match="active chain"):
        node_tip(node)
    node.best = "zz"
    with pytest.raises(MalformedHeaderError, match="block hash"):
        node_tip(node)
    node.best = bh(99)
    with pytest.raises(MalformedHeaderError, match="own tip"):
        node_tip(node)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"hash": bh(3)}, "different block"),
        ({"height": 4}, "unexpected height"),
        ({"height": -1}, "report a height"),
        ({"height": True}, "report a height"),
        ({"confirmations": 0}, "confirmation"),
        ({"confirmations": "-1"}, "confirmation"),
        ({"previousblockhash": None}, "previous block"),
        ({"previousblockhash": "AB" * 32}, "previous block"),
    ],
)
def test_a_malformed_header_is_refused(change: dict[str, Any], message: str) -> None:
    node = FakeNode(main=10, fork=2, side=6)
    node.headers[bh(5, 1)].update(change)
    if change.get("previousblockhash", 0) is None:
        del node.headers[bh(5, 1)]["previousblockhash"]
    with pytest.raises(MalformedHeaderError, match=message):
        fork_point(node, Tip(bh(6, 1), 6))


def test_an_inactive_genesis_is_refused() -> None:
    node = FakeNode(main=3)
    node.headers[bh(0)]["confirmations"] = -1
    with pytest.raises(MalformedHeaderError, match="genesis"):
        fork_point(node, Tip(bh(0), 0))


def test_other_node_errors_propagate() -> None:
    class Down(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader":
                raise RpcCallError(method, -28, "Loading block index")
            return super().call(method, params)

    with pytest.raises(RpcCallError):
        fork_point(Down(main=3), Tip(bh(2), 2))


def test_the_first_tip_is_recorded_without_a_walk(conn: sqlite3.Connection) -> None:
    node = FakeNode(main=10)
    change = catch_up(node, conn)
    assert change == reorg.TipChange(None, Tip(bh(10), 10), None)
    assert cc.scan_target(conn) == Tip(bh(10), 10) and last_tip(conn) is None  # not caught up yet
    assert catch_up(node, conn) is None  # nothing moved
    cc.complete_scan_target(conn, Tip(bh(10), 10))
    assert last_tip(conn) == Tip(bh(10), 10) and catch_up(node, conn) is None


def test_new_blocks_move_the_tip_and_keep_the_cache(conn: sqlite3.Connection) -> None:
    set_tip(conn, Tip(bh(10), 10))
    cc.put_unspent(conn, Outpoint(bh(1), 0), Tip(bh(10), 10))
    change = catch_up(FakeNode(main=12), conn)
    assert change is not None and change.invalidated is None and change.new == Tip(bh(12), 12)
    assert cc.unspent_at(conn, Outpoint(bh(1), 0)) == Tip(bh(10), 10)


def test_a_reorg_invalidates_above_the_fork_and_records_the_new_tip_t207(conn: sqlite3.Connection) -> None:
    old = Tip(bh(9, 1), 9)
    set_tip(conn, old)
    kept = cc.Activity("receive", "00", bh(70), 0, 1, bh(4), 4)
    gone = cc.Activity("receive", "00", bh(71), 0, 1, bh(8, 1), 8)
    cc.put_activity(conn, [kept, gone], old)
    node = FakeNode(main=12, fork=5, side=9)  # the recorded tip is on branch 1, now inactive
    change = catch_up(node, conn)
    assert change is not None and change.old == old and change.new == Tip(bh(12), 12)
    assert change.invalidated is not None and change.invalidated.fork == Tip(bh(5), 5)
    assert change.invalidated.txids == {bh(71)}
    assert cc.activity_for(conn, "00") == [kept]
    assert cc.scan_target(conn) == Tip(bh(12), 12) and last_tip(conn) == Tip(bh(5), 5)


def test_an_unfinished_catch_up_is_walked_back_from_its_target_t207(conn: sqlite3.Connection) -> None:
    set_tip(conn, Tip(bh(4), 4))
    target = Tip(bh(9, 1), 9)
    cc.set_scan_target(conn, target)  # a catch-up wrote rows towards this, then the app stopped
    cc.put_activity(conn, [cc.Activity("receive", "00", bh(72), 0, 1, bh(8, 1), 8)], target)
    change = catch_up(FakeNode(main=12, fork=5, side=9), conn)
    assert change is not None and change.old == target
    assert change.invalidated is not None and change.invalidated.fork == Tip(bh(5), 5)
    assert cc.activity_for(conn, "00") == []


def test_a_tip_moved_by_another_caller_meanwhile_is_left_alone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_tip(conn, Tip(bh(9, 1), 9))
    node = FakeNode(main=12, fork=5, side=9)
    walk = reorg.fork_point

    def walk_then_move(rpc: Any, tip: Tip) -> Tip:
        fork = walk(rpc, tip)
        set_tip(conn, Tip(bh(11), 11))  # someone else recorded a tip during the walk
        return fork

    monkeypatch.setattr(reorg, "fork_point", walk_then_move)
    assert catch_up(node, conn) is None
    assert last_tip(conn) == Tip(bh(11), 11) and cc.scan_target(conn) is None


def test_a_failed_invalidation_leaves_the_old_tip(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = Tip(bh(9, 1), 9)
    set_tip(conn, old)

    def fail(_conn: Any, _fork: Tip) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(reorg, "invalidate_above", fail)
    with pytest.raises(RuntimeError):
        catch_up(FakeNode(main=12, fork=5, side=9), conn)
    assert last_tip(conn) == old and cc.scan_target(conn) is None

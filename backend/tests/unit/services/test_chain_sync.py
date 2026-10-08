"""The chain sync service against a fake node (architecture §8.1, §8.4; THREAT_MODEL T-207, T-210,
T-212)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import scans
from coinacct.chain.scans import Scan
from coinacct.rpc import RpcCallError, RpcForbiddenError, RpcTransportError
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
    again = chain_sync.sync(node, conn, [SUBJECT])  # nothing moved: nothing to catch up
    assert again.change is None and again.complete and again.target == Tip(bh(500), 500)
    assert again.pending == ()  # the mempool pass is rebuilt every time


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


class Reorged(Node):
    """Blocks up to 400 as before; above them a new branch (other hashes) up to 510, and the old
    branch's blocks 401..500 known but inactive."""

    @staticmethod
    def new(h: int) -> str:
        return bh(h) if h <= 400 else f"{7 * 10**9 + h:064x}"

    def call(self, method: str, params: Any = ()) -> Any:
        if method == "getbestblockhash":
            return self.new(self.tip)
        if method == "getblockhash":
            return self.new(params[0])
        if method == "getblockheader":
            value = int(params[0], 16)
            if 400 < value <= 500:  # an old-branch block
                return {
                    "hash": params[0],
                    "height": value,
                    "confirmations": -1,
                    "previousblockhash": bh(value - 1),
                }
            if value > 7 * 10**9:
                h = value - 7 * 10**9
                prev = self.new(h - 1)
                return {
                    "hash": params[0],
                    "height": h,
                    "confirmations": self.tip - h + 1,
                    "previousblockhash": prev,
                }
        if method == "getdescriptoractivity":
            hits = [h for h in self.hits if self.new(h) in params[0]]
            return {
                "activity": [
                    {
                        "type": "receive",
                        "amount": Decimal("0.1"),
                        "blockhash": self.new(h),
                        "height": h,
                        "txid": f"{self.hits[h] + 10**6:064x}",
                        "vout": 0,
                        "output_spk": {"hex": SPK},
                    }
                    for h in hits
                ]
            }
        return super().call(method, params)


def test_a_reorg_is_kept_for_review_until_flagged_t207(conn: sqlite3.Connection) -> None:
    chain_sync.sync(Node(500, {450: 2}), conn, [SUBJECT])
    result = chain_sync.sync(Reorged(510, {300: 1}), conn, [SUBJECT])
    assert result.complete and result.change is not None and result.change.invalidated is not None
    assert result.change.invalidated.fork == Tip(bh(400), 400)
    gone = f"{2 + 10**6:064x}"
    assert gone in result.to_review and cc.pending_review(conn) == result.to_review
    assert cc.activity_for(conn, SPK) == []  # the old branch's receive is gone; block 300 had none before
    # A later sync still reports it, until the events are flagged.
    assert gone in chain_sync.sync(Reorged(510), conn, [SUBJECT]).to_review
    cc.clear_review(conn, [gone])
    assert chain_sync.sync(Reorged(510), conn, [SUBJECT]).to_review == frozenset()


def test_a_reorg_survives_a_sync_that_fails_after_it_t207(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain_sync.sync(Node(500, {450: 2}), conn, [SUBJECT])

    def lost(*_args: Any) -> Any:
        raise RpcTransportError("getdescriptoractivity: connection reset")

    monkeypatch.setattr(scans, "extend", lost)
    with pytest.raises(RpcTransportError):
        chain_sync.sync(Reorged(510), conn, [SUBJECT])
    assert f"{2 + 10**6:064x}" in cc.pending_review(conn)  # never lost


def test_a_reorg_found_at_start_up_is_kept_for_review_t207(conn: sqlite3.Connection) -> None:
    chain_sync.sync(Node(500, {450: 2}), conn, [SUBJECT])
    assert chain_sync.at_startup(Reorged(510), conn) is None
    assert f"{2 + 10**6:064x}" in chain_sync.sync(Reorged(510), conn, [SUBJECT]).to_review


@pytest.mark.parametrize(
    "error", [RpcTransportError("down"), RpcForbiddenError("getblockheader"), RpcCallError("x", -1, "boom")]
)
def test_a_node_error_during_the_start_up_catch_up_means_offline_mode(
    conn: sqlite3.Connection, error: Exception
) -> None:
    class Failing(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader":
                raise error
            return super().call(method, params)

    reason = chain_sync.at_startup(Failing(500), conn)
    assert reason == f"the chain catch-up at start-up failed ({type(error).__name__})"
    assert "boom" not in reason  # never the node's message (T-201)


def test_an_aborted_range_is_retried(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    real = scans.extend
    attempts = []

    def aborted_once(*args: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise scans.ScanAbortedError("another client aborted it")
        return real(*args)

    monkeypatch.setattr(scans, "extend", aborted_once)
    assert chain_sync.sync(Node(500), conn, [SUBJECT]).complete and len(attempts) == 2


def test_the_chain_moving_stops_the_sync_without_retrying(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = []

    def moved(*_args: Any) -> Any:
        attempts.append(1)
        raise scans.ChainMovedError("catch up first")

    monkeypatch.setattr(scans, "extend", moved)
    result = chain_sync.sync(Node(500), conn, [SUBJECT, Scan("t", ("addr(bcrt1qother)",))])
    assert result.waiting == ("s", "t") and not result.complete and attempts == [1]


def test_a_subject_whose_history_starts_above_the_tip_has_nothing_to_scan(conn: sqlite3.Connection) -> None:
    result = chain_sync.sync(Node(500), conn, [Scan("later", ("addr(bcrt1qlater)",), start_height=900)])
    assert result.complete and cc.coverage(conn, "later") is None


def test_a_sync_with_no_tip_change_still_extends_a_new_subject(conn: sqlite3.Connection) -> None:
    node = Node(500, {10: 1})
    chain_sync.sync(node, conn, [])
    result = chain_sync.sync(node, conn, [SUBJECT])  # the subject was added after the catch-up
    assert result.change is None and result.complete
    assert cc.coverage(conn, "s") == cc.Coverage("s", 0, 500, bh(500))


def test_a_marker_set_meanwhile_is_recovered_and_the_range_retried_t212(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = scans.extend
    node = Node(500)
    calls = []

    def in_flight_once(*args: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            cc.set_scan_marker(conn, "s")
            node.running = True
            raise scans.ScanInFlightError("recover first")
        return real(*args)

    monkeypatch.setattr(scans, "extend", in_flight_once)
    assert chain_sync.sync(node, conn, [SUBJECT]).complete and cc.scan_marker(conn) is None


def test_the_mempool_pass_is_rebuilt_before_the_tip_moves_t207(conn: sqlite3.Connection) -> None:
    class WithMempool(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getdescriptoractivity" and params[0] == [] and params[2] is True:
                return {
                    "activity": [
                        {
                            "type": "receive",
                            "amount": Decimal("0.2"),
                            "txid": "ee" * 32,
                            "vout": 1,
                            "output_spk": {"hex": SPK},
                        }
                    ]
                }
            return super().call(method, params)

    result = chain_sync.sync(WithMempool(500), conn, [SUBJECT])
    assert result.complete and result.pending is not None and [p.txid for p in result.pending] == ["ee" * 32]


def test_a_tip_moving_during_the_mempool_pass_leaves_the_catch_up_unfinished(
    conn: sqlite3.Connection,
) -> None:
    class Moves(Node):
        def __init__(self, tip: int) -> None:
            super().__init__(tip)
            self.bests = 0

        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getdescriptoractivity" and params[0] == []:
                self.tip += 1  # a block arrives during the pass
            return super().call(method, params)

    result = chain_sync.sync(Moves(500), conn, [SUBJECT])
    assert not result.complete and result.pending is None and last_tip(conn) is None


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
    assert reason == "the chain catch-up at start-up failed (MalformedHeaderError)"


def test_a_tip_moving_during_start_up_is_left_to_the_poller(conn: sqlite3.Connection) -> None:
    set_tip(conn, Tip(bh(400), 400))

    class Moving(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader" and params[0] == bh(500):
                return {"hash": bh(500), "height": 500, "confirmations": -1, "previousblockhash": bh(499)}
            return super().call(method, params)

    assert chain_sync.at_startup(Moving(500), conn) is None
    assert last_tip(conn) == Tip(bh(400), 400)


def test_a_tip_that_moves_once_during_start_up_is_caught_up_on_the_retry(conn: sqlite3.Connection) -> None:
    class MovesOnce(Node):
        def __init__(self, tip: int) -> None:
            super().__init__(tip)
            self.first = True

        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader" and params[0] == bh(self.tip) and self.first:
                self.first = False  # reorged away between getbestblockhash and getblockheader
                return {
                    "hash": params[0],
                    "height": self.tip,
                    "confirmations": -1,
                    "previousblockhash": bh(self.tip - 1),
                }
            return super().call(method, params)

    assert chain_sync.at_startup(MovesOnce(500), conn) is None
    assert cc.scan_target(conn) == Tip(bh(500), 500)  # the retry caught up

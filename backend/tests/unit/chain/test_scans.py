"""The scan protocol against a fake node (PLAN §1 "Scan protocol"; THREAT_MODEL T-205, T-207, T-210,
T-212)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import scans
from coinacct.chain.scans import (
    ActivityBudgetError,
    MalformedScanError,
    Scan,
    ScanAbortedError,
    ScanBusyError,
    StaleScanError,
    activity,
    extend,
    recover,
    scan_range,
    stop_height,
)
from coinacct.domain.chain import Outpoint
from coinacct.rpc import RpcCallError, RpcResponseTooLargeError, RpcTransportError
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import Tip, record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db

SPK = "0014" + "11" * 20
DESC = ["addr(bcrt1qexample)"]


def bh(height: int) -> str:
    return f"{height:064x}"


def txid(n: int) -> str:
    return f"{n + 10**6:064x}"


class FakeNode:
    """A chain of `tip + 1` blocks; `hits` maps a height to the events Core would report there."""

    def __init__(
        self, tip: int, hits: dict[int, list[dict[str, Any]]], filter_height: int | None = None
    ) -> None:
        self.tip = tip
        self.hits = hits
        self.filter_height = tip if filter_height is None else filter_height
        self.calls: list[tuple[str, Any]] = []
        self.scan_reply: Any = None  # overrides the honest reply
        self.after_scan: Any = None  # run after each scanblocks, e.g. to move the chain
        self.running = False  # what `scanblocks abort` reports
        self.conn: sqlite3.Connection | None = None
        self.markers: list[str | None] = []  # the in-flight marker at each scanblocks start

    def hash_at(self, height: int) -> str:
        return bh(height)

    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append((method, params))
        if method == "getindexinfo":
            return {scans.FILTER_INDEX: {"synced": True, "best_block_height": self.filter_height}}
        if method == "getblockhash":
            return self.hash_at(params[0])
        if method == "scanblocks" and params == ["status"]:
            return {"progress": 40, "current_height": 7} if self.running else None
        if method == "scanblocks" and params == ["abort"]:
            return self.running
        if method == "scanblocks":
            if self.conn is not None:
                self.markers.append(cc.scan_marker(self.conn))
            action, objects, start, stop, kind = params
            assert action == "start" and objects == DESC and kind == "basic"
            reply = (
                self.scan_reply
                if self.scan_reply is not None
                else {
                    "from_height": start,
                    "to_height": stop,
                    "relevant_blocks": [self.hash_at(h) for h in sorted(self.hits) if start <= h <= stop],
                    "completed": True,
                }
            )
            if self.after_scan:
                self.after_scan()
            return reply
        if method == "getdescriptoractivity":
            blocks, objects, mempool = params
            assert objects == DESC and mempool is False
            assert len(blocks) <= scans.ACTIVITY_BLOCKS
            events = []
            for h, evs in sorted(self.hits.items()):
                if self.hash_at(h) in blocks:
                    events += [{"blockhash": self.hash_at(h), "height": h, **e} for e in evs]
            return {"activity": events}
        raise AssertionError(method)


def receive(n: int, sats: str = "0.5") -> dict[str, Any]:
    return {
        "type": "receive",
        "amount": Decimal(sats),
        "txid": txid(n),
        "vout": 0,
        "output_spk": {"hex": SPK},
    }


def spend(n: int, prev: int) -> dict[str, Any]:
    return {
        "type": "spend",
        "amount": Decimal("0.5"),
        "spend_txid": txid(n),
        "spend_vin": 1,
        "prevout_txid": txid(prev),
        "prevout_vout": 0,
        "prevout_spk": {"hex": SPK},
    }


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def target(conn: sqlite3.Connection, node: FakeNode) -> Tip:
    tip = Tip(bh(node.tip), node.tip)
    cc.set_scan_target(conn, tip)
    return tip


def test_the_stop_height_stays_below_the_filter_index_and_the_tip_window_t210() -> None:
    assert stop_height(FakeNode(1000, {}), Tip(bh(1000), 1000)) == 900
    assert stop_height(FakeNode(1000, {}, filter_height=850), Tip(bh(1000), 1000)) == 850
    assert stop_height(FakeNode(50, {}), Tip(bh(50), 50)) == -50


def test_a_scan_covers_every_block_once_with_the_tip_window_read_directly_t210(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 300)
    node = FakeNode(1000, {5: [receive(1)], 650: [spend(2, 1)], 950: [receive(3)], 1000: [receive(4)]})
    at = target(conn, node)
    reached = extend(node, conn, Scan("s", tuple(DESC)))
    assert reached == cc.Coverage("s", 0, 1000, at.blockhash)
    ranges = [(p[2], p[3]) for m, p in node.calls if m == "scanblocks"]
    assert ranges == [(0, 299), (300, 599), (600, 899), (900, 900)]  # bounded, contiguous, stop = tip - 100
    window = [p[0] for m, p in node.calls if m == "getdescriptoractivity"][-1]
    assert window == [bh(h) for h in range(901, 1001)]  # the newest blocks never go through scanblocks
    got = cc.activity_for(conn, SPK)
    assert [(a.kind, a.height) for a in got] == [
        ("receive", 5),
        ("spend", 650),
        ("receive", 950),
        ("receive", 1000),
    ]
    assert got[1].prevout == Outpoint(txid(1), 0) and got[1].n == 1 and got[0].sats == 50_000_000


def test_a_scan_resumes_after_its_coverage(conn: sqlite3.Connection) -> None:
    node = FakeNode(300, {10: [receive(1)], 250: [receive(2)]})
    at = target(conn, node)
    cc.extend_coverage(conn, cc.Coverage("s", 0, 150, bh(150)), at)
    extend(node, conn, Scan("s", tuple(DESC)))
    assert [(p[2], p[3]) for m, p in node.calls if m == "scanblocks"] == [(151, 200)]
    assert [a.height for a in cc.activity_for(conn, SPK)] == [250]


def test_a_young_chain_is_read_entirely_through_the_tip_window(conn: sqlite3.Connection) -> None:
    node = FakeNode(30, {0: [receive(1)], 30: [receive(2)]})
    target(conn, node)
    extend(node, conn, Scan("s", tuple(DESC)))
    assert not [m for m, _ in node.calls if m == "scanblocks"]
    assert [a.height for a in cc.activity_for(conn, SPK)] == [0, 30]


def test_activity_is_read_in_bounded_calls_t205(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "ACTIVITY_BLOCKS", 3)
    node = FakeNode(1000, {h: [receive(h)] for h in range(10, 20)})
    events = activity(node, [bh(h) for h in range(10, 20)], DESC)
    assert len(events) == 10
    assert [len(p[0]) for m, p in node.calls if m == "getdescriptoractivity"] == [3, 3, 3, 1]


def test_a_chain_that_moved_during_a_range_discards_it_t210(conn: sqlite3.Connection) -> None:
    node = FakeNode(1000, {5: [receive(1)]})
    target(conn, node)

    def reorg() -> None:
        node.hash_at = lambda h: bh(h) if h < 800 else f"{h + 7 * 10**9:064x}"  # type: ignore[method-assign,assignment]

    node.after_scan = reorg
    with pytest.raises(StaleScanError):
        extend(node, conn, Scan("s", tuple(DESC)))
    assert cc.activity_for(conn, SPK) == [] and cc.coverage(conn, "s") is None


def test_a_filter_index_that_fell_behind_during_a_range_discards_it_t210(conn: sqlite3.Connection) -> None:
    node = FakeNode(1000, {5: [receive(1)]})
    target(conn, node)

    def fall_behind() -> None:
        node.filter_height = 500

    node.after_scan = fall_behind
    with pytest.raises(StaleScanError):
        extend(node, conn, Scan("s", tuple(DESC)))
    assert cc.coverage(conn, "s") is None


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ({"from_height": 0, "to_height": 900, "relevant_blocks": [], "completed": False}, ScanAbortedError),
        ({"from_height": 0, "to_height": 900, "relevant_blocks": []}, MalformedScanError),
        ({"from_height": 1, "to_height": 900, "relevant_blocks": [], "completed": True}, MalformedScanError),
        ({"from_height": 0, "to_height": 899, "relevant_blocks": [], "completed": True}, MalformedScanError),
        (
            {"from_height": 0, "to_height": 900, "relevant_blocks": ["zz"], "completed": True},
            MalformedScanError,
        ),
        (
            {"from_height": 0, "to_height": 900, "relevant_blocks": [bh(1), bh(1)], "completed": True},
            MalformedScanError,
        ),
        ([], MalformedScanError),
    ],
)
def test_a_bad_scan_reply_is_refused(reply: Any, error: type[Exception]) -> None:
    node = FakeNode(1000, {})
    node.scan_reply = reply
    with pytest.raises(error):
        scan_range(node, DESC, 0, 900, Tip(bh(900), 900))


@pytest.mark.parametrize("state", [{"best_block_height": True}, {"best_block_height": "900"}, {}, None])
def test_a_malformed_filter_index_reply_is_refused(state: Any) -> None:
    class Odd(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getindexinfo":
                return {scans.FILTER_INDEX: state}
            return super().call(method, params)

    with pytest.raises(MalformedScanError, match="filter index"):
        stop_height(Odd(1000, {}), Tip(bh(1000), 1000))


def test_a_scan_range_is_bounded() -> None:
    with pytest.raises(ValueError, match="RANGE_BLOCKS"):
        scan_range(
            FakeNode(10**6, {}), DESC, 0, scans.RANGE_BLOCKS, Tip(bh(scans.RANGE_BLOCKS), scans.RANGE_BLOCKS)
        )
    with pytest.raises(ValueError):
        scan_range(FakeNode(10, {}), DESC, 5, 4, Tip(bh(4), 4))


def test_a_busy_scan_queue_is_its_own_error_t212() -> None:
    class Busy(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "scanblocks":
                raise RpcCallError(method, -8, 'Scan already in progress, use action "abort" or "status"')
            return super().call(method, params)

    with pytest.raises(ScanBusyError):
        scan_range(Busy(1000, {}), DESC, 0, 900, Tip(bh(900), 900))


@pytest.mark.parametrize(
    ("code", "message", "error"),
    [
        (-8, "Block is not in main chain", StaleScanError),
        (-5, "Block not found", StaleScanError),
        (-8, "Invalid parameter", RpcCallError),
        (-1, "something else", RpcCallError),
    ],
)
def test_a_block_that_left_the_chain_means_rescan_anything_else_is_hard(
    code: int, message: str, error: type[Exception]
) -> None:
    class Failing(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getdescriptoractivity":
                raise RpcCallError(method, code, message)
            return super().call(method, params)

    with pytest.raises(error):
        activity(Failing(10, {}), [bh(1)], DESC)


@pytest.mark.parametrize(
    "change",
    [
        {"type": "other"},
        {"amount": 0.5},
        {"amount": None},
        {"amount": Decimal("0.000000001")},
        {"blockhash": "zz"},
        {"height": -1},
        {"height": True},
        {"txid": "zz"},
        {"vout": -1},
        {"output_spk": {}},
        {"output_spk": {"hex": "0"}},
    ],
)
def test_a_malformed_receive_is_refused_t205(change: dict[str, Any]) -> None:
    class Odd(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            reply = super().call(method, params)
            if method == "getdescriptoractivity":
                reply["activity"][0].update(change)
            return reply

    with pytest.raises(MalformedScanError):
        activity(Odd(10, {3: [receive(1)]}), [bh(3)], DESC)


@pytest.mark.parametrize("change", [{"prevout_txid": None}, {"prevout_vout": -1}, {"spend_vin": "1"}])
def test_a_malformed_spend_is_refused_t205(change: dict[str, Any]) -> None:
    event = {**spend(2, 1), **change}
    with pytest.raises(MalformedScanError):
        activity(FakeNode(10, {3: [event]}), [bh(3)], DESC)


def test_an_event_from_a_block_not_asked_for_is_refused() -> None:
    odd = FakeNode(10, {3: [{**receive(1), "blockhash": bh(9)}]})
    with pytest.raises(MalformedScanError, match="wasn't asked for"):
        activity(odd, [bh(3)], DESC)


def test_an_event_outside_its_range_is_refused(conn: sqlite3.Connection) -> None:
    # A node consistent with itself (block 20 is also "at" 40) but not with the range asked for.
    node = FakeNode(30, {20: [{**receive(1), "height": 40}]})
    node.hash_at = lambda h: bh(20) if h == 40 else bh(h)  # type: ignore[method-assign,assignment]
    target(conn, node)
    with pytest.raises(MalformedScanError, match="outside the range"):
        extend(node, conn, Scan("s", tuple(DESC)))
    assert cc.coverage(conn, "s") is None


def test_a_scan_needs_a_target(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="catch up first"):
        extend(FakeNode(10, {}), conn, Scan("s", tuple(DESC)))


def test_a_target_that_left_the_chain_stops_the_tip_window(conn: sqlite3.Connection) -> None:
    node = FakeNode(30, {})
    cc.set_scan_target(conn, Tip(f"{30 + 7 * 10**9:064x}", 30))  # not the node's block 30
    with pytest.raises(StaleScanError, match="target"):
        extend(node, conn, Scan("s", tuple(DESC)))


def test_a_busy_script_stops_at_its_budget_before_reading_blocks_t205(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 300)
    node = FakeNode(1000, {h: [receive(h)] for h in [*range(10, 15), *range(400, 410)]})
    target(conn, node)
    with pytest.raises(ActivityBudgetError) as e:
        extend(node, conn, Scan("s", tuple(DESC), budget=12))
    assert (e.value.candidates, e.value.budget) == (15, 12)
    # The first range (5 candidates) is committed; the second's blocks were never read.
    assert cc.coverage(conn, "s") == cc.Coverage("s", 0, 299, bh(299))
    read = [b for m, p in node.calls if m == "getdescriptoractivity" for b in p[0]]
    assert read == [bh(h) for h in range(10, 15)]
    # After the user agrees, the scan resumes without a budget.
    reached = extend(node, conn, Scan("s", tuple(DESC), budget=None))
    assert reached.stop_height == 1000 and len(cc.activity_for(conn, SPK)) == 15


def test_a_scan_within_its_budget_runs_to_the_end(conn: sqlite3.Connection) -> None:
    node = FakeNode(1000, {h: [receive(h)] for h in range(10, 15)})
    target(conn, node)
    assert extend(node, conn, Scan("s", tuple(DESC), budget=5)).stop_height == 1000


def test_each_scan_call_runs_inside_the_in_flight_marker_t212(conn: sqlite3.Connection) -> None:
    node = FakeNode(1000, {5: [receive(1)]})
    node.conn = conn
    target(conn, node)
    extend(node, conn, Scan("s", tuple(DESC)))
    assert node.markers == ["s"] and cc.scan_marker(conn) is None


def test_a_node_error_clears_the_marker_a_lost_connection_keeps_it_t212(conn: sqlite3.Connection) -> None:
    class Busy(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "scanblocks":
                raise RpcCallError(method, -8, "Scan already in progress")
            return super().call(method, params)

    class Lost(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "scanblocks":
                raise RpcTransportError("scanblocks: timed out")
            return super().call(method, params)

    target(conn, FakeNode(1000, {}))
    with pytest.raises(ScanBusyError):
        extend(Busy(1000, {}), conn, Scan("s", tuple(DESC)))
    assert cc.scan_marker(conn) is None  # the node answered: nothing of ours is running
    with pytest.raises(RpcTransportError):
        extend(Lost(1000, {}), conn, Scan("s", tuple(DESC)))
    assert cc.scan_marker(conn) == "s"  # the node may still be scanning


@pytest.mark.parametrize("running", [True, False])
def test_recovery_aborts_a_scan_the_app_left_running_t212(conn: sqlite3.Connection, running: bool) -> None:
    node = FakeNode(10, {})
    node.running = running
    assert recover(node, conn) is False and node.calls == []  # no marker: never touch others' scans
    cc.set_scan_marker(conn, "s")
    assert recover(node, conn) is running
    assert node.calls[0] == ("scanblocks", ["status"])  # §8.1: status, then abort only if running
    assert (("scanblocks", ["abort"]) in node.calls) is running and cc.scan_marker(conn) is None


def test_a_malformed_abort_reply_keeps_the_marker(conn: sqlite3.Connection) -> None:
    node = FakeNode(10, {})
    node.running = True

    class Odd(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            return "yes" if params == ["abort"] else super().call(method, params)

    node = Odd(10, {})
    node.running = True
    cc.set_scan_marker(conn, "s")
    with pytest.raises(MalformedScanError):
        recover(node, conn)
    assert cc.scan_marker(conn) == "s"


def test_every_range_is_guarded_by_the_same_stop_block_t210(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 300)
    node = FakeNode(1000, {5: [receive(1)], 400: [receive(2)]})
    target(conn, node)
    flips = iter([None, "flip"])

    def switch_after_the_first_range() -> None:
        if next(flips, None):  # the node is on another branch below S during the second range
            node.hash_at = lambda h: bh(h) if h < 350 else f"{h + 7 * 10**9:064x}"  # type: ignore[method-assign,assignment]

    node.after_scan = switch_after_the_first_range
    with pytest.raises(StaleScanError):
        extend(node, conn, Scan("s", tuple(DESC)))
    assert cc.coverage(conn, "s") == cc.Coverage("s", 0, 299, bh(299))  # the second range wasn't committed
    guards = [p[0] for m, p in node.calls if m == "getblockhash"]
    assert guards.count(900) >= 3  # S read once up front, then re-checked after each range


def test_an_event_whose_block_isnt_at_its_height_is_stale_t207() -> None:
    odd = FakeNode(10, {3: [{**receive(1), "height": 4}]})
    with pytest.raises(StaleScanError, match="at its height"):
        activity(odd, [bh(3)], DESC)


def test_a_chain_shorter_than_the_target_is_stale_not_an_error(conn: sqlite3.Connection) -> None:
    class Shorter(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockhash" and params[0] > 25:
                raise RpcCallError(method, -8, "Block height out of range")
            return super().call(method, params)

    node = Shorter(30, {})
    target(conn, node)
    with pytest.raises(StaleScanError, match="shorter"):
        extend(node, conn, Scan("s", tuple(DESC)))


@pytest.mark.parametrize("message", ["Invalid descriptor", "Block not found"])
def test_only_a_missing_block_is_stale_t210(message: str) -> None:
    class Failing(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getdescriptoractivity":
                raise RpcCallError(method, -5, message)
            return super().call(method, params)

    with pytest.raises(StaleScanError if message == "Block not found" else RpcCallError):
        activity(Failing(10, {}), [bh(1)], DESC)


def test_a_filter_index_far_behind_is_waited_for_not_read_directly_t210(conn: sqlite3.Connection) -> None:
    node = FakeNode(1000, {5: [receive(1)], 700: [receive(2)]}, filter_height=600)
    target(conn, node)
    with pytest.raises(scans.FilterIndexBehindError):
        extend(node, conn, Scan("s", tuple(DESC)))
    assert cc.coverage(conn, "s") == cc.Coverage("s", 0, 600, bh(600))  # what the index reached
    assert all(len(p[0]) < 200 for m, p in node.calls if m == "getdescriptoractivity")


def test_the_tip_window_is_read_in_bounded_calls_t205(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "ACTIVITY_BLOCKS", 30)
    node = FakeNode(80, {h: [receive(h)] for h in (0, 40, 80)})
    target(conn, node)
    extend(node, conn, Scan("s", tuple(DESC)))
    assert [len(p[0]) for m, p in node.calls if m == "getdescriptoractivity"] == [30, 30, 21]
    assert [a.height for a in cc.activity_for(conn, SPK)] == [0, 40, 80]


def test_the_budget_counts_the_subjects_whole_history_across_calls_t205(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 300)
    node = FakeNode(1000, {h: [receive(h)] for h in [*range(10, 15), *range(400, 410)]})
    target(conn, node)
    with pytest.raises(ActivityBudgetError):
        extend(node, conn, Scan("s", tuple(DESC), budget=12))
    with pytest.raises(ActivityBudgetError) as e:  # retrying with the same budget doesn't reset it
        extend(node, conn, Scan("s", tuple(DESC), budget=12))
    assert e.value.candidates == 15


def test_a_marker_left_by_a_lost_call_blocks_new_scans_until_recovery_t212(conn: sqlite3.Connection) -> None:
    class Lost(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "scanblocks" and params[0] == "start":
                raise RpcTransportError("scanblocks: timed out")
            return super().call(method, params)

    target(conn, FakeNode(1000, {}))
    with pytest.raises(RpcTransportError):
        extend(Lost(1000, {}), conn, Scan("s", tuple(DESC)))
    node = FakeNode(1000, {})
    with pytest.raises(scans.ScanInFlightError):  # never overwrite the marker of a scan that may run
        extend(node, conn, Scan("s", tuple(DESC)))
    assert cc.scan_marker(conn) == "s" and not [p for m, p in node.calls if m == "scanblocks"]
    node.running = True
    assert recover(node, conn) is True
    extend(node, conn, Scan("s", tuple(DESC)))  # free again


def test_the_marker_is_cleared_as_soon_as_the_node_has_answered_t212(conn: sqlite3.Connection) -> None:
    class GuardLost(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getindexinfo" and any(m == "scanblocks" for m, _ in self.calls):
                raise RpcTransportError("getindexinfo: connection reset")
            return super().call(method, params)

    target(conn, FakeNode(1000, {}))
    with pytest.raises(RpcTransportError):
        extend(GuardLost(1000, {}), conn, Scan("s", tuple(DESC)))
    assert cc.scan_marker(conn) is None  # scanblocks had answered: nothing of ours is running


def test_a_failure_in_a_later_range_keeps_the_earlier_ones(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scans, "RANGE_BLOCKS", 300)

    class Busy(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "scanblocks" and params[0] == "start" and params[2] >= 300:
                raise RpcCallError(method, -8, "Scan already in progress")
            return super().call(method, params)

    node = Busy(1000, {5: [receive(1)]})
    target(conn, node)
    with pytest.raises(ScanBusyError):
        extend(node, conn, Scan("s", tuple(DESC)))
    assert (
        cc.coverage(conn, "s") == cc.Coverage("s", 0, 299, bh(299)) and len(cc.activity_for(conn, SPK)) == 1
    )
    assert cc.scan_marker(conn) is None


@pytest.mark.parametrize("bad", [{"subject": ""}, {"start_height": -1}, {"budget": -1}])
def test_a_malformed_scan_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="a scan needs"):
        Scan(**{"subject": "s", "scanobjects": tuple(DESC), **bad})


def test_an_oversized_scan_reply_still_clears_the_marker_t212(conn: sqlite3.Connection) -> None:
    class TooLarge(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "scanblocks" and params[0] == "start":
                raise RpcResponseTooLargeError("scanblocks: the reply is too large")
            return super().call(method, params)

    target(conn, FakeNode(1000, {}))
    with pytest.raises(RpcResponseTooLargeError):
        extend(TooLarge(1000, {}), conn, Scan("s", tuple(DESC)))
    assert cc.scan_marker(conn) is None  # the node answered: its scan is over

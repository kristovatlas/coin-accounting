"""The scan protocol: a script's or descriptor's full history from the node's indexes, with no silent
gaps (PLAN §1 "Scan protocol"; THREAT_MODEL T-205, T-207, T-210, T-212).

Core's `scanblocks` skips a filter range it can't read without saying so: `completed` stays true and
`to_height` is the requested stop height. So every scan follows this protocol:

1. **The stop height** S is at most the filter index's height and at most the tip minus `TIP_WINDOW`,
   and its block hash is recorded first.
2. **Bounded ranges** of at most `RANGE_BLOCKS`, each processed and committed on its own.
3. **After each range** the filter index must still be at or above S and `getblockhash(S)` unchanged;
   otherwise the range's results are discarded (`StaleScanError`) and the range is retried.
4. **The newest blocks** (above S, up to the scan target) never go through `scanblocks`: their hashes
   go straight to `getdescriptoractivity`, which errors rather than skips.
5. **A block that left the active chain** during a call is `StaleScanError` (retry the range); any
   other failure is a hard error.

**Busy scripts (T-205):** a tagged third-party script, such as an exchange's hot wallet, can have
activity in 100k+ blocks, and `getdescriptoractivity` has no paging or abort. So each scan has an
activity budget: the candidate count `scanblocks` reports comes first, and a range that would take
the scan past its budget is refused (`ActivityBudgetError`) before any of its blocks is read. The
caller asks the user, then scans again with a larger budget, or none.

**Scans the app left running (T-212):** Core runs one `scanblocks` at a time for all RPC users, and
a scan keeps running after the client disconnects. So an in-flight marker is written just before
each `scanblocks` call and cleared once the node has answered (with results or an error). A marker
left behind by a crash or a client timeout makes `recover` abort the node's scan at the next
start-up. Core can't say whose scan is running, so the marker is what makes it ours.

Candidate blocks are read with `getdescriptoractivity` in calls of at most `ACTIVITY_BLOCKS` blocks,
with `include_mempool` always false: the mempool is a separate, ephemeral pass. Its exact matching
removes the filters' false positives. Each range's activity and coverage are committed together,
against the scan target (`storage.chain_cache`), so a reorg found later removes them (T-207).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from coinacct.chain.txs import RPC_NOT_FOUND, ChainRpc
from coinacct.domain.chain import Outpoint, btc_to_sats, is_hash, is_hex
from coinacct.rpc import RpcCallError
from coinacct.storage.chain_cache import (
    Activity,
    Coverage,
    clear_scan_marker,
    coverage,
    extend_coverage,
    put_activity,
    reference_tip,
    scan_marker,
    set_scan_marker,
)
from coinacct.storage.chain_state import Tip
from coinacct.storage.db import Connection, transaction

FILTER_INDEX: Final = "basic block filter index"
# PLAN §1: sized from the M1 mainnet perf check (run by the human); these are the planned defaults.
RANGE_BLOCKS: Final = 50_000
TIP_WINDOW: Final = 100
ACTIVITY_BLOCKS: Final = 200
ACTIVITY_BUDGET: Final = 1_000
# Core's RPC_INVALID_PARAMETER: "Block is not in main chain", and "Scan already in progress".
RPC_INVALID_PARAMETER: Final = -8


class MalformedScanError(ValueError):
    """A scan reply didn't have the expected shape."""


class StaleScanError(RuntimeError):
    """The chain or the filter index moved under the scan: discard the range's results and retry."""


class ScanBusyError(RuntimeError):
    """Another `scanblocks` is running on the node (Core runs one at a time for all users): the
    queue is busy; retry with backoff (T-212)."""


class ActivityBudgetError(RuntimeError):
    """The scan would read more candidate blocks than its budget allows (T-205). What was scanned
    before stays committed. `candidates` is how many blocks the scan has found so far."""

    def __init__(self, candidates: int, budget: int) -> None:
        super().__init__(f"{candidates} candidate blocks, over the budget of {budget}")
        self.candidates = candidates
        self.budget = budget


class ScanAbortedError(RuntimeError):
    """The scan was aborted (`scanblocks abort`) before it finished: its results are incomplete."""


@dataclass(frozen=True, slots=True)
class Scan:
    """What to scan: `subject` names it in the coverage table; `scanobjects` are what Core is given
    (descriptor strings or `{desc, range}` objects); `start_height` is where its history begins;
    `budget` is how many candidate blocks this scan may read (None: no limit, after the user agreed)."""

    subject: str
    scanobjects: tuple[Any, ...]
    start_height: int = 0
    budget: int | None = ACTIVITY_BUDGET


def stop_height(rpc: ChainRpc, target: Tip) -> int:
    """S: at most the filter index's height and at most `target` minus `TIP_WINDOW` (-1 if no block
    is that old yet)."""
    return min(_filter_height(rpc), target.height - TIP_WINDOW)


def scan_range(rpc: ChainRpc, scanobjects: Sequence[Any], start: int, stop: int, stop_hash: str) -> list[str]:
    """The candidate blocks between `start` and `stop`, checked against the silent-skip gap (T-210)."""
    if not 0 <= start <= stop or stop - start + 1 > RANGE_BLOCKS:
        raise ValueError("a scan range is 0 <= start <= stop, at most RANGE_BLOCKS blocks")
    try:
        result = rpc.call("scanblocks", ["start", list(scanobjects), start, stop, "basic"])
    except RpcCallError as e:
        if e.code == RPC_INVALID_PARAMETER and "already in progress" in e.node_message:
            raise ScanBusyError("another scan is running on the node") from None
        raise
    if not isinstance(result, dict):
        raise MalformedScanError("scanblocks didn't return an object")
    if result.get("completed") is not True:
        if result.get("completed") is False:
            raise ScanAbortedError("the scan was aborted before it finished")
        raise MalformedScanError("scanblocks didn't say whether it completed")
    if result.get("from_height") != start or result.get("to_height") != stop:
        raise MalformedScanError("scanblocks scanned a different range")
    blocks = result.get("relevant_blocks")
    if not isinstance(blocks, list) or not all(is_hash(b) for b in blocks) or len(set(blocks)) != len(blocks):
        raise MalformedScanError("scanblocks returned malformed block hashes")
    # The gap guard: the filter index still covers the stop block, and it is still the same block.
    if _filter_height(rpc) < stop or _block_hash(rpc, stop) != stop_hash:
        raise StaleScanError("the chain or the filter index moved during the scan")
    return list(blocks)


def activity(rpc: ChainRpc, blockhashes: Sequence[str], scanobjects: Sequence[Any]) -> list[Activity]:
    """The exact receive and spend events in `blockhashes`, in calls of at most `ACTIVITY_BLOCKS`
    blocks (T-205). Never includes the mempool."""
    events: list[Activity] = []
    for i in range(0, len(blockhashes), ACTIVITY_BLOCKS):
        chunk = list(blockhashes[i : i + ACTIVITY_BLOCKS])
        try:
            result = rpc.call("getdescriptoractivity", [chunk, list(scanobjects), False])
        except RpcCallError as e:
            if e.code == RPC_NOT_FOUND or (
                e.code == RPC_INVALID_PARAMETER and "not in main chain" in e.node_message
            ):
                raise StaleScanError("a block left the active chain during the scan") from None
            raise
        if not isinstance(result, dict) or not isinstance(result.get("activity"), list):
            raise MalformedScanError("getdescriptoractivity didn't return an activity list")
        wanted = set(chunk)
        for raw in result["activity"]:
            event = _event(raw)
            if event.blockhash not in wanted:
                raise MalformedScanError("getdescriptoractivity reported a block that wasn't asked for")
            events.append(event)
    return events


def extend(rpc: ChainRpc, conn: Connection, scan: Scan) -> Coverage:
    """Extend `scan`'s coverage to the scan target, one range at a time, committing each range's
    activity and coverage together. Returns the coverage reached. `StaleScanError` and
    `ScanBusyError` leave everything committed so far; calling again resumes after it."""
    target = reference_tip(conn)
    if target is None:
        raise ValueError("there is no tip to scan to: catch up first")
    now = coverage(conn, scan.subject)
    start = scan.start_height if now is None else now.stop_height + 1
    stop = stop_height(rpc, target)
    candidates = 0
    while start <= stop:
        end = min(start + RANGE_BLOCKS - 1, stop)
        end_hash = _block_hash(rpc, end)
        blocks = _marked_scan(rpc, conn, scan, Coverage(scan.subject, start, end, end_hash))
        candidates += len(blocks)
        if scan.budget is not None and candidates > scan.budget:
            raise ActivityBudgetError(candidates, scan.budget)
        _commit(
            conn,
            activity(rpc, blocks, scan.scanobjects),
            Coverage(scan.subject, start, end, end_hash),
            target,
        )
        start = end + 1
    if start <= target.height:
        # The tip window: every block by hash, straight to getdescriptoractivity.
        hashes = [_block_hash(rpc, h) for h in range(start, target.height + 1)]
        if hashes[-1] != target.blockhash:
            raise StaleScanError("the scan target left the active chain")
        window = Coverage(scan.subject, start, target.height, target.blockhash)
        _commit(conn, activity(rpc, hashes, scan.scanobjects), window, target)
    reached = coverage(conn, scan.subject)
    if reached is None:
        raise ValueError("the scan's start height is above the scan target")
    return reached


def recover(rpc: ChainRpc, conn: Connection) -> bool:
    """At start-up, and after any client error: abort the node's scan if the app may have left one
    running (an in-flight marker is set), then clear the marker. Returns whether a scan was aborted."""
    if scan_marker(conn) is None:
        return False
    aborted = rpc.call("scanblocks", ["abort"])
    if type(aborted) is not bool:
        raise MalformedScanError("scanblocks abort didn't say whether a scan was running")
    clear_scan_marker(conn)
    return aborted


def _marked_scan(rpc: ChainRpc, conn: Connection, scan: Scan, covered: Coverage) -> list[str]:
    """`scan_range` inside the in-flight marker. The marker is cleared once the node has answered;
    on a transport error or timeout it stays, since the node may still be scanning."""
    set_scan_marker(conn, scan.subject)
    try:
        blocks = scan_range(
            rpc, scan.scanobjects, covered.start_height, covered.stop_height, covered.stop_hash
        )
    except (RpcCallError, MalformedScanError, StaleScanError, ScanBusyError, ScanAbortedError):
        clear_scan_marker(conn)
        raise
    clear_scan_marker(conn)
    return blocks


def _commit(conn: Connection, events: list[Activity], covered: Coverage, target: Tip) -> None:
    """A range's activity and coverage, together or not at all."""
    if any(not covered.start_height <= e.height <= covered.stop_height for e in events):
        raise MalformedScanError("getdescriptoractivity reported an event outside the range")
    with transaction(conn):
        put_activity(conn, events, target)
        extend_coverage(conn, covered, target)


def _event(raw: object) -> Activity:
    if not isinstance(raw, dict):
        raise MalformedScanError("an activity event isn't an object")
    kind, blockhash, height = raw.get("type"), raw.get("blockhash"), raw.get("height")
    if not is_hash(blockhash) or type(height) is not int or height < 0:
        raise MalformedScanError("an activity event has no block")
    try:
        sats = btc_to_sats(raw.get("amount"))  # type: ignore[arg-type]  # refuses anything but a Decimal
    except ValueError:
        raise MalformedScanError("an activity event's amount isn't whole satoshis") from None
    if kind == "receive":
        txid, n, spk = raw.get("txid"), raw.get("vout"), raw.get("output_spk")
        prevout = None
    elif kind == "spend":
        txid, n, spk = raw.get("spend_txid"), raw.get("spend_vin"), raw.get("prevout_spk")
        prev_txid, prev_vout = raw.get("prevout_txid"), raw.get("prevout_vout")
        if not is_hash(prev_txid) or type(prev_vout) is not int or prev_vout < 0:
            raise MalformedScanError("a spend event doesn't name the output it spent")
        prevout = Outpoint(prev_txid, prev_vout)
    else:
        raise MalformedScanError("an activity event is neither a receive nor a spend")
    if not is_hash(txid) or type(n) is not int or n < 0:
        raise MalformedScanError("an activity event has no transaction")
    script_hex = spk.get("hex") if isinstance(spk, dict) else None
    if not is_hex(script_hex):
        raise MalformedScanError("an activity event has no script")
    return Activity(kind, script_hex, txid, n, sats, blockhash, height, prevout)


def _filter_height(rpc: ChainRpc) -> int:
    info = rpc.call("getindexinfo", [FILTER_INDEX])
    state = info.get(FILTER_INDEX) if isinstance(info, dict) else None
    height = state.get("best_block_height") if isinstance(state, dict) else None
    if type(height) is not int or height < -1:
        raise MalformedScanError("getindexinfo didn't report the filter index's height")
    return height


def _block_hash(rpc: ChainRpc, height: int) -> str:
    blockhash = rpc.call("getblockhash", [height])
    if not is_hash(blockhash):
        raise MalformedScanError("getblockhash didn't return a block hash")
    return blockhash

"""The scan protocol: a script's or descriptor's full history from the node's indexes, with no silent
gaps (PLAN §1 "Scan protocol"; THREAT_MODEL T-205, T-207, T-210, T-212).

Core's `scanblocks` skips a filter range it can't read without saying so: `completed` stays true and
`to_height` is the requested stop height. So every scan follows this protocol:

1. **The stop height** S is at most the filter index's height and at most the tip minus `TIP_WINDOW`,
   and `getblockhash(S)` is recorded once, before the first range.
2. **Bounded ranges** of at most `RANGE_BLOCKS`, each processed and committed on its own.
3. **After each range** the filter index must still reach S and `getblockhash(S)` must be unchanged;
   otherwise the range's results are discarded (`StaleScanError`). Every range is checked against the
   same S, and against the block the subject's coverage continues from (its *anchor*), which must
   still be the block at its height. So the node switching branches, between ranges or between a
   failed attempt and its retry, can't mix two branches in one coverage. Each event's block hash
   must also be the block at its height.
4. **The newest blocks** (above S, up to the scan target) never go through `scanblocks`: their hashes
   go straight to `getdescriptoractivity`, which errors rather than skips. They are at most
   `TIP_WINDOW` blocks: a filter index further behind is `FilterIndexBehindError`, never a longer
   direct read.
5. **A block that left the active chain** during a call is `StaleScanError`; any other failure is a
   hard error.

`extend` doesn't retry. Its caller (the scan job) retries a `StaleScanError` or `ScanBusyError`
range a few times, runs `recover` after any client error, and calls `extend` again, which resumes
after the coverage committed so far.

**Busy scripts (T-205):** a tagged third-party script, such as an exchange's hot wallet, can have
activity in 100k+ blocks, and `getdescriptoractivity` has no paging or abort. So each subject has
an activity budget: the candidate blocks `scanblocks` reports are counted with its coverage, and a
range that would take the subject's total past its budget is refused (`ActivityBudgetError`) before
any of its blocks is read. Retrying doesn't reset the count. The caller asks the user, then scans
again with a larger budget, or none.

**Scans the app left running (T-212):** Core runs one `scanblocks` at a time for all RPC users, and
a scan keeps running after the client disconnects. So an in-flight marker is written just before
each `scanblocks` call and cleared as soon as the node has answered it (with results or an error).
A transport error or timeout leaves it, since the node may still be scanning. While it is set, no
new scan starts (`ScanInFlightError`): `recover` (at start-up and after any client error) checks
`scanblocks status`, aborts a running scan, and clears it. Core can't say whose scan is running, so
the marker is what makes it ours: without one, `recover` never aborts anything.

Candidate blocks are read with `getdescriptoractivity` in calls of at most `ACTIVITY_BLOCKS` blocks,
with `include_mempool` always false: the mempool is a separate, ephemeral pass. Its exact matching
removes the filters' false positives. Each range's activity and coverage are committed together,
against the scan target (`storage.chain_cache`), so a reorg found later removes them (T-207).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from coinacct.chain.txs import RPC_NOT_FOUND, ChainRpc
from coinacct.domain.chain import Outpoint, btc_to_sats, is_hash, is_hex
from coinacct.rpc import RpcCallError, RpcTransportError
from coinacct.storage.chain_cache import (
    Activity,
    Coverage,
    clear_scan_marker,
    coverage,
    coverage_candidates,
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


class ChainMovedError(StaleScanError):
    """The chain moved below the subject's coverage, or the scan target left it: retrying the range
    can't help; catch up first (`chain.reorg.catch_up`)."""


class FilterIndexBehindError(StaleScanError):
    """The filter index is more than `TIP_WINDOW` blocks behind the scan target: wait for it, rather
    than read the blocks it hasn't reached one by one (T-205, T-210)."""


class ScanInFlightError(RuntimeError):
    """A `scanblocks` call the app started may still be running on the node (the in-flight marker is
    set): run `recover` before scanning again (T-212)."""


class ScanCancelledError(RuntimeError):
    """The job was cancelled (shutdown): no further range is started (T-212)."""


class ScanBusyError(RuntimeError):
    """Another `scanblocks` is running on the node (Core runs one at a time for all users): the
    queue is busy; retry with backoff (T-212)."""


class ActivityBudgetError(RuntimeError):
    """The subject would read more candidate blocks than its budget allows (T-205). What was scanned
    before stays committed. `candidates` is the subject's total with the refused range."""

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
    `budget` is how many candidate blocks the subject may have in all (None: no limit, after the
    user agreed)."""

    subject: str
    scanobjects: tuple[Any, ...]
    start_height: int = 0
    budget: int | None = ACTIVITY_BUDGET

    def __post_init__(self) -> None:
        if not self.subject or self.start_height < 0 or (self.budget is not None and self.budget < 0):
            raise ValueError("a scan needs a subject, a start height >= 0 and a budget >= 0")


def stop_height(rpc: ChainRpc, target: Tip) -> int:
    """S: at most the filter index's height and at most `target` minus `TIP_WINDOW` (-1 if no block
    is that old yet)."""
    return min(_filter_height(rpc), target.height - TIP_WINDOW)


def _never() -> bool:
    return False


def scan_range(  # noqa: PLR0913 - the range, its guard, and where to keep the in-flight marker
    rpc: ChainRpc,
    scanobjects: Sequence[Any],
    start: int,
    stop: int,
    guard: Tip,
    *,
    marker: tuple[Connection, str] | None = None,
    cancelled: Callable[[], bool] = _never,
) -> list[str]:
    """The candidate blocks between `start` and `stop`, checked against the silent-skip gap (T-210):
    afterwards the filter index must still reach `guard` (S and the hash recorded for it before the
    first range), and S must still be that block. With `marker` (the user DB and the subject), the
    `scanblocks` call runs inside the in-flight marker (T-212)."""
    if stop > guard.height:
        raise ValueError("a scan range can't go past its guard block")
    if not 0 <= start <= stop or stop - start + 1 > RANGE_BLOCKS:
        raise ValueError("a scan range is 0 <= start <= stop, at most RANGE_BLOCKS blocks")
    result = _scanblocks(rpc, ["start", list(scanobjects), start, stop, "basic"], marker, cancelled)
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
    # The gap guard: the filter index still reaches S, and S is still the same block.
    if _filter_height(rpc) < guard.height or _block_hash(rpc, guard.height) != guard.blockhash:
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
            if (e.code == RPC_NOT_FOUND and "Block not found" in e.node_message) or (
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
    # Each event's block must be the block at its height, or a reorg could later miss it (T-207).
    for blockhash, height in sorted({(e.blockhash, e.height) for e in events}, key=lambda b: b[1]):
        if _block_hash(rpc, height) != blockhash:
            raise StaleScanError("an event's block isn't the block at its height")
    return events


def extend(rpc: ChainRpc, conn: Connection, scan: Scan, cancelled: Callable[[], bool] = _never) -> Coverage:
    """Extend `scan`'s coverage to the scan target, one range at a time, committing each range's
    activity and coverage together. Returns the coverage reached. `StaleScanError` and
    `ScanBusyError` leave everything committed so far; calling again resumes after it.
    `cancelled` is checked right before each range's `scanblocks`: once it is true no further
    range starts (`ScanCancelledError`), so a shutdown abort isn't followed by a new scan (T-212)."""
    target = reference_tip(conn)
    if target is None:
        raise ValueError("there is no tip to scan to: catch up first")
    now = coverage(conn, scan.subject)
    start = scan.start_height if now is None else now.stop_height + 1
    # The anchor: the block the subject's coverage continues from. A reorg below it (since a failed
    # attempt, or during this one) means catching up first, never appending to it.
    anchor = None if now is None else Tip(now.stop_hash, now.stop_height)
    _check_anchor(rpc, anchor)
    stop = stop_height(rpc, target)
    seen = coverage_candidates(conn, scan.subject)
    guard = Tip(_block_hash(rpc, stop), stop) if start <= stop else None
    while guard is not None and start <= stop:
        end = min(start + RANGE_BLOCKS - 1, stop)
        blocks = scan_range(
            rpc, scan.scanobjects, start, end, guard, marker=(conn, scan.subject), cancelled=cancelled
        )
        _check_anchor(rpc, anchor)
        # Only a range that adds candidates can take the subject past its budget.
        if blocks and scan.budget is not None and seen + len(blocks) > scan.budget:
            raise ActivityBudgetError(seen + len(blocks), scan.budget)
        end_hash = _block_hash(rpc, end)  # after the guard passed: on the same chain as S
        covered = Coverage(scan.subject, start, end, end_hash)
        _commit(conn, activity(rpc, blocks, scan.scanobjects), covered, target, len(blocks))
        seen += len(blocks)
        anchor = Tip(end_hash, end)
        start = end + 1
    if start <= target.height:
        if target.height - start + 1 > TIP_WINDOW:
            raise FilterIndexBehindError("the filter index is behind the chain; wait for it (T-210)")
        # The tip window: every block by hash, straight to getdescriptoractivity.
        hashes = [_block_hash(rpc, h) for h in range(start, target.height + 1)]
        if hashes[-1] != target.blockhash:
            raise ChainMovedError("the scan target left the active chain")
        _check_anchor(rpc, anchor)
        window = Coverage(scan.subject, start, target.height, target.blockhash)
        _commit(conn, activity(rpc, hashes, scan.scanobjects), window, target, 0)
    reached = coverage(conn, scan.subject)
    if reached is None:
        raise ValueError("the scan's start height is above the scan target")
    return reached


def recover(rpc: ChainRpc, conn: Connection) -> bool:
    """At start-up, and after any client error (architecture §8.1): if the app may have left a scan
    running (an in-flight marker is set), check `scanblocks status` and abort a running scan, then
    clear the marker. Returns whether a scan was aborted. Without a marker it never touches the
    node's scan slot, which may be another RPC user's."""
    if scan_marker(conn) is None:
        return False
    status = rpc.call("scanblocks", ["status"])
    aborted = False
    if status is not None:
        if not isinstance(status, dict):
            raise MalformedScanError("scanblocks status didn't return a scan's progress")
        aborted = rpc.call("scanblocks", ["abort"])
        if type(aborted) is not bool:
            raise MalformedScanError("scanblocks abort didn't say whether a scan was running")
    clear_scan_marker(conn)
    return aborted


def _scanblocks(
    rpc: ChainRpc,
    params: list[Any],
    marker: tuple[Connection, str] | None,
    cancelled: Callable[[], bool] = _never,
) -> Any:
    """The `scanblocks` call itself, inside the in-flight marker: cleared as soon as the node has
    answered, kept on a transport error or timeout, since the node may still be scanning.

    The cancel is checked after the marker is set and right before the call. Shutdown sets the
    cancel before it reads the marker, so either this sees the cancel, or shutdown sees the marker
    and sends the abort (T-212). That abort can still reach the node just before this call does;
    shutdown reads the marker again after the worker's join for that case."""
    conn = None if marker is None else marker[0]
    if marker is not None:
        if scan_marker(marker[0]) is not None:
            raise ScanInFlightError("a scan the app started may still be running; recover first")
        set_scan_marker(*marker)
    if cancelled():
        _clear(conn)
        raise ScanCancelledError("the scan job was cancelled")
    try:
        result = rpc.call("scanblocks", params)
    except RpcTransportError:
        raise
    except RpcCallError as e:
        _clear(conn)
        if e.code == RPC_INVALID_PARAMETER and "already in progress" in e.node_message:
            raise ScanBusyError("another scan is running on the node") from None
        raise
    except Exception:
        _clear(conn)
        raise
    _clear(conn)
    return result


def _check_anchor(rpc: ChainRpc, anchor: Tip | None) -> None:
    if anchor is not None and _block_hash(rpc, anchor.height) != anchor.blockhash:
        raise ChainMovedError("the chain moved below the subject's coverage; catch up first (T-207)")


def _clear(conn: Connection | None) -> None:
    if conn is not None:
        clear_scan_marker(conn)


def _commit(
    conn: Connection, events: list[Activity], covered: Coverage, target: Tip, candidates: int
) -> None:
    """A range's activity, coverage and candidate count, together or not at all."""
    if any(not covered.start_height <= e.height <= covered.stop_height for e in events):
        raise MalformedScanError("getdescriptoractivity reported an event outside the range")
    with transaction(conn):
        put_activity(conn, events, target)
        extend_coverage(conn, covered, target, candidates=candidates)


def _event(raw: object) -> Activity:
    if not isinstance(raw, dict):
        raise MalformedScanError("an activity event isn't an object")
    blockhash, height = raw.get("blockhash"), raw.get("height")
    if not is_hash(blockhash) or type(height) is not int or height < 0:
        raise MalformedScanError("an activity event has no block")
    kind, script_hex, txid, n, sats, prevout = event_body(raw)
    return Activity(kind, script_hex, txid, n, sats, blockhash, height, prevout)


def event_body(
    raw: dict[str, Any],
) -> tuple[Literal["receive", "spend"], str, str, int, int, Outpoint | None]:
    """What a `getdescriptoractivity` event says, apart from its block: (kind, script, txid, the
    output or input index, sats, and the spent output for a spend). Shared with the mempool pass,
    whose events have no block."""
    try:
        sats = btc_to_sats(raw.get("amount"))  # type: ignore[arg-type]  # refuses anything but a Decimal
    except ValueError:
        raise MalformedScanError("an activity event's amount isn't whole satoshis") from None
    kind = raw.get("type")
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
    return kind, script_hex, txid, n, sats, prevout


def _filter_height(rpc: ChainRpc) -> int:
    info = rpc.call("getindexinfo", [FILTER_INDEX])
    state = info.get(FILTER_INDEX) if isinstance(info, dict) else None
    height = state.get("best_block_height") if isinstance(state, dict) else None
    if type(height) is not int or height < -1:
        raise MalformedScanError("getindexinfo didn't report the filter index's height")
    return height


def _block_hash(rpc: ChainRpc, height: int) -> str:
    try:
        blockhash = rpc.call("getblockhash", [height])
    except RpcCallError as e:
        if e.code == RPC_INVALID_PARAMETER and "out of range" in e.node_message:
            # The active chain got shorter (a reorg to a higher-work, shorter chain).
            raise ChainMovedError("the chain is shorter than the scan expected") from None
        raise
    if not is_hash(blockhash):
        raise MalformedScanError("getblockhash didn't return a block hash")
    return blockhash

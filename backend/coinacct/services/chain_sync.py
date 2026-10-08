"""Keeping the chain cache in step with the node (architecture §8.1, §8.4; PLAN §1; THREAT_MODEL
T-207, T-210, T-212).

- `at_startup`, after the node checks pass (§8.1): abort a scan the app may have left running
  (`chain.scans.recover`), then the fork-point check against the reference tip (`chain.reorg`).
- `sync`, the tip-change job (§8.4): catch up (invalidating what a reorg removed), extend every
  subject's coverage to the new target, and only then make the target the last-seen tip. A subject
  that can't be extended right now (a stale or aborted range, a busy scan slot, a lagging filter
  index, a budget the user hasn't agreed to) leaves the catch-up unfinished, so the next tip check
  retries it. The chain moving under the sync stops it: the next one catches up first.
- What a reorg removed is kept in the user DB's review queue (`chain_cache.pending_review`) until
  the events built on it are flagged, so a crash or a failed sync never loses it (T-207, T-506).
  Every `SyncResult` reports the whole queue.
- Before the target becomes the last-seen tip, the mempool pass is rebuilt for every subject at that
  tip (§8.4; `chain.mempool`); a pass that finds the tip moved leaves the catch-up unfinished.

The job runs on the one job worker (§3): Core runs one `scanblocks` at a time, so subjects are
scanned one after another. A stale range is retried a few times (§8.2) before the subject waits for
the next sync. Each sync starts with `recover`, so a scan a lost connection left running on the
node is aborted before anything scans again; transport errors themselves reach the caller (the job
fails, and the next sync recovers).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from coinacct.chain import mempool, reorg, scans
from coinacct.chain.mempool import PendingActivity
from coinacct.chain.reorg import NodeSyncingError, TipChange, TipMovedError
from coinacct.chain.scans import (
    ActivityBudgetError,
    ChainMovedError,
    Scan,
    ScanAbortedError,
    ScanBusyError,
    ScanInFlightError,
    StaleScanError,
)
from coinacct.chain.txs import ChainRpc
from coinacct.storage.chain_cache import (
    StaleTipError,
    complete_scan_target,
    coverage,
    pending_review,
    reference_tip,
)
from coinacct.storage.chain_state import Tip
from coinacct.storage.db import Connection

log = logging.getLogger(__name__)

# §8.2: a range whose guard failed is retried, then the subject waits for the next sync.
RANGE_RETRIES: Final = 3


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one sync did. `target` is the tip it worked towards; `complete` is whether everything is
    caught up to it. `waiting` names the subjects left for the next sync, and `over_budget` those
    that need the user to agree to a larger budget (T-205). `to_review` is the whole review queue:
    every txid a reorg removed whose events haven't been flagged yet (T-506)."""

    change: TipChange | None
    target: Tip | None
    complete: bool
    waiting: tuple[str, ...] = ()
    over_budget: tuple[str, ...] = ()
    to_review: frozenset[str] = field(default_factory=frozenset)
    # The rebuilt mempool pass at `target`, or None if it couldn't be rebuilt this time.
    pending: tuple[PendingActivity, ...] | None = None


def at_startup(rpc: ChainRpc, conn: Connection) -> str | None:
    """§8.1, once the node checks have passed: recover a scan the app left running, then the
    fork-point check (what it invalidates goes to the review queue). Returns a reason for offline
    mode, or None. Any failure here means offline mode, never a failed start (ADR 0004): the reason
    names only the error's class, never the node's message (T-201)."""
    try:
        if scans.recover(rpc, conn):
            log.warning("aborted a scan the app had left running on the node (T-212)")
        try:
            reorg.catch_up(rpc, conn)
        except (TipMovedError, StaleTipError):
            reorg.catch_up(rpc, conn)  # the tip moved once during start-up: try again
    except NodeSyncingError:
        return "the node is still syncing its chain; chain access waits for it"
    except (TipMovedError, StaleTipError):
        log.info("the node's tip keeps moving during start-up; the tip poller catches up")
    except Exception as e:  # a node or DB problem here means offline mode, not a failed start
        return f"the chain catch-up at start-up failed ({type(e).__name__})"
    return None


def sync(rpc: ChainRpc, conn: Connection, subjects: Sequence[Scan]) -> SyncResult:
    """The tip-change job (§8.4). `subjects` are the scripts and descriptors whose history is kept."""
    scans.recover(rpc, conn)
    change = reorg.catch_up(rpc, conn)
    target = change.new if change is not None else reference_tip(conn)
    waiting: list[str] = []
    over_budget: list[str] = []
    for subject in subjects:
        if (
            target is not None
            and coverage(conn, subject.subject) is None
            and subject.start_height > target.height
        ):
            continue  # its history starts above the tip: nothing to scan yet
        outcome = _extend(rpc, conn, subject)
        if outcome == "over budget":
            over_budget.append(subject.subject)
        elif outcome != "done":
            waiting.append(subject.subject)
            if outcome == "chain moved":
                waiting.extend(s.subject for s in subjects[subjects.index(subject) + 1 :])
                break
    complete = not waiting and not over_budget
    pending = None
    if target is not None:
        objects = [o for s in subjects for o in s.scanobjects]
        try:
            pending = tuple(mempool.pending_activity(rpc, objects, target)) if objects else ()
        except StaleScanError:
            complete = False  # the tip moved: the next sync catches up first
        except ActivityBudgetError:
            log.warning("the mempool pass is over its limit; unconfirmed activity isn't shown (T-205)")
    if complete and change is not None:
        try:
            complete_scan_target(conn, change.new)
        except StaleTipError:
            complete = False  # another catch-up moved the target meanwhile: the next sync goes on
    return SyncResult(
        change, target, complete, tuple(waiting), tuple(over_budget), pending_review(conn), pending
    )


def _extend(rpc: ChainRpc, conn: Connection, subject: Scan) -> str:
    """Extend one subject: "done", "over budget", or why it waits for the next sync."""
    for _ in range(RANGE_RETRIES):
        try:
            scans.extend(rpc, conn, subject)
        except scans.FilterIndexBehindError:
            return "filter index behind"
        except ChainMovedError:
            return "chain moved"
        except (StaleScanError, ScanAbortedError):
            continue  # the chain moved under a range, or another client aborted it: retry
        except ScanBusyError:
            return "scan slot busy"
        except ScanInFlightError:
            scans.recover(rpc, conn)
            continue
        except ActivityBudgetError:
            return "over budget"
        return "done"
    return "stale"

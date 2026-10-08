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
- Before the target becomes the last-seen tip, the mempool pass is rebuilt at that tip, one subject
  at a time (§8.4; `chain.mempool`), so one busy script over the limit only hides its own unconfirmed
  activity (T-205); `mempool_refused` names the subjects without a pass. The pass is display-only:
  a refused or failed pass never holds up confirmed progress. Only a tip that moved during it leaves
  the catch-up unfinished.

The job runs on the one job worker (§3): Core runs one `scanblocks` at a time, so subjects are
scanned one after another. A stale range is retried a few times (§8.2) before the subject waits for
the next sync. Each sync starts with `recover`, so a scan a lost connection left running on the
node is aborted before anything scans again; transport errors themselves reach the caller (the job
fails, and the next sync recovers).
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from coinacct.chain import mempool, reorg, scans
from coinacct.chain.mempool import PendingActivity
from coinacct.chain.reorg import NodeSyncingError, TipChange, TipMovedError
from coinacct.chain.scans import (
    ActivityBudgetError,
    ChainMovedError,
    MalformedScanError,
    Scan,
    ScanAbortedError,
    ScanBusyError,
    ScanInFlightError,
    StaleScanError,
)
from coinacct.chain.txs import ChainRpc, NodeError
from coinacct.storage.chain_cache import (
    StaleTipError,
    complete_scan_target,
    coverage,
    pending_review,
    reference_tip,
)
from coinacct.storage.chain_state import Tip
from coinacct.storage.db import Connection, DbError

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
    # The rebuilt mempool pass at `target` (None: the tip moved during it), and the subjects it
    # couldn't cover (over the limit, or the node refused or garbled the pass).
    pending: tuple[PendingActivity, ...] | None = None
    mempool_refused: tuple[str, ...] = ()


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
        # The fork-point check didn't finish, and §8.1 says any failed node step means offline mode.
        return "the node's tip kept moving during the start-up catch-up"
    except (NodeError, MalformedScanError, reorg.MalformedHeaderError) as e:
        return f"the chain catch-up at start-up failed ({type(e).__name__})"  # only the class (T-201)
    except DbError as e:
        # The message is the app's own and says what to do (e.g. a damaged cache, T-408).
        log.error("the start-up catch-up stopped on the user DB: %s", e)
        return f"the user DB stopped the start-up catch-up: {e}"
    except Exception as e:  # a bug: offline mode, not a failed start, with its traceback in the log
        # The frames, not the message: a bug's message can carry a txid or a script (T-403).
        log.error(
            "the start-up catch-up failed (%s):\n%s",
            type(e).__name__,
            "".join(traceback.format_tb(e.__traceback__)),
        )
        return f"the chain catch-up at start-up failed ({type(e).__name__})"
    return None


def never() -> bool:
    return False


def sync(
    rpc: ChainRpc, conn: Connection, subjects: Sequence[Scan], cancelled: Callable[[], bool] = never
) -> SyncResult:
    """The tip-change job (§8.4). `subjects` are the scripts and descriptors whose history is kept.
    `cancelled` is checked before each subject, each range and the mempool pass: a cancelled sync
    stops there, unfinished, and never starts a scan after the shutdown abort (§3 step 2, T-212)."""
    scans.recover(rpc, conn)
    change = reorg.catch_up(rpc, conn)
    target = change.new if change is not None else reference_tip(conn)
    waiting: list[str] = []
    over_budget: list[str] = []
    for i, subject in enumerate(subjects):
        if _nothing_to_scan(conn, subject, target):
            continue
        outcome = _extend(rpc, conn, subject, cancelled)
        if outcome == "over budget":
            over_budget.append(subject.subject)
        elif outcome != "done":
            waiting.append(subject.subject)
            if outcome in ("chain moved", "cancelled"):
                waiting.extend(s.subject for s in subjects[i + 1 :] if not _nothing_to_scan(conn, s, target))
                break
    complete = not waiting and not over_budget
    pending: tuple[PendingActivity, ...] | None = None
    refused: list[str] = []
    if cancelled():
        complete = False
    elif target is not None:
        pending, refused = _mempool_pass(rpc, subjects, target)
        complete = complete and pending is not None
    if complete and change is not None:
        try:
            complete_scan_target(conn, change.new)
        except StaleTipError:
            complete = False  # another catch-up moved the target meanwhile: the next sync goes on
    return SyncResult(
        change,
        target,
        complete,
        tuple(waiting),
        tuple(over_budget),
        pending_review(conn),
        pending,
        tuple(refused),
    )


def _nothing_to_scan(conn: Connection, subject: Scan, target: Tip | None) -> bool:
    """A new subject whose history starts above the tip: nothing to scan yet."""
    return (
        target is not None
        and coverage(conn, subject.subject) is None
        and subject.start_height > target.height
    )


def _mempool_pass(
    rpc: ChainRpc, subjects: Sequence[Scan], target: Tip
) -> tuple[tuple[PendingActivity, ...] | None, list[str]]:
    """The mempool pass at `target`, one subject at a time. None if the tip moved during it."""
    pending: list[PendingActivity] = []
    refused: list[str] = []
    for i, subject in enumerate(subjects):
        try:
            pending.extend(mempool.pending_activity(rpc, subject.scanobjects, target))
        except StaleScanError:
            return None, refused  # the tip moved: the next sync catches up first
        except ActivityBudgetError:
            refused.append(subject.subject)  # a busy script: only its own pass is hidden (T-205)
        except (MalformedScanError, NodeError) as e:
            # Display-only: never hold up confirmed progress. Only the class: node text can name scripts.
            log.warning("the mempool pass failed (%s); unconfirmed activity isn't shown", type(e).__name__)
            refused.extend(s.subject for s in subjects[i:])
            break
    # Subjects can share scripts, so the same event can come back twice; across subjects the
    # mempool still never holds two spends of one output (`chain.mempool`'s per-call check).
    unique = list(dict.fromkeys(pending))
    spends = [p.prevout for p in unique if p.prevout is not None]
    if len(spends) != len(set(spends)):
        log.warning("the mempool pass reported two spends of one output; unconfirmed activity isn't shown")
        return (), [s.subject for s in subjects]
    return tuple(unique), refused


def _extend(  # noqa: PLR0911 - one outcome per way a range can end
    rpc: ChainRpc, conn: Connection, subject: Scan, cancelled: Callable[[], bool] = never
) -> str:
    """Extend one subject: "done", "over budget", or why it waits for the next sync."""
    for _ in range(RANGE_RETRIES):
        if cancelled():
            return "cancelled"
        try:
            scans.extend(rpc, conn, subject)
        except scans.FilterIndexBehindError:
            return "filter index behind"
        except ChainMovedError:
            return "chain moved"
        except (StaleScanError, ScanAbortedError):
            # The chain moved under a range, or a client aborted it; after our own shutdown abort
            # the cancel check above ends the loop, so no scan starts after it (T-212).
            continue
        except ScanBusyError:
            return "scan slot busy"
        except ScanInFlightError:
            scans.recover(rpc, conn)
            continue
        except ActivityBudgetError:
            return "over budget"
        return "done"
    return "stale"

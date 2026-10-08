"""Keeping the chain cache in step with the node (architecture §8.1, §8.4; PLAN §1; THREAT_MODEL
T-207, T-210, T-212).

- `at_startup`, after the node checks pass (§8.1): abort a scan the app may have left running
  (`chain.scans.recover`), then the fork-point check against the reference tip (`chain.reorg`).
- `sync`, the tip-change job (§8.4): catch up (invalidating what a reorg removed), extend every
  subject's coverage to the new target, and only then make the target the last-seen tip. A subject
  that can't be extended right now (a stale range, a busy scan slot, a lagging filter index, a
  budget the user hasn't agreed to) leaves the catch-up unfinished, so the next tip check retries it.

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

from coinacct.chain import reorg, scans
from coinacct.chain.reorg import MalformedHeaderError, NodeSyncingError, TipChange, TipMovedError
from coinacct.chain.scans import (
    ActivityBudgetError,
    MalformedScanError,
    Scan,
    ScanBusyError,
    ScanInFlightError,
    StaleScanError,
)
from coinacct.chain.txs import ChainRpc
from coinacct.storage.chain_cache import StaleTipError, complete_scan_target
from coinacct.storage.chain_state import Tip
from coinacct.storage.db import Connection

log = logging.getLogger(__name__)

# §8.2: a range whose guard failed is retried, then the subject waits for the next sync.
RANGE_RETRIES: Final = 3


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one sync did. `target` is the tip it caught up to (None: nothing to do); `complete` is
    whether the last-seen tip moved to it. `waiting` names the subjects left for the next sync, and
    `over_budget` those that need the user to agree to a larger budget (T-205)."""

    change: TipChange | None
    target: Tip | None
    complete: bool
    waiting: tuple[str, ...] = ()
    over_budget: tuple[str, ...] = ()
    invalidated_txids: frozenset[str] = field(default_factory=frozenset)


def at_startup(rpc: ChainRpc, conn: Connection) -> str | None:
    """§8.1, once the node checks have passed: recover a scan the app left running, then the
    fork-point check. Returns a reason for offline mode, or None. A tip that moves meanwhile is left
    to the tip poller."""
    try:
        if scans.recover(rpc, conn):
            log.warning("aborted a scan the app had left running on the node (T-212)")
        reorg.catch_up(rpc, conn)
    except NodeSyncingError:
        return "the node is still syncing its chain; chain access waits for it"
    except (MalformedHeaderError, MalformedScanError):
        return "the node sent a reply the app can't read (chain catch-up)"
    except (TipMovedError, StaleTipError):
        log.info("the node's tip moved during start-up; the tip poller catches up")
    return None


def sync(rpc: ChainRpc, conn: Connection, subjects: Sequence[Scan]) -> SyncResult:
    """The tip-change job (§8.4). `subjects` are the scripts and descriptors whose history is kept."""
    scans.recover(rpc, conn)
    change = reorg.catch_up(rpc, conn)
    if change is None:
        return SyncResult(None, None, complete=True)
    target = change.new
    invalidated = frozenset() if change.invalidated is None else change.invalidated.txids
    waiting: list[str] = []
    over_budget: list[str] = []
    for subject in subjects:
        outcome = _extend(rpc, conn, subject)
        if outcome == "over budget":
            over_budget.append(subject.subject)
        elif outcome != "done":
            waiting.append(subject.subject)
    complete = not waiting and not over_budget
    if complete:
        try:
            complete_scan_target(conn, target)
        except StaleTipError:
            complete = False  # another catch-up moved the target meanwhile: the next sync goes on
    return SyncResult(change, target, complete, tuple(waiting), tuple(over_budget), invalidated)


def _extend(rpc: ChainRpc, conn: Connection, subject: Scan) -> str:
    """Extend one subject: "done", "over budget", or why it waits for the next sync."""
    for _ in range(RANGE_RETRIES):
        try:
            scans.extend(rpc, conn, subject)
        except scans.FilterIndexBehindError:
            return "filter index behind"
        except StaleScanError:
            continue  # the chain moved under a range: retry it
        except ScanBusyError:
            return "scan slot busy"
        except ScanInFlightError:
            scans.recover(rpc, conn)
            continue
        except ActivityBudgetError:
            return "over budget"
        return "done"
    return "stale"

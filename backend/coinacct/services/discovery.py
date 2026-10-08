"""Growing a ranged descriptor's window as its addresses are used (PLAN §1, §3; T-207, T-210).

A ranged descriptor is imported with one window, indexes 0..gap_limit-1. After each sync, the scripts
of every window with confirmed activity say which indexes are used, and the highest is recorded
(`storage.accounts.mark_used`). When the highest used index is within the gap limit of the window's
end, the window grows:
- to at least highest used + gap limit, so the gap limit of unused indexes follows the last used one;
- and at least to double its size, so a long-used wallet needs a few wider scans, not one per gap;
- by at most `chain.descriptors.MAX_DERIVE` indexes at once, and never past `AUTO_RANGE_END`.

Only confirmed use grows a window. Anyone who knows the xpub can pay its addresses: an unconfirmed
transaction costs nothing to broadcast and replace, so mempool use would let a third party grow the
window, and force a full rescan, at will (T-205, T-207). Confirmed use costs them a fee per step, and
`AUTO_RANGE_END` bounds the steps: beyond it the window is reported as full for the user to decide
(the schema allows up to 100,000). The cap is provisional until the M1 perf check sizes it (PLAN §1).

Each wider window is a new scan subject, scanned from the descriptor's start over the whole window
(`services.imports`): simple, and correct whatever the earlier coverage, at the cost of rescanning
the old indexes' history once per growth. Doubling keeps the number of growths small (a wallet used
to index 1,000 needs about six); scanning only the new indexes is a later optimisation (#188).

A descriptor whose scan didn't finish this sync (waiting, or over its activity budget) isn't grown:
its activity is incomplete. One that can't be grown (the node's new addresses can't be used, or a new
script belongs to another owner or account) is reported and skipped; the others still grow. The new
scripts come from the node (`deriveaddresses`), like the import's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Final

from coinacct.chain import descriptors
from coinacct.chain.descriptors import DescriptorError, DescriptorInfo
from coinacct.chain.txs import ChainRpc
from coinacct.domain.addresses import AddressError, parse_address
from coinacct.services.imports import descriptor_subject
from coinacct.storage import accounts
from coinacct.storage.chain_state import recorded_chain
from coinacct.storage.db import Connection

AUTO_RANGE_END: Final = 10_000  # the largest window growth reaches by itself (see the docstring)

log = logging.getLogger(__name__)


def never() -> bool:
    return False


class DiscoveryError(ValueError):
    """A window couldn't be grown. The message names the descriptor's id only, never its text."""


@dataclass(frozen=True, slots=True)
class Grown:
    """What one pass did: the descriptors whose windows grew, those that couldn't be grown, and those
    whose window is at `AUTO_RANGE_END` with used indexes still within the gap limit of its end."""

    grown: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    full: tuple[int, ...] = ()


def wanted_end(range_end: int, gap_limit: int, highest_used: int | None) -> int:
    """The window end a descriptor should have (see the module docstring); `range_end` if none."""
    if highest_used is None or highest_used + gap_limit <= range_end:
        return range_end
    grown = max(highest_used + gap_limit, 2 * (range_end + 1) - 1)
    return max(range_end, min(grown, range_end + descriptors.MAX_DERIVE, AUTO_RANGE_END))


def extend_windows(
    rpc: ChainRpc,
    conn: Connection,
    unfinished: Collection[str] = (),
    cancelled: Callable[[], bool] = never,
) -> Grown:
    """Record used indexes and grow the windows that need it. `unfinished` are the subjects the sync
    didn't finish."""
    chain = recorded_chain(conn)
    if chain is None:
        return Grown()
    grown: list[int] = []
    failed: list[int] = []
    full: list[int] = []
    for d in accounts.descriptors(conn):
        if cancelled():
            break
        if "*" not in d.text or descriptor_subject(d) in unfinished:
            continue
        confirmed = accounts.highest_active_index(conn, d.id)
        if confirmed is not None:
            accounts.mark_used(conn, d.id, confirmed)
        used = [i for i in (confirmed, d.highest_used) if i is not None]
        highest = max(used, default=None)
        end = wanted_end(d.range_end, d.gap_limit, highest)
        if end == d.range_end:
            if highest is not None and highest + d.gap_limit > d.range_end:
                full.append(d.id)
            continue
        try:
            _grow(rpc, conn, chain, d, end)
        except DiscoveryError as e:
            log.warning("a descriptor window wasn't grown: %s", e)
            failed.append(d.id)
            continue
        grown.append(d.id)
    if full:
        log.warning("descriptor windows at the largest size, with used indexes near the end: %s", full)
    return Grown(tuple(grown), tuple(failed), tuple(full))


def _grow(rpc: ChainRpc, conn: Connection, chain: str, d: accounts.Descriptor, end: int) -> None:
    # Stored descriptors were checked at import (describe: ranged, solvable); derive reads is_range.
    info = DescriptorInfo(d.text, is_range=True, is_solvable=True)
    try:
        found = descriptors.derive(rpc, info, d.range_end + 1, end)
        derived = [
            (d.range_end + 1 + i, parse_address(text, chain).script_hex, text) for i, text in enumerate(found)
        ]
    except (DescriptorError, AddressError):
        raise DiscoveryError(f"descriptor {d.id}: the node's new addresses can't be used") from None
    try:
        accounts.extend_descriptor(conn, d.id, derived)
    except accounts.AccountsError as e:
        raise DiscoveryError(f"descriptor {d.id}: {e}") from None

"""Growing a ranged descriptor's window as its addresses are used (PLAN §1, §3; T-207, T-210).

A ranged descriptor is imported with one window, indexes 0..gap_limit-1. After each sync, the scripts
of every window that have activity (confirmed, or in the sync's mempool pass) say which indexes are
used. The highest confirmed one is recorded (`storage.accounts.mark_used`). When the highest used
index is within the gap limit of the window's end, the window grows:
- to at least highest used + gap limit, so the gap limit of unused indexes follows the last used one;
- and at least to double its size, so a long-used wallet needs a few wider scans, not one per gap
  (each wider window is a new scan subject, scanned from the descriptor's start: `services.imports`);
- by at most `chain.descriptors.MAX_DERIVE` indexes at once, and never past `MAX_RANGE_END`.

The new scripts come from the node (`deriveaddresses`), like the import's. The sync that follows
scans the wider window, and so on until the gap limit of unused indexes is found.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from coinacct.chain import descriptors
from coinacct.chain.descriptors import DescriptorError, DescriptorInfo
from coinacct.chain.txs import ChainRpc
from coinacct.domain.addresses import AddressError, parse_address
from coinacct.storage import accounts, chain_cache
from coinacct.storage.chain_state import recorded_chain
from coinacct.storage.db import Connection

MAX_RANGE_END: Final = 100_000  # the schema's limit on a window


class DiscoveryError(ValueError):
    """A window couldn't be grown. The message names the descriptor's id only, never its text."""


def wanted_end(range_end: int, gap_limit: int, highest_used: int | None) -> int:
    """The window end a descriptor should have (see the module docstring); `range_end` if none."""
    if highest_used is None or highest_used + gap_limit <= range_end:
        return range_end
    grown = max(highest_used + gap_limit, 2 * (range_end + 1) - 1)
    return min(grown, range_end + descriptors.MAX_DERIVE, MAX_RANGE_END)


def extend_windows(rpc: ChainRpc, conn: Connection, pending_scripts: Iterable[str] = ()) -> list[int]:
    """Record used indexes and grow the windows that need it; returns the ids of the grown ones.
    `pending_scripts` are the scripts with activity in the last mempool pass."""
    chain = recorded_chain(conn)
    if chain is None:
        return []
    pending = set(pending_scripts)
    grown: list[int] = []
    for d in accounts.descriptors(conn):
        if "*" not in d.text:
            continue
        confirmed, unconfirmed = -1, -1
        for index, script in accounts.descriptor_scripts(conn, d.id):
            if chain_cache.activity_for(conn, script):
                confirmed = max(confirmed, index)
            elif script in pending:
                unconfirmed = max(unconfirmed, index)
        if confirmed >= 0:
            accounts.mark_used(conn, d.id, confirmed)
        recorded = -1 if d.highest_used is None else d.highest_used
        highest = max(confirmed, unconfirmed, recorded)
        end = wanted_end(d.range_end, d.gap_limit, None if highest < 0 else highest)
        if end == d.range_end:
            continue
        info = DescriptorInfo(d.text, is_range=True, is_solvable=True)
        try:
            found = descriptors.derive(rpc, info, d.range_end + 1, end)
            derived = [
                (d.range_end + 1 + i, parse_address(text, chain).script_hex, text)
                for i, text in enumerate(found)
            ]
        except (DescriptorError, AddressError):
            raise DiscoveryError(f"descriptor {d.id}: the node's new addresses can't be used") from None
        try:
            accounts.extend_descriptor(conn, d.id, derived)
        except accounts.AccountsError as e:
            raise DiscoveryError(f"descriptor {d.id}: {e}") from None
        grown.append(d.id)
    return grown

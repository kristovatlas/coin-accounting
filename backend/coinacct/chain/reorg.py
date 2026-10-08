"""Fork-point reorg handling (PLAN §1 "Reorgs and new blocks"; architecture §8, the tip-change
sequence; THREAT_MODEL T-207).

On startup and on every tip change, `catch_up` compares the node's tip with the reference tip (the
scan target of an unfinished catch-up, else the last-seen tip). If the reference tip has left the
active chain, it walks back with `getblockheader` while `confirmations == -1` to the fork point, at
any depth, and removes every cached row, snapshot and stretch of coverage above it
(`storage.chain_cache.invalidate_above`). Then it records the node's tip as the scan target. Both
happen in one transaction. The caller extends coverage to the target and then calls
`complete_scan_target`, which makes it the last-seen tip (architecture §8); until then, the target
stays and the catch-up is retried.

A reference tip the node doesn't know at all (a resynced node, or a different one) can't be walked
back from, so everything above the genesis block is invalidated. A node still connecting blocks it
has headers for (initial sync, a reindex) is refused before anything is walked or invalidated: its
active chain is behind, so the blocks above it would look reorged (`NodeSyncingError`).

The node can switch branches while this runs. So the node's tip is recorded as the target only if
it descends from the fork point, checked on the headers' immutable `previousblockhash` links;
otherwise `TipMovedError`, and the caller tries again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coinacct.chain.txs import RPC_NOT_FOUND, ChainRpc
from coinacct.domain.chain import is_hash
from coinacct.rpc import RpcCallError
from coinacct.storage.chain_cache import (
    Invalidated,
    StaleTipError,
    invalidate_above,
    reference_tip,
    scan_target,
    set_scan_target,
)
from coinacct.storage.chain_state import Tip, last_tip
from coinacct.storage.db import Connection, transaction


class MalformedHeaderError(ValueError):
    """A block header reply didn't have the expected shape."""


class TipMovedError(RuntimeError):
    """The node's tip moved, or it switched branches, while a catch-up read it: try again."""


class NodeSyncingError(RuntimeError):
    """The node is still connecting blocks it has headers for (initial sync, a reindex). Nothing was
    walked or invalidated; wait for the node (offline mode, ADR 0004)."""


@dataclass(frozen=True, slots=True)
class TipChange:
    """The reference tip moved from `old` to the scan target `new`. `invalidated` is what a reorg
    removed, if any."""

    old: Tip | None
    new: Tip
    invalidated: Invalidated | None


def node_tip(rpc: ChainRpc) -> Tip:
    info = rpc.call("getblockchaininfo")
    syncing, blocks, headers = (
        (info.get("initialblockdownload"), info.get("blocks"), info.get("headers"))
        if isinstance(info, dict)
        else (None, None, None)
    )
    if type(syncing) is not bool or type(blocks) is not int or type(headers) is not int:
        raise MalformedHeaderError("getblockchaininfo didn't report the node's sync state")
    if syncing or blocks < headers:
        raise NodeSyncingError("the node is still syncing its chain")
    blockhash = rpc.call("getbestblockhash")
    if not is_hash(blockhash):
        raise MalformedHeaderError("getbestblockhash didn't return a block hash")
    header = _header(rpc, blockhash)
    if header is None:
        raise MalformedHeaderError("the node doesn't know its own tip")
    height, confirmations, _ = header
    if confirmations < 1:
        raise TipMovedError("the node's tip changed while it was read")
    return Tip(blockhash, height)


def fork_point(rpc: ChainRpc, tip: Tip) -> Tip:
    """The newest block at or below `tip` that is still in the active chain: `tip` itself if no reorg
    removed it. The walk goes back one block at a time, so a reorg of any depth is found."""
    blockhash, height = tip.blockhash, tip.height
    while True:
        header = _header(rpc, blockhash)
        if header is None:
            return _genesis(rpc)
        got_height, confirmations, prev = header
        if got_height != height:
            raise MalformedHeaderError("a block header reported an unexpected height")
        if confirmations >= 1:
            return Tip(blockhash, height)
        if prev is None:  # `_header` gives a previous block for every height but 0
            raise MalformedHeaderError("the genesis block can't leave the active chain")
        blockhash, height = prev, height - 1


def catch_up(rpc: ChainRpc, conn: Connection) -> TipChange | None:
    """Start catching up to the node's tip: invalidate what a reorg removed and record the node's tip
    as the scan target. Returns what changed, including a catch-up still unfinished from before
    (`old` is then the last-seen tip): the caller extends coverage to `new` and calls
    `complete_scan_target`. None only when everything is caught up to the node's tip. The chain
    must already be recorded (T-206). Another caller moving the reference tip meanwhile is
    `StaleTipError`."""
    new = node_tip(rpc)
    old = reference_tip(conn)
    if old == new:
        return None if scan_target(conn) is None else TipChange(last_tip(conn), new, None)
    # The walks happen before the transaction: a write lock isn't held across RPC calls.
    fork = None if old is None else fork_point(rpc, old)
    if fork is not None and fork.height > 0 and not _descends(rpc, new, fork):
        raise TipMovedError("the node switched branches during the catch-up")
    with transaction(conn):
        if reference_tip(conn) != old:
            raise StaleTipError("another catch-up moved the reference tip meanwhile (T-207)")
        invalidated = None if fork is None or fork == old else invalidate_above(conn, fork)
        set_scan_target(conn, new)
    return TipChange(old, new, invalidated)


def _descends(rpc: ChainRpc, tip: Tip, ancestor: Tip) -> bool:
    """Whether `ancestor` is on `tip`'s chain, walking `tip`'s headers back. A header's previous
    block never changes, so a branch switch on the node can't make this answer wrong."""
    blockhash, height = tip.blockhash, tip.height
    while height > ancestor.height:
        header = _header(rpc, blockhash)
        if header is None or header[0] != height or header[2] is None:
            raise TipMovedError("the node's tip is no longer known")
        blockhash, height = header[2], height - 1
    return height == ancestor.height and blockhash == ancestor.blockhash


def _header(rpc: ChainRpc, blockhash: str) -> tuple[int, int, str | None] | None:
    """(height, confirmations, previous block hash) of a block, or None if the node doesn't know it.
    Confirmations are -1 for a block on a side branch and at least 1 for an active one."""
    try:
        header: Any = rpc.call("getblockheader", [blockhash, True])
    except RpcCallError as e:
        if e.code == RPC_NOT_FOUND:
            return None
        raise
    if not isinstance(header, dict) or header.get("hash") != blockhash:
        raise MalformedHeaderError("getblockheader returned a different block")
    height, confirmations, prev = (
        header.get("height"),
        header.get("confirmations"),
        header.get("previousblockhash"),
    )
    if type(height) is not int or height < 0:
        raise MalformedHeaderError("getblockheader didn't report a height")
    if type(confirmations) is not int or (confirmations != -1 and confirmations < 1):
        raise MalformedHeaderError("getblockheader didn't report a valid confirmation count")
    if (prev is None) != (height == 0) or (prev is not None and not is_hash(prev)):
        raise MalformedHeaderError("getblockheader's previous block hash doesn't fit its height")
    return height, confirmations, prev


def _genesis(rpc: ChainRpc) -> Tip:
    blockhash = rpc.call("getblockhash", [0])
    if not is_hash(blockhash):
        raise MalformedHeaderError("getblockhash didn't return a block hash")
    return Tip(blockhash, 0)

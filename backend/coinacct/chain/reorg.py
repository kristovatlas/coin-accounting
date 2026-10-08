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
back from, so everything above the genesis block is invalidated. A tip that moves while this runs is
caught on the next call, which walks back from the target recorded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coinacct.chain.txs import RPC_NOT_FOUND, ChainRpc
from coinacct.domain.chain import is_hash
from coinacct.rpc import RpcCallError
from coinacct.storage.chain_cache import Invalidated, invalidate_above, reference_tip, set_scan_target
from coinacct.storage.chain_state import Tip
from coinacct.storage.db import Connection, transaction


class MalformedHeaderError(ValueError):
    """A block header reply didn't have the expected shape."""


@dataclass(frozen=True, slots=True)
class TipChange:
    """The reference tip moved from `old` to the scan target `new`. `invalidated` is what a reorg
    removed, if any."""

    old: Tip | None
    new: Tip
    invalidated: Invalidated | None


def node_tip(rpc: ChainRpc) -> Tip:
    blockhash = rpc.call("getbestblockhash")
    if not is_hash(blockhash):
        raise MalformedHeaderError("getbestblockhash didn't return a block hash")
    header = _header(rpc, blockhash)
    if header is None:
        raise MalformedHeaderError("the node doesn't know its own tip")
    height, confirmations, _ = header
    if confirmations < 1:
        raise MalformedHeaderError("the node's tip isn't in its active chain")
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
    as the scan target. None when the reference tip already is the node's tip. The chain must
    already be recorded (T-206)."""
    new = node_tip(rpc)
    old = reference_tip(conn)
    if old == new:
        return None
    # The walk happens before the transaction: a write lock isn't held across RPC calls.
    fork = None if old is None else fork_point(rpc, old)
    with transaction(conn):
        if reference_tip(conn) != old:
            return None  # another caller moved it meanwhile; the next call starts from there
        invalidated = None if fork is None or fork == old else invalidate_above(conn, fork)
        set_scan_target(conn, new)
    return TipChange(old, new, invalidated)


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

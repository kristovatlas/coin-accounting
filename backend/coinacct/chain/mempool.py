"""The mempool pass: unconfirmed activity, kept apart from the block-backed cache (PLAN §1
"Unconfirmed activity"; THREAT_MODEL T-207).

One `getdescriptoractivity [] [scanobjects] true` call returns the mempool's receives and spends for
the scripts, and only those: no block is asked for. The result is never stored. It is read again on
every refresh, so a transaction that was replaced (RBF), evicted or confirmed in the meantime simply
isn't there any more. Confirmed activity comes only from the scan protocol (`chain/scans`).
Unconfirmed spends of a single output come from `chain/spenders` (`SPENT_UNCONFIRMED`).

The pass belongs to one tip: the caller passes the reference tip its cache has caught up to, and the
node's best block must be that tip before and after the call (`StaleScanError` otherwise). A block
that arrived in between would otherwise move a transaction out of the mempool before it reached the
cache, and a reorg would put a cached one back, so it would be counted twice (T-207, T-506). A busy
script's mempool activity is bounded (`MAX_PENDING`, T-205).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from coinacct.chain.scans import ActivityBudgetError, MalformedScanError, StaleScanError, event_body
from coinacct.chain.txs import ChainRpc
from coinacct.domain.chain import Outpoint, is_hash
from coinacct.storage.chain_state import Tip

# The most unconfirmed events one pass returns (T-205): a busy script's mempool can be large.
MAX_PENDING = 2_000


@dataclass(frozen=True, slots=True)
class PendingActivity:
    """An unconfirmed receive (`n` is the output index) or spend (`n` is the input index, `prevout`
    the output it spends) of `script_hex`. Shown as unconfirmed; never a tax event (T-506)."""

    kind: Literal["receive", "spend"]
    script_hex: str
    txid: str
    n: int
    sats: int
    prevout: Outpoint | None = None


def pending_activity(rpc: ChainRpc, scanobjects: Sequence[Any], tip: Tip) -> list[PendingActivity]:
    """The mempool's activity for `scanobjects` on top of `tip`, the reference tip the cache has
    caught up to."""
    _at_tip(rpc, tip)
    result = rpc.call("getdescriptoractivity", [[], list(scanobjects), True])
    _at_tip(rpc, tip)
    if not isinstance(result, dict) or not isinstance(result.get("activity"), list):
        raise MalformedScanError("getdescriptoractivity didn't return an activity list")
    if len(result["activity"]) > MAX_PENDING:
        raise ActivityBudgetError(len(result["activity"]), MAX_PENDING)
    events = []
    seen: set[tuple[str, str, int]] = set()
    spent: set[Outpoint] = set()
    for raw in result["activity"]:
        if not isinstance(raw, dict):
            raise MalformedScanError("an activity event isn't an object")
        if "blockhash" in raw or "height" in raw:
            raise MalformedScanError("a mempool event names a block")
        kind, script_hex, txid, n, sats, prevout = event_body(raw)
        if (kind, txid, n) in seen:
            raise MalformedScanError("the mempool reported the same event twice")
        seen.add((kind, txid, n))
        if prevout is not None:
            if prevout in spent:  # the mempool never holds two spends of one output
                raise MalformedScanError("the mempool reported two spends of one output")
            spent.add(prevout)
        events.append(PendingActivity(kind, script_hex, txid, n, sats, prevout))
    return events


def _at_tip(rpc: ChainRpc, tip: Tip) -> None:
    best = rpc.call("getbestblockhash")
    if not is_hash(best):
        raise MalformedScanError("getbestblockhash didn't return a block hash")
    if best != tip.blockhash:
        raise StaleScanError("the node's tip isn't the cache's: catch up before the mempool pass")

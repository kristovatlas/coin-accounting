"""The mempool pass: unconfirmed activity, kept apart from the block-backed cache (PLAN §1
"Unconfirmed activity"; THREAT_MODEL T-207).

One `getdescriptoractivity [] [scanobjects] true` call returns the mempool's receives and spends for
the scripts, and only those: no block is asked for. The result is never stored. It is read again on
every refresh, so a transaction that was replaced (RBF), evicted or confirmed in the meantime simply
isn't there any more. Confirmed activity comes only from the scan protocol (`chain/scans`).
Unconfirmed spends of a single output come from `chain/spenders` (`SPENT_UNCONFIRMED`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from coinacct.chain.scans import MalformedScanError, event_body
from coinacct.chain.txs import ChainRpc
from coinacct.domain.chain import Outpoint


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


def pending_activity(rpc: ChainRpc, scanobjects: Sequence[Any]) -> list[PendingActivity]:
    """The mempool's activity for `scanobjects`, as the node sees it now."""
    result = rpc.call("getdescriptoractivity", [[], list(scanobjects), True])
    if not isinstance(result, dict) or not isinstance(result.get("activity"), list):
        raise MalformedScanError("getdescriptoractivity didn't return an activity list")
    events = []
    seen: set[tuple[str, str, int]] = set()
    for raw in result["activity"]:
        if not isinstance(raw, dict):
            raise MalformedScanError("an activity event isn't an object")
        if "blockhash" in raw or "height" in raw:
            raise MalformedScanError("a mempool event names a block")
        kind, script_hex, txid, n, sats, prevout = event_body(raw)
        if (kind, txid, n) in seen:
            raise MalformedScanError("the mempool reported the same event twice")
        seen.add((kind, txid, n))
        events.append(PendingActivity(kind, script_hex, txid, n, sats, prevout))
    return events

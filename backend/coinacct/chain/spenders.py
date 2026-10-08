"""Forward expansion: which transaction spent an output (PLAN §1 "Forward expansion" and "Is this output
spent?"; THREAT_MODEL T-205, T-210).

One `gettxspendingprevout` call answers a batch of outputs. Core checks the mempool, then
`txospenderindex`, and waits for the index to catch up with the chain before answering. The app
always passes `mempool_only: false`, so a missing index is an error rather than a silent "unspent".

- **Unspendable outputs** (OP_RETURN, over-long scripts) never enter the UTXO set, so they are
  answered here without asking the node: they are terminal.
- **Only outputs that exist.** Core answers "unspent" for an outpoint it has never heard of, so the
  API takes a fetched `Tx` and an output index, never a bare outpoint.
- **"Unspent" is a snapshot**, true at the tip the caller observed. The cache records it with that
  tip hash, never as a fact (PLAN §1 "Caching and chain state").
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from coinacct.chain.txs import ChainRpc, MalformedTxError
from coinacct.domain.chain import Outpoint, Tx, is_hash

# Outputs per call: well under the RPC client's response cap, and small enough to fail fast.
MAX_BATCH: Final = 100


class SpendState(enum.Enum):
    UNSPENDABLE = "unspendable"  # terminal: never in the UTXO set
    UNSPENT = "unspent"  # as of the tip the caller observed
    SPENT_UNCONFIRMED = "spent_unconfirmed"  # by a mempool transaction, which may still be replaced
    SPENT = "spent"  # by a confirmed transaction


@dataclass(frozen=True, slots=True)
class Spend:
    outpoint: Outpoint
    state: SpendState
    spending_txid: str | None = None
    blockhash: str | None = None


def spends(rpc: ChainRpc, txs_and_outputs: Sequence[tuple[Tx, int]]) -> list[Spend]:
    """What spent each `(tx, output index)`, in the order given."""
    results: list[Spend | None] = []
    ask: list[tuple[int, Outpoint]] = []
    for tx, n in txs_and_outputs:
        if not 0 <= n < len(tx.outputs):
            raise IndexError("the transaction has no such output")
        outpoint = Outpoint(tx.txid, n)
        if tx.outputs[n].unspendable:
            results.append(Spend(outpoint, SpendState.UNSPENDABLE))
        else:
            results.append(None)
            ask.append((len(results) - 1, outpoint))
    for start in range(0, len(ask), MAX_BATCH):
        batch = ask[start : start + MAX_BATCH]
        answers = _ask(rpc, [o for _, o in batch])
        for (slot, _), answer in zip(batch, answers, strict=True):
            results[slot] = answer
    return [r for r in results if r is not None]


def spend_of(rpc: ChainRpc, tx: Tx, n: int) -> Spend:
    [result] = spends(rpc, [(tx, n)])
    return result


def _ask(rpc: ChainRpc, outpoints: list[Outpoint]) -> list[Spend]:
    reply = rpc.call(
        "gettxspendingprevout",
        [[{"txid": o.txid, "vout": o.vout} for o in outpoints], {"mempool_only": False}],
    )
    if not isinstance(reply, list) or len(reply) != len(outpoints):
        raise MalformedTxError("gettxspendingprevout didn't answer every output")
    return [_spend(o, item) for o, item in zip(outpoints, reply, strict=True)]


def _spend(outpoint: Outpoint, item: Any) -> Spend:
    if not isinstance(item, dict) or item.get("txid") != outpoint.txid or item.get("vout") != outpoint.vout:
        raise MalformedTxError("gettxspendingprevout answered for a different output")
    spending, block = item.get("spendingtxid"), item.get("blockhash")
    if spending is None:
        if block is not None:
            raise MalformedTxError("a spending block without a spending transaction")
        return Spend(outpoint, SpendState.UNSPENT)
    if not is_hash(spending) or (block is not None and not is_hash(block)):
        raise MalformedTxError("a spender isn't a hash")
    if block is None:
        return Spend(outpoint, SpendState.SPENT_UNCONFIRMED, spending)
    return Spend(outpoint, SpendState.SPENT, spending, block)

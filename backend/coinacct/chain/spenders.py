"""Forward expansion: which transaction spent an output (PLAN §1 "Forward expansion" and "Is this output
spent?"; THREAT_MODEL T-205, T-207, T-208, T-210).

One `gettxspendingprevout` call answers a batch of outputs. Core checks the mempool, then
`txospenderindex`, and waits for the index to catch up with the chain before answering. The app
always passes `mempool_only: false`, so a missing index is an error rather than a silent "unspent".

- **Unspendable outputs** (OP_RETURN, over-long scripts) never enter the UTXO set, so they are
  answered here without asking the node: they are terminal.
- **Only outputs that exist.** Core answers "unspent" for an outpoint it has never heard of, so the
  API takes a fetched `Tx` and an output index, never a bare outpoint.
- **"Unspent" is a snapshot**, true at a tip. The caller reads the tip **before** calling `spends`
  and records each "unspent" with that tip hash, never as a fact (PLAN §1 "Caching and chain
  state"): a block that arrives in between then only makes the snapshot stale, never wrong for its tip.
- **A spender's block** is Core's answer. The cache stores it with the row and re-checks it on every
  tip change, like every block-backed row (T-207).
- **BIP30 (T-208):** two early mainnet coinbases share their txids with later ones, and Core's
  indexes keep only the later. The earlier copies' outputs were overwritten and can never be spent,
  so they are answered `UNSPENDABLE` here, by block hash, without asking the node.
- **Budget (T-205):** at most `MAX_OUTPUTS` outputs per call (`BudgetExceededError` beyond).
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from coinacct.chain.txs import BudgetExceededError, ChainRpc, MalformedTxError
from coinacct.domain.chain import Outpoint, Tx, is_hash

# Outputs per RPC call: well under the RPC client's response cap, and small enough to fail fast.
MAX_BATCH: Final = 100
# Outputs per `spends` call: about the outputs of a standard (100 kvB) transaction (T-205).
MAX_OUTPUTS: Final = 4000
# BIP30: the duplicate coinbase txids, each with the later block, whose copy Core's indexes keep
# (Bitcoin Core src/validation.cpp, the two BIP30 exceptions at heights 91842 and 91880).
BIP30_LATER: Final = {
    "d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599": (
        "00000000000a4d0a398161ffc163c503763b1f4360639393e0e4c8e300e0caec"
    ),
    "e3bf3d07d4b0375638d5f1db5255fe07ba2c4cb067cd81b84ee974b6585fb468": (
        "00000000000743f190a18c5577a3c2d2a1f610ae9601ac046a38084ccb7cd721"
    ),
}


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

    def __post_init__(self) -> None:
        spender = self.state in (SpendState.SPENT_UNCONFIRMED, SpendState.SPENT)
        if spender != (self.spending_txid is not None) or (self.state is SpendState.SPENT) != (
            self.blockhash is not None
        ):
            raise ValueError("a spend's fields don't match its state")


def spends(rpc: ChainRpc, txs_and_outputs: Sequence[tuple[Tx, int]]) -> list[Spend]:
    """What spent each `(tx, output index)`, in the order given."""
    if len(txs_and_outputs) > MAX_OUTPUTS:
        raise BudgetExceededError("too many outputs to look up in one call")
    results: list[Spend | None] = []
    ask: list[tuple[int, Outpoint]] = []
    for tx, n in txs_and_outputs:
        if not 0 <= n < len(tx.outputs):
            raise IndexError("the transaction has no such output")
        outpoint = Outpoint(tx.txid, n)
        if tx.outputs[n].unspendable or _overwritten(tx):
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


def _overwritten(tx: Tx) -> bool:
    """The earlier copy of a BIP30 duplicate coinbase: same txid, not the block Core kept."""
    later = BIP30_LATER.get(tx.txid)
    return later is not None and tx.blockhash is not None and tx.blockhash != later


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
    vout = item.get("vout") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or item.get("txid") != outpoint.txid
        or type(vout) is not int
        or vout != outpoint.vout
    ):
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

"""Fetching transactions from the node (PLAN §1 "Transaction details, and backward expansion";
THREAT_MODEL T-205, T-207, T-208, T-502).

`fetch_tx` calls `getrawtransaction <txid> 2 [<blockhash>]`: Core decodes, so there is no binary
parser here (T-205). The reply is checked field by field, and anything of the wrong shape is a
`MalformedTxError`, never a guess.

- **BIP30 (T-208):** a confirmed transaction is fetched with its block hash, so a txid that occurs
  twice (the two duplicate coinbases) resolves to the right one.
- **Reorgs (T-207):** a block that has left the active chain (`in_active_chain: false`), or that the
  node doesn't know at all, is a `StaleBlockError`, so cached rows from it are invalidated rather
  than read as "this transaction doesn't exist".
- **Prevouts:** Core includes the spent outputs only for confirmed transactions. For an unconfirmed
  one, `fill_prevouts` fetches each parent, up to `MAX_PARENTS` (T-205).
- **Amounts (T-502):** every amount is exact satoshis; an output total or a spent-output total above
  21M BTC, and a fee below zero once the spent outputs are known, are malformed.
- **Not found:** the genesis coinbase, and any txid the node doesn't know, are `TxNotFoundError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final, Protocol, TypeIs

from coinacct.domain.chain import MAX_SATS, Outpoint, Tx, TxIn, TxOut, btc_to_sats, is_hash, is_hex
from coinacct.rpc import RpcCallError

# Core's RPC_INVALID_ADDRESS_OR_KEY, returned when getrawtransaction doesn't know the txid.
RPC_NOT_FOUND: Final = -5
MAX_SEQUENCE: Final = 0xFFFFFFFF
# Distinct parents `fill_prevouts` fetches for one transaction (T-205: a budget, failing closed).
# About as many inputs as a standard (100 kvB) transaction can have, so an ordinary consolidation fits.
MAX_PARENTS: Final = 2500


class ChainRpc(Protocol):
    """The part of `coinacct.rpc.RpcClient` this module uses."""

    def call(self, method: str, params: Any = ()) -> Any: ...


class MalformedTxError(ValueError):
    """The node's reply didn't have the shape of a decoded transaction."""


class TxNotFoundError(LookupError):
    """The node doesn't have this transaction (or it is the genesis coinbase, which has no tx)."""


class StaleBlockError(LookupError):
    """The transaction was asked for in a block that is no longer in the active chain, or that the
    node doesn't know (a reorg, or a different or resynced node): cached rows from it are stale."""


class BudgetExceededError(RuntimeError):
    """A lookup would need more node calls than its budget allows (T-205)."""


def fetch_tx(rpc: ChainRpc, txid: str, blockhash: str | None = None) -> Tx:
    """The decoded transaction. Pass the block hash for a confirmed transaction (BIP30)."""
    if not is_hash(txid) or (blockhash is not None and not is_hash(blockhash)):
        raise ValueError("a txid and a block hash are 64 lowercase hex digits")
    params: list[Any] = [txid, 2] if blockhash is None else [txid, 2, blockhash]
    try:
        raw = rpc.call("getrawtransaction", params)
    except RpcCallError as e:
        if e.code != RPC_NOT_FOUND:
            raise
        # With a block hash, -5 also means "no such block": tell that apart, since it means the
        # cached row is stale, not that the transaction doesn't exist.
        if blockhash is not None and not _block_active(rpc, blockhash):
            raise StaleBlockError("the node doesn't have that block in its active chain") from None
        raise TxNotFoundError("the node doesn't have this transaction") from None
    tx = parse_tx(raw)
    if tx.txid != txid:
        raise MalformedTxError("the node returned a different transaction")
    if blockhash is not None:
        if tx.blockhash != blockhash:
            raise MalformedTxError("the node returned the transaction from a different block")
        active = raw.get("in_active_chain") if isinstance(raw, dict) else None
        if not isinstance(active, bool):
            raise MalformedTxError("in_active_chain is missing or not a bool")
        if not active:
            raise StaleBlockError("that block is no longer in the active chain")
        if tx.confirmations < 1 or tx.block_time is None:
            raise MalformedTxError("a transaction in an active block needs confirmations and a block time")
    return tx


def _block_active(rpc: ChainRpc, blockhash: str) -> bool:
    """Whether the node knows the block and it is in the active chain. `confirmations` is -1 for a
    known block on a side branch and at least 1 for an active one; anything else is malformed. The
    answer holds until the next tip change, which re-checks cached rows anyway (T-207)."""
    try:
        header = rpc.call("getblockheader", [blockhash, True])
    except RpcCallError as e:
        if e.code == RPC_NOT_FOUND:
            return False
        raise
    confirmations = header.get("confirmations") if isinstance(header, dict) else None
    if not _is_int(confirmations) or (confirmations != -1 and confirmations < 1):
        raise MalformedTxError("getblockheader didn't report a valid confirmation count")
    return confirmations >= 1


def fill_prevouts(rpc: ChainRpc, tx: Tx) -> Tx:
    """`tx` with every spent output known, fetching parents where Core didn't include them, which is
    what it does for unconfirmed transactions. A parent is fetched without a block hash: Core finds it
    through `txindex` or the mempool. Only a BIP30 duplicate coinbase is ambiguous, and `txindex`
    keeps the later of the two, the one whose outputs can still be spent (T-208).

    Budgets (T-205): more than `MAX_PARENTS` distinct parents is a `BudgetExceededError`, raised before
    any parent is fetched (callers are expected to show such a transaction as pending with its fee
    unknown). Only the outputs the inputs spend are kept; each parent is dropped once read, so memory
    stays bounded by the transaction, not by its parents' sizes."""
    if tx.prevouts_known:
        return tx
    wanted: dict[str, set[int]] = {}
    for i in tx.inputs:
        if i.coinbase or i.spent is not None:
            continue
        if i.prevout is None:  # unreachable: only a coinbase input has no prevout
            raise MalformedTxError("an input without a prevout")
        wanted.setdefault(i.prevout.txid, set()).add(i.prevout.vout)
    if len(wanted) > MAX_PARENTS:
        raise BudgetExceededError("the transaction spends from too many parents to look up")
    found: dict[Outpoint, TxOut] = {}
    for txid, vouts in wanted.items():
        outputs = fetch_tx(rpc, txid).outputs
        for n in vouts:
            if n >= len(outputs):
                raise MalformedTxError("an input spends an output its parent doesn't have")
            found[Outpoint(txid, n)] = outputs[n]
    inputs = tuple(
        i
        if i.coinbase or i.spent is not None or i.prevout is None
        else TxIn(i.prevout, i.sequence, found[i.prevout])
        for i in tx.inputs
    )
    return _checked(Tx(tx.txid, tx.blockhash, tx.confirmations, tx.block_time, inputs, tx.outputs))


def _checked(tx: Tx) -> Tx:
    """T-502: neither the outputs nor, once known, the spent outputs can total more than all bitcoin,
    and the outputs can't exceed what they spend."""
    if sum(o.sats for o in tx.outputs) > MAX_SATS:
        raise MalformedTxError("the outputs total more than 21 million BTC")
    if sum(i.spent.sats for i in tx.inputs if i.spent is not None) > MAX_SATS:
        raise MalformedTxError("the spent outputs total more than 21 million BTC")
    fee = tx.fee_sats
    if fee is not None and fee < 0:
        raise MalformedTxError("the outputs are worth more than the outputs they spend")
    return tx


def parse_tx(raw: object) -> Tx:
    """Pure: Core's verbosity-2 transaction object, checked and converted."""
    obj = _object(raw, "transaction")
    txid = obj.get("txid")
    if not is_hash(txid):
        raise MalformedTxError("txid isn't a hash")
    blockhash = obj.get("blockhash")
    if blockhash is not None and not is_hash(blockhash):
        raise MalformedTxError("blockhash isn't a hash")
    confirmations = obj.get("confirmations", 0)
    if not _is_int(confirmations) or confirmations < 0 or (blockhash is None and confirmations):
        raise MalformedTxError("confirmations don't match the block")
    block_time = obj.get("blocktime")
    if block_time is not None and (not _is_int(block_time) or block_time < 0):
        raise MalformedTxError("blocktime isn't a timestamp")
    vin, vout = obj.get("vin"), obj.get("vout")
    if not isinstance(vin, list) or not vin or not isinstance(vout, list) or not vout:
        raise MalformedTxError("a transaction needs inputs and outputs")
    outputs = []
    for n, raw_out in enumerate(vout):
        out = _object(raw_out, "vout")
        if out.get("n") != n:
            raise MalformedTxError("outputs are out of order")
        outputs.append(_txout(out.get("value"), out.get("scriptPubKey")))
    inputs = tuple(_txin(_object(i, "vin"), coinbase_allowed=len(vin) == 1) for i in vin)
    return _checked(
        Tx(
            txid,
            blockhash,
            confirmations,
            block_time if blockhash is not None else None,
            inputs,
            tuple(outputs),
        )
    )


def _txin(obj: Mapping[str, Any], *, coinbase_allowed: bool) -> TxIn:
    sequence = obj.get("sequence")
    if not _is_int(sequence) or not 0 <= sequence <= MAX_SEQUENCE:
        raise MalformedTxError("an input's sequence isn't a 32-bit number")
    if "coinbase" in obj:
        if not coinbase_allowed or not is_hex(obj["coinbase"]):
            raise MalformedTxError("a malformed coinbase input")
        return TxIn(None, sequence)
    txid, n = obj.get("txid"), obj.get("vout")
    if not is_hash(txid) or not _is_int(n) or n < 0:
        raise MalformedTxError("an input doesn't name the output it spends")
    prev = obj.get("prevout")
    spent = None
    if prev is not None:
        p = _object(prev, "prevout")
        spent = _txout(p.get("value"), p.get("scriptPubKey"))
    return TxIn(Outpoint(txid, n), sequence, spent)


def _txout(value: object, script: object) -> TxOut:
    if not isinstance(value, Decimal):
        raise MalformedTxError("an amount isn't a decimal number")
    try:
        sats = btc_to_sats(value)
    except ValueError as e:
        raise MalformedTxError(str(e)) from None
    spk = _object(script, "scriptPubKey")
    script_hex, script_type, address = spk.get("hex"), spk.get("type"), spk.get("address")
    if not is_hex(script_hex) or not isinstance(script_type, str) or not script_type:
        raise MalformedTxError("an output script is malformed")
    if address is not None and not isinstance(address, str):
        raise MalformedTxError("an output address isn't a string")
    return TxOut(sats, script_hex, script_type, address)


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MalformedTxError(f"{what} isn't an object")
    return value


def _is_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)

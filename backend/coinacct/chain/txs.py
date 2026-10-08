"""Fetching transactions from the node (PLAN §1 "Transaction details, and backward expansion";
THREAT_MODEL T-205, T-210).

`fetch_tx` calls `getrawtransaction <txid> 2 [<blockhash>]`: Core decodes, so there is no binary
parser here (T-205). The reply is checked field by field, and anything of the wrong shape is a
`MalformedTxError`, never a guess.

- **BIP30:** a confirmed transaction is fetched with its block hash, so a txid that occurs twice
  (the two duplicate coinbases) resolves to the right one, and so a reorged-away block is noticed:
  Core then reports `in_active_chain: false`, which is an error here (`StaleBlockError`).
- **Prevouts:** Core includes the spent outputs only for confirmed transactions. For an unconfirmed
  one, `fill_prevouts` fetches each parent.
- **Not found:** the genesis coinbase, and any txid the node doesn't know, are `TxNotFoundError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final, Protocol, TypeIs

from coinacct.domain.chain import Outpoint, Tx, TxIn, TxOut, btc_to_sats, is_hash, is_hex
from coinacct.rpc import RpcCallError

# Core's RPC_INVALID_ADDRESS_OR_KEY, returned when getrawtransaction doesn't know the txid.
RPC_NOT_FOUND: Final = -5
MAX_SEQUENCE: Final = 0xFFFFFFFF


class ChainRpc(Protocol):
    """The part of `coinacct.rpc.RpcClient` this module uses."""

    def call(self, method: str, params: Any = ()) -> Any: ...


class MalformedTxError(ValueError):
    """The node's reply didn't have the shape of a decoded transaction."""


class TxNotFoundError(LookupError):
    """The node doesn't have this transaction (or it is the genesis coinbase, which has no tx)."""


class StaleBlockError(LookupError):
    """The transaction was asked for in a block that is no longer in the active chain (a reorg)."""


def fetch_tx(rpc: ChainRpc, txid: str, blockhash: str | None = None) -> Tx:
    """The decoded transaction. Pass the block hash for a confirmed transaction (BIP30)."""
    if not is_hash(txid) or (blockhash is not None and not is_hash(blockhash)):
        raise ValueError("a txid and a block hash are 64 lowercase hex digits")
    params: list[Any] = [txid, 2] if blockhash is None else [txid, 2, blockhash]
    try:
        raw = rpc.call("getrawtransaction", params)
    except RpcCallError as e:
        if e.code == RPC_NOT_FOUND:
            raise TxNotFoundError("the node doesn't have this transaction") from None
        raise
    tx = parse_tx(raw)
    if tx.txid != txid:
        raise MalformedTxError("the node returned a different transaction")
    if blockhash is not None:
        if tx.blockhash != blockhash:
            raise MalformedTxError("the node returned the transaction from a different block")
        if not isinstance(raw, dict) or raw.get("in_active_chain") is not True:
            raise StaleBlockError("that block is no longer in the active chain")
    return tx


def fill_prevouts(rpc: ChainRpc, tx: Tx) -> Tx:
    """`tx` with every spent output known, fetching parents where Core didn't include them (unconfirmed
    transactions). A parent is fetched without a block hash: Core finds it through `txindex` or the
    mempool, and only a BIP30 coinbase is ambiguous, which can't be the parent of a mempool tx."""
    if tx.prevouts_known:
        return tx
    parents: dict[str, Tx] = {}
    inputs = []
    for i in tx.inputs:
        if i.coinbase or i.spent is not None:
            inputs.append(i)
            continue
        if i.prevout is None:  # unreachable: only a coinbase input has no prevout
            raise MalformedTxError("an input without a prevout")
        parent = parents.get(i.prevout.txid)
        if parent is None:
            parent = parents[i.prevout.txid] = fetch_tx(rpc, i.prevout.txid)
        if i.prevout.vout >= len(parent.outputs):
            raise MalformedTxError("an input spends an output its parent doesn't have")
        inputs.append(TxIn(i.prevout, i.sequence, parent.outputs[i.prevout.vout]))
    return Tx(tx.txid, tx.blockhash, tx.confirmations, tx.block_time, tuple(inputs), tx.outputs)


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
    return Tx(
        txid, blockhash, confirmations, block_time if blockhash is not None else None, inputs, tuple(outputs)
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

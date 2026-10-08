"""Chain data as the app holds it: transactions, outputs and outpoints (PLAN §1; THREAT_MODEL T-205, T-502).

Pure types. Amounts are integer satoshis, converted exactly from the node's BTC `Decimal`s; a value
that isn't a whole number of satoshis, or is out of range, is refused rather than rounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, TypeIs

SATS_PER_BTC: Final = 100_000_000
MAX_SATS: Final = 21_000_000 * SATS_PER_BTC
# Core's MAX_SCRIPT_SIZE: a longer output script can never be spent, so it never enters the UTXO set.
MAX_SCRIPT_BYTES: Final = 10_000
OP_RETURN: Final = "6a"

_HASH = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"(?:[0-9a-f]{2})*")


def is_hash(value: object) -> TypeIs[str]:
    """A txid or block hash as Core prints it: 64 lowercase hex digits."""
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def is_hex(value: object) -> TypeIs[str]:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def btc_to_sats(value: Decimal) -> int:
    """Exact: 0.00000001 BTC is 1 sat. Raises ValueError for anything that isn't a whole, in-range
    number of satoshis (no rounding, T-502)."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("an amount must be a finite Decimal")
    sats = value * SATS_PER_BTC
    if sats != sats.to_integral_value():
        raise ValueError("an amount isn't a whole number of satoshis")
    result = int(sats)
    if not 0 <= result <= MAX_SATS:
        raise ValueError("an amount is out of range")
    return result


@dataclass(frozen=True, slots=True)
class Outpoint:
    txid: str
    vout: int

    def __post_init__(self) -> None:
        if not is_hash(self.txid) or self.vout < 0:
            raise ValueError("an outpoint needs a txid and a non-negative output index")


@dataclass(frozen=True, slots=True)
class TxOut:
    """An output: its value, and the script it pays to (`script_type` is Core's name, e.g.
    `witness_v0_keyhash`, `nulldata`, `nonstandard`)."""

    sats: int
    script_hex: str
    script_type: str
    address: str | None = None

    @property
    def unspendable(self) -> bool:
        """Provably unspendable: OP_RETURN, or a script too long to ever execute. Such outputs never
        enter the UTXO set or the block filters, so they are terminal in the graph (PLAN §1)."""
        return (
            self.script_type == "nulldata"
            or self.script_hex.startswith(OP_RETURN)
            or len(self.script_hex) // 2 > MAX_SCRIPT_BYTES
        )


@dataclass(frozen=True, slots=True)
class TxIn:
    """An input. `prevout` is None for a coinbase. `spent` is the output it spends, when known:
    Core includes it for confirmed transactions; for unconfirmed ones it is filled in from the parent."""

    prevout: Outpoint | None
    sequence: int
    spent: TxOut | None = None

    @property
    def coinbase(self) -> bool:
        return self.prevout is None


@dataclass(frozen=True, slots=True)
class Tx:
    """A decoded transaction. `blockhash` is None while it is unconfirmed. Cached transactions are
    keyed by (txid, blockhash): BIP30 allows two coinbases with the same txid (PLAN §1)."""

    txid: str
    blockhash: str | None
    confirmations: int
    block_time: int | None
    inputs: tuple[TxIn, ...]
    outputs: tuple[TxOut, ...]

    @property
    def coinbase(self) -> bool:
        return len(self.inputs) == 1 and self.inputs[0].coinbase

    @property
    def confirmed(self) -> bool:
        return self.blockhash is not None and self.confirmations > 0

    @property
    def prevouts_known(self) -> bool:
        return all(i.coinbase or i.spent is not None for i in self.inputs)

    @property
    def fee_sats(self) -> int | None:
        """Inputs minus outputs, once every spent output is known; 0 for a coinbase."""
        if self.coinbase:
            return 0
        if not self.prevouts_known:
            return None
        return sum(i.spent.sats for i in self.inputs if i.spent is not None) - sum(
            o.sats for o in self.outputs
        )

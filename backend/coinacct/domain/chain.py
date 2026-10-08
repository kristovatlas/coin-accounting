"""Chain data as the app holds it: transactions, outputs and outpoints (PLAN §1; THREAT_MODEL T-205, T-502).

Pure types. Amounts are integer satoshis, converted exactly from the node's BTC `Decimal`s; a value
that isn't a whole number of satoshis, or is out of range, is refused rather than rounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)
from typing import Final, TypeIs

SATS_PER_BTC: Final = 100_000_000
MAX_SATS: Final = 21_000_000 * SATS_PER_BTC
# Core's MAX_SCRIPT_SIZE: a longer output script can never be spent, so it never enters the UTXO set.
MAX_SCRIPT_BYTES: Final = 10_000
OP_RETURN: Final = "6a"

# A fresh context, not a copy of the caller's: its precision, exponent limits and traps are all ours.
_EXACT: Final = Context(
    prec=60,
    Emin=MIN_EMIN,
    Emax=MAX_EMAX,
    clamp=0,
    traps=[Inexact, Underflow, Overflow, InvalidOperation, DivisionByZero],
)

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
    # Range first: an exact comparison, so a huge exponent never reaches the arithmetic.
    if not 0 <= value <= MAX_SATS // SATS_PER_BTC:
        raise ValueError("an amount is out of range")
    # Then multiply in a fixed context, whatever the caller's is, where any rounding is an error.
    with localcontext(_EXACT):
        try:
            sats = value * SATS_PER_BTC
        except DecimalException:
            raise ValueError("an amount isn't a whole number of satoshis") from None
    if sats != sats.to_integral_value():
        raise ValueError("an amount isn't a whole number of satoshis")
    return int(sats)


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
        """Provably unspendable: OP_RETURN, or a script too long to ever execute. Neither enters the
        UTXO set, so they are terminal in the graph (PLAN §1). Only OP_RETURN (and empty) scripts are
        left out of the BIP158 filters; an over-long script is still in them."""
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

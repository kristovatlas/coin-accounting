"""USD amounts for the tax engine: exact cents, and the arithmetic the engine may do (PLAN §7; T-502).

Pure. USD is a `Decimal` with at most two fractional digits, never a float (ENGINEERING §5.2), and
less than `MAX_USD`. An amount with more digits is refused rather than rounded: rounding is explicit.

All money arithmetic runs in `CONTEXT`, a fixed context, never the caller's: a lowered precision
elsewhere in the process can't change a figure (T-502). Amounts are bounded so that every sum,
difference and `amount * part` is exact in it; only `share` rounds, once.

`share` is the only way `tax/` divides money (ENGINEERING §5.2 bans `/` there). It rounds to the
cent, half to even. The engine splits an amount by always taking a share of what remains and giving
the last part the remainder, so the parts of every split add up to the whole exactly.
"""

from __future__ import annotations

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
)
from typing import Final

CENT: Final = Decimal("0.01")
MAX_USD: Final = Decimal(10) ** 15  # a quadrillion dollars: far above any real figure
# 60 digits hold any sum of bounded amounts, and any amount times a satoshi count, exactly; Inexact
# and Rounded are trapped so that no operation but `share`'s final quantize can ever round.
CONTEXT: Final = Context(
    prec=60,
    rounding=ROUND_HALF_EVEN,
    traps=[FloatOperation, InvalidOperation, DivisionByZero, Overflow, Inexact, Rounded],
)
_DIVIDE: Final = Context(
    prec=60, rounding=ROUND_HALF_EVEN, traps=[FloatOperation, InvalidOperation, DivisionByZero, Overflow]
)


def usd(value: str | Decimal) -> Decimal:
    """A USD amount from text or a `Decimal`: finite, not negative, below `MAX_USD`, at most two
    fractional digits. Anything else raises `ValueError`; nothing is rounded."""
    if isinstance(value, str):
        try:
            value = Decimal(value.strip())  # exact: no context applies to the constructor
        except DecimalException:
            raise ValueError("a USD amount must be a number") from None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("a USD amount must be a finite Decimal")
    if value < 0:
        raise ValueError("a USD amount can't be negative")
    if value >= MAX_USD:
        raise ValueError("a USD amount must be below a quadrillion dollars")
    cents = value.quantize(CENT, context=_DIVIDE)  # bounded above, so this can't overflow
    if cents != value:  # an exact comparison: "1.230" is 1.23, "1.234" is refused
        raise ValueError("a USD amount has at most two fractional digits")
    return cents.copy_abs()  # -0.00 is 0.00


def add(a: Decimal, b: Decimal) -> Decimal:
    """`a + b`, exactly."""
    return CONTEXT.add(a, b)


def subtract(a: Decimal, b: Decimal) -> Decimal:
    """`a - b`, exactly (a gain or a loss can be negative)."""
    return CONTEXT.subtract(a, b)


def share(amount: Decimal, part: int, whole: int) -> Decimal:
    """`amount * part / whole`, rounded to the cent (half to even). `0 <= part <= whole`, `whole > 0`;
    `share(amount, whole, whole) == amount` exactly."""
    if (
        isinstance(part, bool)
        or isinstance(whole, bool)
        or not isinstance(part, int)
        or not isinstance(whole, int)
    ):
        raise TypeError("part and whole are integer satoshis")
    if whole <= 0 or not 0 <= part <= whole:
        raise ValueError("a share needs 0 <= part <= whole and whole > 0")
    cents = usd(amount)
    if part == whole:
        return cents
    exact = _DIVIDE.divide(CONTEXT.multiply(cents, Decimal(part)), Decimal(whole))
    return exact.quantize(CENT, rounding=ROUND_HALF_EVEN, context=_DIVIDE)

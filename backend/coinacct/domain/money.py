"""USD amounts for the tax engine: exact cents, and the one division the engine may do (PLAN §7; T-502).

Pure. USD is a `Decimal` with at most two fractional digits, never a float (ENGINEERING §5.2). An
amount with more digits is refused rather than rounded: rounding is the caller's explicit choice.

`share` is the only way `tax/` divides money (ENGINEERING §5.2 bans `/` there). It rounds once, to
the cent, half to even. The engine splits an amount by always taking a share of what remains and
giving the last part the remainder, so the parts of every split add up to the whole exactly.
"""

from __future__ import annotations

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    FloatOperation,
    InvalidOperation,
    Overflow,
)
from typing import Final

CENT: Final = Decimal("0.01")
# Enough digits for any USD amount the app can meet, and for `amount * part` before the division.
_CONTEXT: Final = Context(
    prec=60, rounding=ROUND_HALF_EVEN, traps=[FloatOperation, InvalidOperation, DivisionByZero, Overflow]
)


def usd(value: str | Decimal) -> Decimal:
    """A USD amount from text or a `Decimal`: finite, not negative, at most two fractional digits."""
    if isinstance(value, str):
        try:
            value = _CONTEXT.create_decimal(value.strip())
        except InvalidOperation:
            raise ValueError("a USD amount must be a number") from None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("a USD amount must be a finite Decimal")
    if value < 0:
        raise ValueError("a USD amount can't be negative")
    if value != value.quantize(CENT, context=_CONTEXT):
        raise ValueError("a USD amount has at most two fractional digits")
    return value.quantize(CENT, context=_CONTEXT)


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
    if part == whole:
        return usd(amount)
    exact = _CONTEXT.divide(_CONTEXT.multiply(usd(amount), Decimal(part)), Decimal(whole))
    return exact.quantize(CENT, rounding=ROUND_HALF_EVEN, context=_CONTEXT)

"""Fiat prices: daily values, how each was made, and sanity checks (PLAN §6, ADR 0007; THREAT_MODEL T-303,
T-304).

Pure: values in, values out. Fetching (F3) and storage live elsewhere; nothing here uses the network,
the filesystem or the clock. Prices are exact `Decimal` cents, never floats.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, localcontext
from itertools import pairwise
from typing import Final, Literal

# ADR 0007: the trade VWAP; (H+L+C)/3 where there are no trades; or, for display, USD at the ECB rate
Method = Literal["vwap", "typical", "fx"]
METHODS: Final = ("vwap", "typical", "fx")
CURRENCIES: Final = ("USD", "EUR", "GBP")  # USD for tax figures; the others for display (ADR 0007)
CENT: Final = Decimal("0.01")
# Our own context for the little arithmetic here, whatever the caller's: exact, or an error.
_EXACT: Final = Context(prec=60, traps=[Inexact, InvalidOperation, Overflow])
# The largest price: just below 10^15, the most the parsers' 15 integer digits can hold. A value that
# rounds up past it is refused. Both are literals, so no import-time arithmetic meets the caller's context.
MAX_PRICE: Final = Decimal("999999999999999.99")
MAX_CENTS: Final = 99_999_999_999_999_999
# A day-over-day rise of more than 50 %, or a fall of more than a third, is flagged for review, never
# refused: such moves are rare enough that bad data is the likelier cause, and the user can override.
OUTLIER_FACTOR: Final = Decimal("1.5")


class PriceError(ValueError):
    """Downloaded or uploaded price data that can't be used. Raised before any value is returned, so a
    bad file never yields a partial series (T-304)."""


@dataclass(frozen=True)
class DailyPrice:
    """The price of one BTC in `currency` on UTC day `day`, in whole cents, and how it was made. Only a
    positive, whole number of cents up to MAX_PRICE can be built, so a zero or rounded-away price never
    reaches a tax figure, whatever path made it (T-303). A USD price is always a market price: "fx"
    is only for display currencies (ADR 0007: tax figures are USD)."""

    day: date
    currency: str
    price: Decimal
    method: Method
    source: str

    def __post_init__(self) -> None:
        if type(self.day) is not date:  # a datetime is a date too, but not a UTC day
            raise PriceError("a price's day must be a date")
        if self.currency not in CURRENCIES:
            raise PriceError(f"{self.day}: unsupported currency {self.currency!r}")
        if self.method not in METHODS or (self.method == "fx" and self.currency == "USD"):
            raise PriceError(f"{self.day}: unsupported method {self.method!r} for {self.currency}")
        if not isinstance(self.source, str) or not self.source:
            raise PriceError(f"{self.day}: a price needs its source")
        if (
            type(self.price) is not Decimal
            or not self.price.is_finite()
            or not CENT <= self.price <= MAX_PRICE
            or self.price.as_tuple().exponent != -2  # exactly two decimals, as every parser makes them
        ):
            raise PriceError(f"{self.day}: a price must be a positive whole number of cents")


@dataclass(frozen=True)
class Gap:
    """Days with no price between two priced days: `first` to `last`, inclusive."""

    first: date
    last: date


@dataclass(frozen=True)
class Outlier:
    """A day whose price moved by more than OUTLIER_FACTOR from the priced day before it, across any
    gap (T-303)."""

    day: date
    previous: Decimal
    price: Decimal


def content_hash(data: bytes) -> str:
    """The SHA-256 of a downloaded or uploaded file, stored with each import (T-303)."""
    return hashlib.sha256(data).hexdigest()


def combine(vwap: Iterable[DailyPrice], typical: Iterable[DailyPrice]) -> list[DailyPrice]:
    """One series by day: the trade VWAP where there is one, otherwise the typical price (ADR 0007).
    Both series must be in the same currency, and neither may price a day twice."""
    best, fill = _by_day(vwap, "vwap"), _by_day(typical, "typical")
    if len({p.currency for p in (*best.values(), *fill.values())}) > 1:
        raise PriceError("the series mix currencies")
    for p in fill.values():
        best.setdefault(p.day, p)
    return [best[d] for d in sorted(best)]


def check(series: Sequence[DailyPrice]) -> tuple[list[Gap], list[Outlier]]:
    """The gaps and day-over-day outliers in a series sorted by day (T-303). Both are for the user to
    review or override; neither is an error. The first day has no day before it to compare with."""
    gaps: list[Gap] = []
    outliers: list[Outlier] = []
    for before, p in pairwise(series):
        if p.day <= before.day:
            raise PriceError(f"{p.day}: the series isn't sorted by day, or prices a day twice")
        if p.day - before.day > timedelta(days=1):
            gaps.append(Gap(before.day + timedelta(days=1), p.day - timedelta(days=1)))
        with localcontext(_EXACT):
            moved = max(p.price, before.price) > OUTLIER_FACTOR * min(p.price, before.price)
        if moved:
            outliers.append(Outlier(p.day, before.price, p.price))
    return gaps, outliers


def _by_day(series: Iterable[DailyPrice], method: Method) -> dict[date, DailyPrice]:
    out: dict[date, DailyPrice] = {}
    for p in series:
        if p.method != method:
            raise PriceError(f"{p.day}: expected a {method} price, got {p.method}")
        if p.day in out:
            raise PriceError(f"{p.day}: priced twice")
        out[p.day] = p
    return out

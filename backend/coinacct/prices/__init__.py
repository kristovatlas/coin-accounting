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
from decimal import Decimal
from itertools import pairwise
from typing import Final, Literal

Method = Literal["vwap", "typical"]  # ADR 0007: the trade VWAP, or (H+L+C)/3 where there are no trades
METHODS: Final = ("vwap", "typical")
CENT: Final = Decimal("0.01")
# A day-over-day move by more than this factor (either way) is flagged for review, not refused: BTC's
# largest daily closes moved by well under half, so a jump of 50 % or more is far more likely bad data.
OUTLIER_FACTOR: Final = Decimal("1.5")


class PriceError(ValueError):
    """Downloaded or uploaded price data that can't be used. Raised before any value is returned, so a
    bad file never yields a partial series (T-304)."""


@dataclass(frozen=True)
class DailyPrice:
    """The price of one BTC in `currency` on UTC day `day`, in whole cents, and how it was made."""

    day: date
    currency: str
    price: Decimal
    method: Method
    source: str


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
    best = _by_day(vwap, "vwap")
    for p in _by_day(typical, "typical").values():
        best.setdefault(p.day, p)
    out = [best[d] for d in sorted(best)]
    if len({p.currency for p in out}) > 1:
        raise PriceError("the series mix currencies")
    return out


def check(series: Sequence[DailyPrice]) -> tuple[list[Gap], list[Outlier]]:
    """The gaps and day-over-day outliers in a series sorted by day (T-303). Both are for the user to
    review or override; neither is an error."""
    gaps: list[Gap] = []
    outliers: list[Outlier] = []
    for before, p in pairwise(series):
        if p.day <= before.day:
            raise PriceError(f"{p.day}: the series isn't sorted by day, or prices a day twice")
        if p.day - before.day > timedelta(days=1):
            gaps.append(Gap(before.day + timedelta(days=1), p.day - timedelta(days=1)))
        if max(p.price, before.price) > OUTLIER_FACTOR * min(p.price, before.price):
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

"""Display prices in EUR and GBP from the USD series and the ECB's reference rates (PLAN §6, ADR 0007;
THREAT_MODEL T-301, T-303, T-304).

The ECB's historical file (`eurofxref-hist.csv`) has one row per TARGET business day, newest first:
`Date,USD,JPY,…,` with each rate in units per euro and `N/A` where a currency had no rate. All of it is
parsed and every supported currency converted, so nothing reveals which one the user displays (T-301).

Display only: tax figures are always USD (ADR 0007). A weekend or holiday uses the latest earlier rate
within MAX_RATE_AGE; a USD day with no rate that recent gets no display price (a gap). Pure and exact:
rates are integers scaled by 10^6, rounded once, half to even, to cents. A malformed file raises
`PriceError` naming its line, before any value is returned (T-304).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, NoReturn

from coinacct.prices import DailyPrice, PriceError

ECB_SOURCE: Final = "ecb:eurofxref-hist"
DISPLAY: Final = ("EUR", "GBP")  # every display currency, always converted together (T-301)
MAX_RATE_AGE: Final = timedelta(days=7)  # the ECB skips weekends and TARGET holidays, never a week
_SCALE: Final = 10**6
_COLUMNS: Final = ("USD", *(c for c in DISPLAY if c != "EUR"))  # EUR is the base: no column
_RATE: Final = re.compile(r"([0-9]{1,6})\.([0-9]{1,6})")
_DAY: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_CODE: Final = re.compile(r"[A-Z]{3}")

Rates = Mapping[date, Mapping[str, int]]  # day -> currency -> units per euro, scaled by 10^6


def ecb_rates(lines: Iterable[str]) -> dict[date, dict[str, int]]:
    """The USD and display-currency rates per day from the ECB's historical CSV. Days are strictly
    descending in the file; a rate that is `N/A` or empty is simply absent for that day."""
    out: dict[date, dict[str, int]] = {}
    header: list[str] | None = None
    last: date | None = None
    for n, raw in enumerate(lines, 1):
        fields = raw.removesuffix("\n").removesuffix("\r").split(",")
        if fields and fields[-1] == "":
            fields.pop()  # the ECB ends every line with a comma
        if header is None:
            if not fields or fields[0] != "Date" or len(fields) < 2:
                _fail(f"line {n}: expected the header Date,<currencies>")
            header = fields[1:]
            if not all(_CODE.fullmatch(c) for c in header) or len(set(header)) != len(header):
                _fail(f"line {n}: expected distinct three-letter currency codes")
            if not set(_COLUMNS) <= set(header):
                _fail(f"line {n}: the file lacks the USD or a display currency's rates")
            continue
        if len(fields) != len(header) + 1:
            _fail(f"line {n}: expected {len(header) + 1} fields")
        day = _day(fields[0], f"line {n}")
        if last is not None and day >= last:
            _fail(f"line {n}: out of date order (the file is newest first), or a day twice")
        last = day
        row = {}
        for code, text in zip(header, fields[1:], strict=True):
            if code in _COLUMNS and text not in ("N/A", ""):
                row[code] = _rate(text, f"line {n}")
        out[day] = row
    if header is None:
        _fail("the file is empty")
    return out


def to_display(usd: Sequence[DailyPrice], rates: Rates) -> list[DailyPrice]:
    """Every USD price in every display currency, by the latest ECB rate on or before its day and at
    most MAX_RATE_AGE older. EUR is USD divided by the USD rate; another currency is that times its
    own rate. The result keeps the USD price's day and gets method "fx"."""
    days = sorted(rates)
    out: list[DailyPrice] = []
    for currency in DISPLAY:
        for p in usd:
            if p.currency != "USD":
                _fail(f"{p.day}: expected a USD price, got {p.currency}")
            rate = _latest(rates, days, p.day, currency)
            if rate is None:
                continue
            per_usd, per_target = rate
            cents = int(p.price.scaleb(2).to_integral_exact())  # exact: a price is whole cents
            out.append(
                DailyPrice(
                    p.day,
                    currency,
                    _cents(cents * per_target, per_usd),
                    "fx",
                    f"{ECB_SOURCE}*{p.source}",
                )
            )
    return out


def _latest(rates: Rates, days: list[date], on: date, currency: str) -> tuple[int, int] | None:
    """(USD per euro, `currency` per euro) from the latest day on or before `on` with both rates,
    within MAX_RATE_AGE; None if there is none."""
    i = bisect.bisect_right(days, on)
    while i:
        i -= 1
        day = days[i]
        if on - day > MAX_RATE_AGE:
            return None
        row = rates[day]
        if "USD" in row and (currency == "EUR" or currency in row):
            return row["USD"], _SCALE if currency == "EUR" else row[currency]
    return None


def _cents(numerator: int, denominator: int) -> Decimal:
    q, r = divmod(numerator, denominator)
    if 2 * r > denominator or (2 * r == denominator and q % 2):
        q += 1
    return Decimal(f"{q // 100}.{q % 100:02d}")


def _rate(text: str, where: str) -> int:
    m = _RATE.fullmatch(text)
    if m is None or not int(m[1] + m[2]):
        _fail(f"{where}: {text[:20]!r} is not a positive rate with at most 6 decimals")
    return int(m[1]) * _SCALE + int(m[2].ljust(6, "0"))


def _day(text: str, where: str) -> date:
    m = _DAY.fullmatch(text)
    try:
        if m is None:
            raise ValueError
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        _fail(f"{where}: {text[:20]!r} is not a date (YYYY-MM-DD)")


def _fail(message: str) -> NoReturn:
    raise PriceError(message)

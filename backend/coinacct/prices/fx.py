"""Display prices in EUR and GBP from the USD series and the ECB's reference rates (PLAN §6, ADR 0039;
THREAT_MODEL T-301, T-303, T-304).

The ECB's historical file (`eurofxref-hist.csv`) has one row per TARGET business day, newest first:
`Date,USD,JPY,…,` with each rate in units per euro and `N/A` where a currency had no rate. All of it is
parsed and every supported currency converted, so nothing reveals which one the user displays (T-301).

Display only: tax figures are always USD (ADR 0039). A weekend or holiday uses the latest earlier rate
within MAX_RATE_AGE; a USD day with no rate that recent, or whose conversion rounds below a cent, gets
no display price (a gap). Blank lines at the end of the file and a byte-order mark are ignored. Pure and
exact:
rates are integers scaled by 10^6, rounded once, half to even, to cents. A malformed file raises
`PriceError` naming its line, before any value is returned (T-304).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, NoReturn

from coinacct.prices import MAX_CENTS, DailyPrice, PriceError

ECB_SOURCE: Final = "ecb:eurofxref-hist"
DISPLAY: Final = ("EUR", "GBP")  # every display currency, always converted together (T-301)
MAX_RATE_AGE: Final = timedelta(days=7)  # the ECB skips weekends and TARGET holidays, never a week
_SCALE: Final = 10**6
MAX_RATE: Final = 999_999_999_999  # 999999.999999 per euro, the most the CSV's rate pattern can hold
_MAX_RATE_TEXT: Final = f"{MAX_RATE // _SCALE}.{MAX_RATE % _SCALE:06d}"  # as the CSV writes a rate
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
    for n, fields in _rows(lines):
        if header is None:
            header = _header(fields, n)
            continue
        if len(fields) != len(header) + 1:
            _fail(f"line {n}: expected {len(header) + 1} fields")
        day = _day(fields[0], f"line {n}")
        if last is not None and day >= last:
            _fail(f"line {n}: out of date order (the file is newest first), or a day twice")
        last = day
        out[day] = {
            code: _rate(text, f"line {n}")
            for code, text in zip(header, fields[1:], strict=True)
            if code in _COLUMNS and text not in ("N/A", "")
        }
    if header is None:
        _fail("the file is empty")
    return out


def _rows(lines: Iterable[str]) -> Iterator[tuple[int, list[str]]]:
    """Each non-blank line's number and comma-separated fields, without the ECB's trailing comma. A
    byte-order mark is dropped; blank lines may only end the file."""
    blank = 0  # the line number of a blank line: only more blank lines may follow it
    for n, raw in enumerate(lines, 1):
        text = raw.removesuffix("\n").removesuffix("\r")
        if n == 1:
            text = text.removeprefix("\ufeff")
        if not text:
            blank = blank or n
            continue
        if blank:
            _fail(f"line {blank}: a blank line before the end of the file")
        fields = text.split(",")
        yield n, fields[:-1] if fields[-1] == "" else fields


def _header(fields: list[str], n: int) -> list[str]:
    if fields[0] != "Date" or len(fields) < 2:
        _fail(f"line {n}: expected the header Date,<currencies>")
    codes = fields[1:]
    if not all(_CODE.fullmatch(c) for c in codes) or len(set(codes)) != len(codes):
        _fail(f"line {n}: expected distinct three-letter currency codes")
    if not set(_COLUMNS) <= set(codes):
        _fail(f"line {n}: the file lacks the USD or a display currency's rates")
    return codes


def to_display(usd: Sequence[DailyPrice], rates: Rates) -> list[DailyPrice]:
    """Every USD price in every display currency, by the latest ECB rate on or before its day and at
    most MAX_RATE_AGE older. EUR is USD divided by the USD rate; another currency is that times its
    own rate. The result keeps the USD price's day and gets method "fx", with the USD price's source
    after the `*` in its own, so a price converted from an import stays identifiable (T-303).
    Every rate in `rates` is checked, used or not, and so is the mapping's shape, since the rates may
    come from anywhere (a cache, an upload)."""
    if not isinstance(rates, Mapping):
        _fail("the rates must map each day (a date) to its currencies' rates")
    for day, row in rates.items():
        if type(day) is not date or not isinstance(row, Mapping):  # a datetime is a date too
            _fail("the rates must map each day (a date) to its currencies' rates")
        if not all(type(c) is str and type(v) is int and 0 < v <= MAX_RATE for c, v in row.items()):
            _fail(
                f"{day}: a rate must be a positive integer scaled by 10^6, at most {_MAX_RATE_TEXT} per euro"
            )
    for before, p in zip((None, *usd), usd, strict=False):
        if p.currency != "USD":  # DailyPrice already refuses a USD price made by "fx"
            _fail(f"{p.day}: expected a USD price, got {p.currency}")
        if before is not None and p.day <= before.day:
            _fail(f"{p.day}: the USD series isn't sorted by day, or prices a day twice")
    days = sorted(rates)
    out: list[DailyPrice] = []
    for currency in DISPLAY:
        for p in usd:
            rate = _latest(rates, days, p.day, currency)
            if rate is None:
                continue
            rate_day, per_usd, per_target = rate
            num, den = p.price.as_integer_ratio()  # exact, whatever the caller's decimal context
            converted = _round(num * 100 * per_target, den * per_usd)
            if converted == 0:  # rounds to nothing: no display price that day, like a missing rate
                continue
            if converted > MAX_CENTS:  # only an absurd rate gets here: refuse the input, loudly
                _fail(f"{p.day}: the {currency} price at the rate of {rate_day} is out of range")
            out.append(DailyPrice(p.day, currency, _text(converted), "fx", f"{ECB_SOURCE}*{p.source}"))
    return out


def _latest(rates: Rates, days: list[date], on: date, currency: str) -> tuple[date, int, int] | None:
    """(the rate's day, USD per euro, `currency` per euro) from the latest day on or before `on` with
    both rates, within MAX_RATE_AGE; None if there is none."""
    i = bisect.bisect_right(days, on)
    while i:
        i -= 1
        day = days[i]
        if on - day > MAX_RATE_AGE:
            return None
        row = rates[day]
        if "USD" in row and (currency == "EUR" or currency in row):
            return day, row["USD"], _SCALE if currency == "EUR" else row[currency]
    return None


def _round(numerator: int, denominator: int) -> int:
    """numerator / denominator, rounded half to even to an integer (whole cents here)."""
    q, r = divmod(numerator, denominator)
    if 2 * r > denominator or (2 * r == denominator and q % 2):
        q += 1
    return q


def _text(cents: int) -> Decimal:
    return Decimal(f"{cents // 100}.{cents % 100:02d}")  # built from text: exact in any context


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

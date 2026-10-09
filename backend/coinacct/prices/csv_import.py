"""A price CSV the user uploads: the fallback when a source disappears (PLAN §6, ADR 0007; THREAT_MODEL
T-304, T-701, trust boundary TB6).

The format is strict and documented, so nothing is guessed:

    date,currency,price
    2024-01-02,USD,44950.12

- **One header line, exactly as above,** then one row per day and currency. Every line ends in `\\n`,
  or `\\r\\n`, the last one too, so a file cut off mid-row is refused; blank lines may only end the
  file.
- **`date`** is a UTC day (`YYYY-MM-DD`), from FIRST_DAY (Bitcoin's first block) up to the day
  before the caller's `today`: a typo in the year, or a day not yet over, is refused.
- **`currency`** is one of `CURRENCIES`, and **`price`** an amount above zero with at most two
  decimals and 15 integer digits: the user's own value, never rounded.
- **No day twice for a currency.** Rows may come in any order; each currency's series is returned
  sorted by day.

A file over MAX_BYTES or MAX_ROWS, a byte outside printable ASCII, or any bad row refuses the whole
file, naming the line, before any value is returned (T-304, T-701). Imported prices carry the method
"import", so reports can flag them as user-supplied (T-303); a display price converted from one
keeps `SOURCE` in its own source (`fx.to_display`). The module can't read the clock (architecture
§2), so the caller passes `today`.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Final, NoReturn

from coinacct.prices import CURRENCIES, DailyPrice, PriceError

HEADER: Final = "date,currency,price"
SOURCE: Final = "csv-import"
MAX_BYTES: Final = 8 << 20  # 15 years of three currencies is about 1 MB
MAX_ROWS: Final = 100_000
FIRST_DAY: Final = date(2009, 1, 3)  # the genesis block: no price before it
_DAY: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_NOT_ALLOWED: Final = re.compile(rb"[^\x20-\x7e\n\r]")  # one C-speed scan, not a loop per byte
_PRICE: Final = re.compile(r"[0-9]{1,15}(\.[0-9]{1,2})?")


def parse(data: bytes, today: date) -> dict[str, list[DailyPrice]]:
    """Each currency's imported series, sorted by day, from an uploaded CSV file. `today` is the
    caller's UTC date: only days before it are complete, so only those can be priced."""
    out: dict[str, dict[date, DailyPrice]] = {}
    for n, line in enumerate(_lines(data)[1:], 2):
        p = _row(line, n, today)
        series = out.setdefault(p.currency, {})
        if p.day in series:
            _fail(f"line {n}: {p.currency} on {p.day} is priced twice")
        series[p.day] = p
    return {c: [s[d] for d in sorted(s)] for c, s in sorted(out.items())}


def _lines(data: bytes) -> list[str]:
    """The file's lines, header first, once the file as a whole is known to be well formed."""
    if len(data) > MAX_BYTES:
        _fail(f"the file is larger than {MAX_BYTES >> 20} MB")
    if _NOT_ALLOWED.search(data) is not None:
        _fail("the file holds a byte that isn't printable ASCII")
    text = data.decode("ascii")
    if "\r" in text.replace("\r\n", ""):
        _fail("the file holds a carriage return that doesn't end a line")
    text = text.replace("\r\n", "\n")
    if text and not text.endswith("\n"):  # a last row cut short could still read as a price
        _fail("the last line has no line break: the file may be cut off")
    text = text.rstrip("\n")
    if text.count("\n") > MAX_ROWS:  # counted before splitting, so a long file is never held as lines
        _fail(f"the file has more than {MAX_ROWS} rows")
    lines = text.split("\n")
    if lines[0] != HEADER:
        _fail(f"line 1: expected the header {HEADER}")
    return lines


def _row(line: str, n: int, today: date) -> DailyPrice:
    fields = line.split(",", 3)  # bounded: a row of a million commas is never split up
    if len(fields) != 3:
        _fail(f"line {n}: expected date,currency,price")
    text_day, currency, text_price = fields
    day = _day(text_day, n)
    if not FIRST_DAY <= day < today:
        _fail(f"line {n}: {day} is not a past day from {FIRST_DAY} on")
    if currency not in CURRENCIES:
        _fail(f"line {n}: the currency must be one of {', '.join(CURRENCIES)}")
    if _PRICE.fullmatch(text_price) is None:
        _fail(f"line {n}: the price must be a plain amount with at most two decimals")
    whole, _, cents = text_price.partition(".")
    if int(whole) == 0 and int(cents or "0") == 0:
        _fail(f"line {n}: the price must be more than zero")
    # Two decimals, from text: exact in any context. The checks above leave nothing DailyPrice
    # refuses; if one were missed, its PriceError still refuses the file.
    return DailyPrice(day, currency, Decimal(f"{whole}.{cents.ljust(2, '0')}"), "import", SOURCE)


def _day(text: str, n: int) -> date:
    m = _DAY.fullmatch(text)
    try:
        if m is None:
            raise ValueError
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        _fail(f"line {n}: {text[:20]!r} is not a date (YYYY-MM-DD)")


def _fail(message: str) -> NoReturn:
    raise PriceError(message)

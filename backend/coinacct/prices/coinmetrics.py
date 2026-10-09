"""Coin Metrics' daily reference rate into daily USD prices (PLAN §6, ADR 0039; THREAT_MODEL T-303,
T-304).

**The data** is the community API's `PriceUSD` for BTC at daily frequency: one row per UTC day, its
`time` that day's 00:00 UTC, its value "the price as of the end of the day in UTC time". So the row
dated D prices day D at its close, the same moment that ends Bitstamp's daily candle for D (ADR 0039).

**Stated tax position** (ADR 0009, ADR 0039): each UTC day is valued at this rate, a robust price at
the day's close, not a whole-day average. Coin Metrics published its reference-rate method in 2019 and
doesn't document which exchanges its earlier days come from; its trade data begins with Mt. Gox in
July 2010. Rounding to whole cents is coarse on the earliest days (BTC traded around $0.05 to $0.30
from July 2010 into early 2011). A per-event override values any such event exactly. Attribution: the
data is Coin Metrics' under CC BY-NC 4.0, credited wherever it is shown or exported. The owner checked
and accepted the community API's terms for this use (automated, possibly through Tor) on 2026-10-09,
as ADR 0039 requires before this downloader lands.

A pure parser: text in, values out. Each value is a decimal string, converted with exact integer
arithmetic and rounded once, half to even, to whole cents. A day with no value (`null`), or one that
rounds below a cent, gets no price: a gap. Anything malformed raises `PriceError` before any value is
returned (T-304).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, NoReturn

from coinacct.prices import MAX_CENTS, DailyPrice, PriceError

SOURCE: Final = "coinmetrics:PriceUSD"
FIRST_DAY: Final = date(2010, 7, 18)  # the first day Coin Metrics prices
_NUMBER: Final = re.compile(r"([0-9]{1,15})(?:\.([0-9]{1,20}))?")
_TIME: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T00:00:00(?:\.0{1,9})?Z")
# The paging token seen in 2026: a version number, a dot, and URL-safe base64 of a time. Anything else
# fails the refresh; it is put into the next page's URL, so it must be plain (ADR 0039).
_TOKEN: Final = re.compile(r"[0-9]{1,3}\.[A-Za-z0-9_-]{1,200}")


@dataclass(frozen=True)
class Page:
    """One page of rows: the prices, the last day it had a row for (priced or not), and the token for
    the next page, if the server says there is one."""

    prices: list[DailyPrice]
    last: date | None
    next_token: str | None


def reference_page(text: str, complete_before: date, after: date | None) -> Page:
    """One page of the API's JSON. Every row is BTC, dated on a day after `after` (the last page's last
    day) and before `complete_before` (the refresh's UTC date: a day not yet over has no close), in
    strictly increasing order. A row that breaks any of this fails the whole page, not just itself."""
    try:
        doc = json.loads(
            text,
            parse_float=_no_number,
            parse_int=_no_number,
            parse_constant=_no_number,
            object_pairs_hook=_unique,
        )
    except (ValueError, RecursionError) as e:
        raise PriceError(f"Coin Metrics: not valid JSON ({type(e).__name__})") from None
    if (
        not isinstance(doc, dict)
        or "data" not in doc
        or not set(doc)
        <= {
            "data",
            "next_page_token",
            "next_page_url",
        }
    ):
        _fail("Coin Metrics: expected data and, at most, the next page's token and URL")
    rows = doc["data"]
    if not isinstance(rows, list):
        _fail("Coin Metrics: expected a list of rows")
    token = _token(doc)
    out: list[DailyPrice] = []
    seen = after
    for n, row in enumerate(rows, 1):
        where = f"Coin Metrics row {n}"
        if not isinstance(row, dict) or set(row) != {"asset", "time", "PriceUSD"}:
            _fail(f"{where}: expected asset, time and PriceUSD")
        if row["asset"] != "btc":
            _fail(f"{where}: not BTC")
        day = _day(row["time"], where)
        if seen is not None and day <= seen:
            _fail(f"{where}: {day} is out of day order, or a day twice")
        if day >= complete_before:
            _fail(f"{where}: {day} isn't over yet, so it has no close")
        seen = day
        value = row["PriceUSD"]
        if value is None:
            continue
        if not isinstance(value, str):
            _fail(f"{where}: expected the price as a string")
        price = _cents(value, where)
        if price is not None:
            out.append(DailyPrice(day, "USD", price, "reference", SOURCE))
    return Page(out, seen if seen != after else None, token)


def _token(doc: dict[str, Any]) -> str | None:
    """The next page's token, or None on the last page. A next page named without a token (more to
    come, but no way to ask for it) fails, rather than end the history early."""
    token = doc.get("next_page_token")
    if token is not None and (not isinstance(token, str) or _TOKEN.fullmatch(token) is None):
        _fail("Coin Metrics: the next page's token isn't in the expected form")
    if token is None and doc.get("next_page_url") is not None:
        _fail("Coin Metrics: a next page is named without its token")
    return token


def _day(text: Any, where: str) -> date:
    m = _TIME.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        _fail(f"{where}: {str(text)[:40]!r} is not a day's 00:00 UTC")
    try:
        day = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        _fail(f"{where}: {text[:40]!r} is not a real date")
    if day < FIRST_DAY:
        _fail(f"{where}: {day} is before Coin Metrics' first day")
    return day


def _cents(text: str, where: str) -> Decimal | None:
    """A non-negative decimal of at most 15 integer digits and 20 decimals, rounded half to even to
    whole cents with integers alone. None if it rounds to zero. ASCII only: `re`'s [0-9] doesn't match
    other scripts' digits, which `int` and `Decimal` would accept."""
    m = _NUMBER.fullmatch(text)
    if m is None:
        _fail(f"{where}: {text[:30]!r} is not a plain decimal number")
    decimals = m[2] or ""
    denominator = 10 ** len(decimals)
    q, r = divmod((int(m[1]) * denominator + int(decimals or "0")) * 100, denominator)
    if 2 * r > denominator or (2 * r == denominator and q % 2):
        q += 1
    if q > MAX_CENTS:
        _fail(f"{where}: the price is out of range")
    return Decimal(f"{q // 100}.{q % 100:02d}") if q else None


def _no_number(text: str) -> NoReturn:
    raise ValueError("bare JSON numbers are not expected")  # Coin Metrics sends values as strings


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("a duplicate key")
    return out


def _fail(message: str) -> NoReturn:
    raise PriceError(message)

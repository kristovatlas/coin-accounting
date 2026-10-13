"""Bitstamp price data into daily prices (PLAN §6, ADR 0039; THREAT_MODEL T-303, T-304).

**Daily OHLC** (Bitstamp's `/api/v2/ohlc/<pair>/` with `step=86400`): JSON candles. Each day's price is
the typical price (H+L+C)/3. For USD it is the cross-check on Coin Metrics' reference rate, and the fill
for any day the rate lacks (ADR 0039).

Pure parsers: text in, values out. All arithmetic is exact integers (values scaled by 10^12), rounded
once, half to even, to whole cents. Anything malformed raises `PriceError` naming the line or candle,
before any value is returned (T-304). Days on or after `complete_before` (the UTC day of the download)
are dropped, since their data is partial.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, NoReturn

from coinacct.prices import CURRENCIES, MAX_CENTS, DailyPrice, PriceError

OHLC_SOURCE: Final = "bitstamp:ohlc"
DAY_SECONDS: Final = 86_400
_SCALE: Final = 10**12  # a candle's values have at most 12 decimals
_NUMBER: Final = re.compile(r"([0-9]{1,15})(?:\.([0-9]{1,12}))?")
_TIME: Final = re.compile(r"[0-9]{1,12}")
# 2009-01-03 (the genesis block) to 9999-12-31: a timestamp outside this is not a candle's.
_FIRST: Final = 1_230_940_800
_LAST: Final = 253_402_300_799


def typical_by_day(
    text: str, currency: str, complete_before: date, *, within: tuple[date, date | None] | None = None
) -> list[DailyPrice]:
    """Each complete UTC day's typical price (H+L+C)/3 from one page of Bitstamp's daily OHLC JSON for
    BTC in `currency`. A candle with no volume (no trades that day) gets no price. Bitstamp's page always
    covers whole days, so its last day is kept when it is before `complete_before`. With `within`
    (first, end), every candle, priced or not, must fall on a day from `first` and before `end` (if
    any): a page that answers for other days is refused, not filtered (T-304)."""
    if currency not in CURRENCIES:
        _fail(f"unsupported currency {currency!r}")
    try:
        doc = json.loads(
            text,
            parse_float=_no_float,
            parse_int=_no_float,
            parse_constant=_no_float,
            object_pairs_hook=_unique,
        )
    except (ValueError, RecursionError) as e:
        raise PriceError(f"OHLC: not valid JSON ({type(e).__name__})") from None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict) or data.get("pair") != f"BTC/{currency}":
        _fail(f"OHLC: expected data for the pair BTC/{currency}")
    candles = data.get("ohlc")
    if not isinstance(candles, list):
        _fail("OHLC: expected a list of candles")
    out: list[DailyPrice] = []
    seen: date | None = None  # the last candle's day, priced or not
    for n, c in enumerate(candles, 1):
        where = f"candle {n}"
        if not isinstance(c, dict) or set(c) != {"timestamp", "open", "high", "low", "close", "volume"}:
            _fail(f"{where}: expected timestamp, open, high, low, close and volume")
        if not all(isinstance(v, str) for v in c.values()):
            _fail(f"{where}: expected string values")
        when = _time(c["timestamp"], where)
        if when % DAY_SECONDS:
            _fail(f"{where}: not a daily candle (it doesn't start at 00:00 UTC)")
        o, h, lo, cl = (_number(c[k], where) for k in ("open", "high", "low", "close"))
        if not 0 < lo <= min(o, cl) <= max(o, cl) <= h:
            _fail(f"{where}: needs 0 < low <= open, close <= high")
        on = datetime.fromtimestamp(when, UTC).date()
        if seen is not None and on <= seen:
            _fail(f"{where}: out of day order, or a day twice")
        if within is not None and (on < within[0] or (within[1] is not None and on >= within[1])):
            _fail(f"{where}: {on} is outside the requested page")
        seen = on
        if _number(c["volume"], where):
            out.append(
                DailyPrice(
                    on,
                    currency,
                    _cents(h + lo + cl, 3 * _SCALE, where),
                    "typical",
                    f"{OHLC_SOURCE}:btc{currency.lower()}",
                )
            )
    return [p for p in out if p.day < complete_before]


def _cents(numerator: int, denominator: int, where: str) -> Decimal:
    """numerator / denominator, rounded half to even to whole cents, exactly. Built from text, which
    `Decimal` takes exactly whatever the caller's context (no context arithmetic at all)."""
    q, r = divmod(numerator * 100, denominator)
    if 2 * r > denominator or (2 * r == denominator and q % 2):
        q += 1
    if q > MAX_CENTS:  # 15 integer digits can round up past MAX_PRICE
        _fail(f"{where}: the price is out of range")
    return Decimal(f"{q // 100}.{q % 100:02d}")


def _number(text: str, where: str) -> int:
    """A non-negative decimal of at most 15 digits and 12 decimals, scaled by 10^12. ASCII only: `re`'s
    [0-9] doesn't match other scripts' digits, which `int` and `Decimal` would accept."""
    m = _NUMBER.fullmatch(text)
    if m is None:
        _fail(f"{where}: {text[:20]!r} is not a plain decimal number")
    return int(m[1]) * _SCALE + int((m[2] or "").ljust(12, "0"))


def _time(text: str, where: str) -> int:
    if _TIME.fullmatch(text) is None or not _FIRST <= int(text) <= _LAST:
        _fail(f"{where}: {text[:20]!r} is not a unix time from 2009 on")
    return int(text)


def _no_float(text: str) -> NoReturn:
    raise ValueError("bare JSON numbers are not expected")  # Bitstamp sends every value as a string


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("a duplicate key")
    return out


def _fail(message: str) -> NoReturn:
    raise PriceError(message)

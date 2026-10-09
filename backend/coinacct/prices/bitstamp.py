"""Bitstamp price data into daily prices (PLAN §6, ADR 0007; THREAT_MODEL T-303, T-304).

- **Trade dump** (bitcoincharts' `bitstampUSD.csv`): one trade per line, `unix time,price,amount`, in
  time order. Each UTC day's price is its volume-weighted average.
- **Daily OHLC** (Bitstamp's `/api/v2/ohlc/<pair>/` with `step=86400`): JSON candles. Each day's price
  is the typical price (H+L+C)/3, used where the dump has no trades (`prices.combine`).

Pure parsers: text in, values out. All arithmetic is exact integers (values scaled by 10^12), rounded
once, half to even, to whole cents. Anything malformed raises `PriceError` naming the line or candle,
before any value is returned (T-304). Days on or after `complete_before` (the UTC day of the download)
are dropped, since their data is partial.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, NoReturn

from coinacct.prices import CENT, DailyPrice, PriceError

DUMP_SOURCE: Final = "bitcoincharts:bitstampUSD"
OHLC_SOURCE: Final = "bitstamp:ohlc"
DAY_SECONDS: Final = 86_400
_SCALE: Final = 10**12  # the dump's values have at most 12 decimals
_NUMBER: Final = re.compile(r"([0-9]{1,15})(?:\.([0-9]{1,12}))?")
_TIME: Final = re.compile(r"[0-9]{1,12}")
# 2009-01-03 (the genesis block) to 9999-12-31: a timestamp outside this is not a trade's.
_FIRST: Final = 1_230_940_800
_LAST: Final = 253_402_300_799
CURRENCIES: Final = ("USD", "EUR", "GBP")  # Bitstamp's BTC pairs that are fetched, all of them (T-301)


def vwap_by_day(lines: Iterable[str], complete_before: date) -> list[DailyPrice]:
    """Each complete UTC day's volume-weighted average USD price from the trade dump's lines. A day
    whose trades have no volume gets no price (a gap, for `prices.check`)."""
    out: list[DailyPrice] = []
    day: date | None = None
    value = volume = 0  # sum of price * amount (scale 10^24), sum of amounts (scale 10^12)
    last = _FIRST
    for n, raw in enumerate(lines, 1):
        fields = raw.removesuffix("\n").removesuffix("\r").split(",")
        if len(fields) != 3:
            _fail(f"line {n}: expected time,price,amount")
        when = _time(fields[0], f"line {n}")
        if when < last:
            _fail(f"line {n}: out of time order")
        last = when
        price, amount = _number(fields[1], f"line {n}"), _number(fields[2], f"line {n}")
        if price == 0:
            _fail(f"line {n}: a price must be positive")
        on = datetime.fromtimestamp(when, UTC).date()
        if on != day:
            if day is not None and volume:
                out.append(DailyPrice(day, "USD", _cents(value, volume * _SCALE), "vwap", DUMP_SOURCE))
            day, value, volume = on, 0, 0
        value += price * amount
        volume += amount
    if day is not None and volume:
        out.append(DailyPrice(day, "USD", _cents(value, volume * _SCALE), "vwap", DUMP_SOURCE))
    return [p for p in out if p.day < complete_before]


def typical_by_day(text: str, currency: str, complete_before: date) -> list[DailyPrice]:
    """Each complete UTC day's typical price (H+L+C)/3 from one page of Bitstamp's daily OHLC JSON for
    BTC in `currency`. A candle with no volume (no trades that day) gets no price."""
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
        if out and on <= out[-1].day:
            _fail(f"{where}: out of day order, or a day twice")
        if _number(c["volume"], where):
            out.append(
                DailyPrice(
                    on,
                    currency,
                    _cents(h + lo + cl, 3 * _SCALE),
                    "typical",
                    f"{OHLC_SOURCE}:btc{currency.lower()}",
                )
            )
    return [p for p in out if p.day < complete_before]


def _cents(numerator: int, denominator: int) -> Decimal:
    """numerator / denominator, rounded half to even to whole cents, exactly."""
    q, r = divmod(numerator * 100, denominator)
    if 2 * r > denominator or (2 * r == denominator and q % 2):
        q += 1
    return Decimal(q) * CENT


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

"""Daily prices in the user DB (PLAN §2 `price`, §6; ADR 0039; THREAT_MODEL T-303, T-304, T-408).

- **Batches.** A refresh's prices (`store_refresh`) or an import's (`store_import`) are written in
  one transaction with the batch that records them, or not at all (T-304). A refresh records each
  source's SHA-256 (T-303), the reference rate's last priced day, whether it was stale, and the review
  flags; the latest refresh's flags are what a report shows.
- **The user's own prices stand.** A refresh replaces the day's fetched price, never one the user
  imported (method `import`): an import is the fallback for a source that went wrong (T-304), so a
  later refresh mustn't undo it. An import replaces whatever the day had.
- **Values.** A price is a `Decimal` above zero in whole cents, at most 15 integer digits, as
  `prices.DailyPrice` rounds it; a day is a UTC date from Bitcoin's first block on; the currency is
  USD (tax figures) or EUR or GBP (display only).
- **Reads are checked** (T-408): a stored row the module wouldn't write is refused with the remedy,
  never half-read.

The module reads no clock: the caller passes the batch's time.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from coinacct.storage.db import DbError, transaction

CURRENCIES: Final = ("USD", "EUR", "GBP")
METHODS: Final = ("reference", "typical", "fx", "import")
FIRST_DAY: Final = date(2009, 1, 3)  # Bitcoin's first block
MAX_SOURCE: Final = 200
_PRICE: Final = re.compile(r"(?:0|[1-9][0-9]{0,14})\.[0-9]{2}", re.ASCII)
_DAY: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_HASH: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)
_DAMAGED: Final = (
    "a stored price is damaged (T-408): refresh the prices, or restore the user DB from a backup"
)

type Method = Literal["reference", "typical", "fx", "import"]


class PriceStoreError(DbError):
    """A price or batch the module refuses, with a fixed message that never echoes the value."""


@dataclass(frozen=True, slots=True)
class Price:
    day: date
    currency: str
    price: Decimal
    method: Method
    source: str


@dataclass(frozen=True, slots=True)
class Batch:
    id: int
    at: datetime
    kind: Literal["refresh", "import"]
    usd_through: date | None
    usd_stale: bool
    hashes: dict[str, str]
    flags: dict[str, Any]


def store_refresh(  # noqa: PLR0913 - a refresh's prices and what it recorded
    conn: sqlite3.Connection,
    prices: Iterable[Price],
    *,
    hashes: Mapping[str, str],
    usd_through: date | None,
    usd_stale: bool,
    flags: Mapping[str, Any],
    at: datetime,
) -> int:
    """Store a refresh's prices and its batch, all or nothing; imported prices are kept. Returns the
    batch's id."""
    rows = [_checked(p) for p in prices]
    if any(p.method == "import" for p in rows):
        raise PriceStoreError("a refresh stores fetched prices, never imported ones")
    if not isinstance(hashes, Mapping) or not all(
        isinstance(k, str) and 1 <= len(k) <= MAX_SOURCE and isinstance(v, str) and _HASH.fullmatch(v)
        for k, v in hashes.items()
    ):
        raise PriceStoreError("a refresh records each source's SHA-256")
    if usd_through is not None and not isinstance(usd_through, date):
        raise PriceStoreError("the reference rate's last day is a date")
    if not isinstance(usd_stale, bool):
        raise PriceStoreError("a refresh says whether the reference rate is stale")
    with transaction(conn):
        batch = _batch(
            conn,
            kind="refresh",
            at=at,
            usd_through=usd_through,
            usd_stale=usd_stale,
            hashes=dict(hashes),
            flags=flags,
        )
        conn.executemany(
            "INSERT INTO price (day, currency, price, method, source, batch_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (day, currency) DO UPDATE SET price = excluded.price, method = excluded.method,"
            " source = excluded.source, batch_id = excluded.batch_id WHERE price.method != 'import'",
            [_row(p, batch) for p in rows],
        )
        return batch


def store_import(conn: sqlite3.Connection, prices: Iterable[Price], *, at: datetime) -> int:
    """Store the user's imported prices and their batch, all or nothing, replacing whatever each day
    had. Returns the batch's id."""
    rows = [_checked(p) for p in prices]
    if not rows or any(p.method != "import" for p in rows):
        raise PriceStoreError("an import stores the user's own prices")
    with transaction(conn):
        batch = _batch(conn, kind="import", at=at, usd_through=None, usd_stale=False, hashes={}, flags={})
        conn.executemany(
            "INSERT INTO price (day, currency, price, method, source, batch_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (day, currency) DO UPDATE SET price = excluded.price, method = excluded.method,"
            " source = excluded.source, batch_id = excluded.batch_id",
            [_row(p, batch) for p in rows],
        )
        return batch


def price_on(conn: sqlite3.Connection, day: date, currency: str) -> Price | None:
    """The stored price for `day` in `currency`, if there is one."""
    if not isinstance(day, date) or currency not in CURRENCIES:
        raise PriceStoreError("a price is asked for by day and a supported currency")
    row = conn.execute(
        "SELECT day, currency, price, method, source FROM price WHERE day = ? AND currency = ?",
        (day.isoformat(), currency),
    ).fetchone()
    if row is None:
        return None
    with _damaged():
        return _price(row)


def prices(conn: sqlite3.Connection, currency: str) -> list[Price]:
    """Every stored price in `currency`, by day."""
    if currency not in CURRENCIES:
        raise PriceStoreError("prices are asked for in a supported currency")
    rows = conn.execute(
        "SELECT day, currency, price, method, source FROM price WHERE currency = ? ORDER BY day", (currency,)
    )
    with _damaged():
        return [_price(r) for r in rows]


def last_refresh(conn: sqlite3.Connection) -> Batch | None:
    """The latest refresh's batch: its hashes, the reference rate's state and its review flags."""
    row = conn.execute(
        "SELECT id, at, kind, usd_through, usd_stale, hashes, flags FROM price_batch"
        " WHERE kind = 'refresh' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    with _damaged():
        return _stored_batch(row)


def _batch(  # noqa: PLR0913 - one batch row's columns
    conn: sqlite3.Connection,
    *,
    kind: str,
    at: datetime,
    usd_through: date | None,
    usd_stale: bool,
    hashes: dict[str, str],
    flags: Mapping[str, Any],
) -> int:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise PriceStoreError("a batch's time is a timezone-aware moment")
    if not isinstance(flags, Mapping):
        raise PriceStoreError("a refresh's review flags are an object")
    try:
        flags_json = json.dumps(dict(flags), sort_keys=True, allow_nan=False, default=str)
    except (TypeError, ValueError):
        raise PriceStoreError("a refresh's review flags are plain data") from None
    cur = conn.execute(
        "INSERT INTO price_batch (at, kind, usd_through, usd_stale, hashes, flags) VALUES (?, ?, ?, ?, ?, ?)",
        (
            at.isoformat(),
            kind,
            None if usd_through is None else usd_through.isoformat(),
            int(usd_stale),
            json.dumps(hashes, sort_keys=True),
            flags_json,
        ),
    )
    if cur.lastrowid is None:  # pragma: no cover - an INSERT into a rowid table always has one
        raise PriceStoreError("the batch wasn't recorded")
    return cur.lastrowid


def _checked(p: object) -> Price:
    if not isinstance(p, Price):
        raise PriceStoreError("prices are Price values")
    if not isinstance(p.day, date) or isinstance(p.day, datetime) or p.day < FIRST_DAY:
        raise PriceStoreError("a price's day is a date from Bitcoin's first block on")
    if p.currency not in CURRENCIES:
        raise PriceStoreError("a price's currency is USD, EUR or GBP")
    if p.method not in METHODS:
        raise PriceStoreError("a price's method is one the app knows")
    if not isinstance(p.source, str) or not 1 <= len(p.source) <= MAX_SOURCE:
        raise PriceStoreError("a price names its source")
    if not isinstance(p.price, Decimal) or not p.price.is_finite() or p.price <= 0:
        raise PriceStoreError("a price is an amount above zero")
    if not _PRICE.fullmatch(format(p.price, "f")):
        raise PriceStoreError("a price is in whole cents, with at most 15 integer digits")
    return p


def _row(p: Price, batch: int) -> tuple[str, str, str, str, str, int]:
    return (p.day.isoformat(), p.currency, format(p.price, "f"), p.method, p.source, batch)


@contextmanager
def _damaged() -> Iterator[None]:
    try:
        yield
    except (ValueError, TypeError, ArithmeticError, InvalidOperation, KeyError, PriceStoreError):
        raise PriceStoreError(_DAMAGED) from None


def _price(r: Sequence[Any]) -> Price:
    day, currency, text, method, source = r
    if not isinstance(day, str) or not _DAY.fullmatch(day) or not isinstance(text, str):
        raise PriceStoreError(_DAMAGED)
    p = Price(date.fromisoformat(day), currency, Decimal(text), method, source)
    _checked(p)
    if format(p.price, "f") != text:
        raise PriceStoreError(_DAMAGED)
    return p


def _stored_batch(r: Sequence[Any]) -> Batch:
    batch_id, at, kind, usd_through, usd_stale, hashes, flags = r
    moment = datetime.fromisoformat(at)
    if moment.utcoffset() is None or kind not in ("refresh", "import") or usd_stale not in (0, 1):
        raise PriceStoreError(_DAMAGED)
    through = None if usd_through is None else date.fromisoformat(usd_through)
    hash_map, flag_map = json.loads(hashes), json.loads(flags)
    if not isinstance(hash_map, dict) or not isinstance(flag_map, dict):
        raise PriceStoreError(_DAMAGED)
    if not all(isinstance(k, str) and isinstance(v, str) and _HASH.fullmatch(v) for k, v in hash_map.items()):
        raise PriceStoreError(_DAMAGED)
    return Batch(batch_id, moment, kind, through, bool(usd_stale), hash_map, flag_map)


__all__ = [
    "CURRENCIES",
    "FIRST_DAY",
    "METHODS",
    "Batch",
    "Price",
    "PriceStoreError",
    "last_refresh",
    "price_on",
    "prices",
    "store_import",
    "store_refresh",
]

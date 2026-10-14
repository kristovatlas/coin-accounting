"""Daily prices in the user DB (PLAN §2 `price`, §6; ADR 0039; THREAT_MODEL T-303, T-304, T-408).

- **Batches.** A refresh's prices (`store_refresh`) or an import's (`store_import`) are written in
  one transaction with the batch that records them, or not at all (T-304). A refresh records each
  source's SHA-256 (T-303), the reference rate's last priced day, whether it was stale, and the review
  flags; an import records its uploaded file's SHA-256. A refresh downloads every source's whole
  history (`services.prices`), so its flags cover every day, and the latest refresh's flags are what
  a report shows. A refresh with no prices is refused: a source that came back empty fails loudly.
- **The user's own prices stand.** A refresh replaces the day's fetched price, never one the user
  imported (method `import`): an import is the fallback for a source that went wrong (T-304), so a
  later refresh mustn't undo it. An import replaces whatever the day had. A display price for a day
  whose USD price was imported stays the fetched one (display only, never a tax figure).
  `clear_import` removes the user's imported prices for some days, recorded as a batch, so the next
  refresh fills them again.
- **Fetching never depends on imports** (T-301): `fetched_through` is the last day of fetched USD
  prices, the only thing a refresh's range may start from; imported prices may cover the user's own
  trading days.
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
from datetime import UTC, date, datetime, timedelta
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
    kind: Literal["refresh", "import", "clear"]
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
    if not rows:
        raise PriceStoreError("a refresh stores the prices it fetched")
    if any(p.method == "import" for p in rows):
        raise PriceStoreError("a refresh stores fetched prices, never imported ones")
    if (
        not isinstance(hashes, Mapping)
        or not hashes
        or not all(
            isinstance(k, str) and 1 <= len(k) <= MAX_SOURCE and isinstance(v, str) and _HASH.fullmatch(v)
            for k, v in hashes.items()
        )
    ):
        raise PriceStoreError("a refresh records each source's SHA-256")
    if usd_through is not None and (type(usd_through) is not date or usd_through < FIRST_DAY):
        raise PriceStoreError("the reference rate's last day is a date")
    if not isinstance(usd_stale, bool):
        raise PriceStoreError("a refresh says whether the reference rate is stale")
    if usd_through is None and not usd_stale:
        raise PriceStoreError("a reference rate that priced no day is stale")
    _distinct(rows)
    with _refusals(), transaction(conn):
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


def store_import(conn: sqlite3.Connection, prices: Iterable[Price], *, file_hash: str, at: datetime) -> int:
    """Store the user's imported prices and their batch, with the uploaded file's SHA-256, all or
    nothing, replacing whatever each day had. Returns the batch's id."""
    rows = [_checked(p) for p in prices]
    if not rows or any(p.method != "import" for p in rows):
        raise PriceStoreError("an import stores the user's own prices")
    if not isinstance(file_hash, str) or not _HASH.fullmatch(file_hash):
        raise PriceStoreError("an import records its file's SHA-256")
    _distinct(rows)
    with _refusals(), transaction(conn):
        batch = _batch(
            conn,
            kind="import",
            at=at,
            usd_through=None,
            usd_stale=False,
            hashes={"upload": file_hash},
            flags={},
        )
        conn.executemany(
            "INSERT INTO price (day, currency, price, method, source, batch_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (day, currency) DO UPDATE SET price = excluded.price, method = excluded.method,"
            " source = excluded.source, batch_id = excluded.batch_id",
            [_row(p, batch) for p in rows],
        )
        return batch


def clear_import(conn: sqlite3.Connection, days: Iterable[date], currency: str, *, at: datetime) -> int:
    """Remove the user's imported prices in `currency` on `days`, recorded as a batch, so the next
    refresh fills them again. Days without an imported price are left as they are. Returns the
    batch's id."""
    wanted = sorted(set(days)) if not isinstance(days, (str, bytes)) else []
    if not wanted or not all(isinstance(d, date) and not isinstance(d, datetime) for d in wanted):
        raise PriceStoreError("clearing names the days")
    if currency not in CURRENCIES:
        raise PriceStoreError("a price's currency is USD, EUR or GBP")
    with _refusals(), transaction(conn):
        batch = _batch(conn, kind="clear", at=at, usd_through=None, usd_stale=False, hashes={}, flags={})
        conn.executemany(
            "DELETE FROM price WHERE day = ? AND currency = ? AND method = 'import'",
            [(d.isoformat(), currency) for d in wanted],
        )
        return batch


def fetched_through(conn: sqlite3.Connection) -> date | None:
    """The last day of fetched USD prices (never an imported one): where a refresh's range may start
    (T-301)."""
    (day,) = conn.execute(
        "SELECT max(day) FROM price WHERE currency = 'USD' AND method != 'import'"
    ).fetchone()
    if day is None:
        return None
    with _damaged():
        d = date.fromisoformat(day)
        if d < FIRST_DAY:
            raise PriceStoreError(_DAMAGED)
        return d


def price_on(conn: sqlite3.Connection, day: date, currency: str) -> Price | None:
    """The stored price for `day` in `currency`, if there is one."""
    if not isinstance(day, date) or currency not in CURRENCIES:
        raise PriceStoreError("a price is asked for by day and a supported currency")
    row = conn.execute(
        "SELECT p.day, p.currency, p.price, p.method, p.source, b.kind FROM price p"
        " JOIN price_batch b ON b.id = p.batch_id WHERE p.day = ? AND p.currency = ?",
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
        "SELECT p.day, p.currency, p.price, p.method, p.source, b.kind FROM price p"
        " JOIN price_batch b ON b.id = p.batch_id WHERE p.currency = ? ORDER BY p.day",
        (currency,),
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
        flags_json = json.dumps(dict(flags), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise PriceStoreError("a refresh's review flags are plain data") from None
    cur = conn.execute(
        "INSERT INTO price_batch (at, kind, usd_through, usd_stale, hashes, flags) VALUES (?, ?, ?, ?, ?, ?)",
        (
            at.astimezone(UTC).isoformat(),
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
    if type(p.day) is not date or p.day < FIRST_DAY:
        raise PriceStoreError("a price's day is a date from Bitcoin's first block on")
    if p.currency not in CURRENCIES:
        raise PriceStoreError("a price's currency is USD, EUR or GBP")
    if p.method not in METHODS:
        raise PriceStoreError("a price's method is one the app knows")
    if p.currency == "USD" and p.method == "fx":
        raise PriceStoreError("a USD price comes from a source, never a conversion (ADR 0039)")
    if not isinstance(p.source, str) or not 1 <= len(p.source) <= MAX_SOURCE:
        raise PriceStoreError("a price names its source")
    if not isinstance(p.price, Decimal) or not p.price.is_finite() or p.price <= 0:
        raise PriceStoreError("a price is an amount above zero")
    if not _PRICE.fullmatch(format(p.price, "f")):
        raise PriceStoreError("a price is in whole cents, with at most 15 integer digits")
    return p


def _distinct(rows: Sequence[Price]) -> None:
    if len({(p.day, p.currency) for p in rows}) != len(rows):
        raise PriceStoreError("a batch prices each day once per currency")


@contextmanager
def _refusals() -> Iterator[None]:
    """The schema's refusals as a `PriceStoreError` with a fixed message: SQLite's names the constraint."""
    try:
        yield
    except sqlite3.IntegrityError:
        raise PriceStoreError("the prices break a rule of the price table") from None


def _row(p: Price, batch: int) -> tuple[str, str, str, str, str, int]:
    return (p.day.isoformat(), p.currency, format(p.price, "f"), p.method, p.source, batch)


@contextmanager
def _damaged() -> Iterator[None]:
    try:
        yield
    except (ValueError, TypeError, ArithmeticError, InvalidOperation, KeyError, PriceStoreError):
        raise PriceStoreError(_DAMAGED) from None


def _price(r: Sequence[Any]) -> Price:
    day, currency, text, method, source, kind = r
    if not isinstance(day, str) or not _DAY.fullmatch(day) or not isinstance(text, str):
        raise PriceStoreError(_DAMAGED)
    if (method == "import") != (
        kind == "import"
    ) or kind == "clear":  # a price and the batch it says wrote it
        raise PriceStoreError(_DAMAGED)
    p = Price(date.fromisoformat(day), currency, Decimal(text), method, source)
    _checked(p)
    if format(p.price, "f") != text:
        raise PriceStoreError(_DAMAGED)
    return p


def _stored_batch(r: Sequence[Any]) -> Batch:
    batch_id, at, kind, usd_through, usd_stale, hashes, flags = r
    moment = datetime.fromisoformat(at)
    if (
        moment.isoformat() != at
        or moment.utcoffset() != timedelta(0)
        or kind not in ("refresh", "import", "clear")
        or usd_stale not in (0, 1)
    ):
        raise PriceStoreError(_DAMAGED)
    through = None if usd_through is None else date.fromisoformat(usd_through)
    if through is not None and through < FIRST_DAY:
        raise PriceStoreError(_DAMAGED)
    if kind == "refresh" and through is None and not usd_stale:
        raise PriceStoreError(_DAMAGED)
    hash_map, flag_map = json.loads(hashes), json.loads(flags)
    if not isinstance(hash_map, dict) or not isinstance(flag_map, dict):
        raise PriceStoreError(_DAMAGED)
    if not all(
        isinstance(k, str) and 1 <= len(k) <= MAX_SOURCE and isinstance(v, str) and _HASH.fullmatch(v)
        for k, v in hash_map.items()
    ):
        raise PriceStoreError(_DAMAGED)
    return Batch(batch_id, moment, kind, through, bool(usd_stale), hash_map, flag_map)


__all__ = [
    "CURRENCIES",
    "FIRST_DAY",
    "METHODS",
    "Batch",
    "Price",
    "PriceStoreError",
    "clear_import",
    "fetched_through",
    "last_refresh",
    "price_on",
    "prices",
    "store_import",
    "store_refresh",
]

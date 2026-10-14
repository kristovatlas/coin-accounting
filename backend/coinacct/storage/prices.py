"""Daily prices in the user DB (PLAN §2 `price`, §6; ADR 0039; THREAT_MODEL T-303, T-304, T-408).

- **Nothing is lost without a trace.** An import's batch records the prices it replaced, and a
  clear's the imported prices it removed (`Batch.flags`), so a change to the user's own USD prices,
  which are tax figures, can be seen afterwards.
- **Batches.** A refresh's prices (`store_refresh`) or an import's (`store_import`) are written in
  one transaction with the batch that records them, or not at all (T-304). A refresh records each
  source's SHA-256 (T-303), the reference rate's last priced day, whether it was stale, and the review
  flags; an import records its uploaded file's SHA-256. A refresh downloads every source's whole
  history (`services.prices`), so its flags cover the days it wrote; a report takes each price's
  flags from the batch that wrote it (`Price.batch`, `batch`). A refresh with no prices is refused:
  a source that came back empty fails loudly.
- **The cache is authoritative once fetched** (T-304). A refresh adds the days it doesn't have and
  never changes a stored day: a source that later serves a different value for a stored day (a
  revision, or a tampered feed, T-303) is recorded in the batch's flags (`revised`, old and new),
  for review, and the stored price stands.
- **The user's own prices stand.** A refresh never touches one the user imported (method `import`):
  an import is the fallback for a source that went wrong (T-304). An import replaces whatever the
  day had. A display price for a day whose USD price was imported stays the fetched one (display
  only, never a tax figure).
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
from dataclasses import dataclass, field
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

# a price with the batch that wrote it, checked together on read; a LEFT JOIN, so a price whose batch
# is gone is refused rather than hidden
_SELECT_PRICES: Final = (
    "SELECT p.day, p.currency, p.price, p.method, p.source, p.batch_id,"
    " b.id, b.at, b.kind, b.usd_through, b.usd_stale, b.hashes, b.flags"
    " FROM price p LEFT JOIN price_batch b ON b.id = p.batch_id"
)

type Method = Literal["reference", "typical", "fx", "import"]
type Currency = Literal["USD", "EUR", "GBP"]


class PriceStoreError(DbError):
    """A price or batch the module refuses, with a fixed message that never echoes the value."""


@dataclass(frozen=True, slots=True)
class Price:
    day: date
    currency: Currency
    price: Decimal
    method: Method
    source: str
    # the batch that wrote it, once stored: its hashes say where it came from (T-303); not part of the value
    batch: int | None = field(default=None, compare=False)


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
    if not all(_covered(p.source, hashes, "refresh") for p in rows):
        raise PriceStoreError("each fetched price's source has its SHA-256 in the refresh")
    _distinct(rows)
    if usd_through is not None and isinstance(at, datetime) and at.utcoffset() is not None:
        if usd_through > at.astimezone(UTC).date():
            raise PriceStoreError("the reference rate can't have priced a day after the refresh")
    with _refusals(), transaction(conn):
        stored = {(r["day"], r["currency"]): r for r in _existing(conn, [(p.day, p.currency) for p in rows])}
        revised = [
            old | {"fetched": format(p.price, "f"), "fetched_method": p.method, "fetched_source": p.source}
            for p in rows
            if (old := stored.get((p.day.isoformat(), p.currency))) is not None
            and old["method"] != "import"
            and (old["price"], old["method"], old["source"]) != (format(p.price, "f"), p.method, p.source)
        ]
        batch = _batch(
            conn,
            kind="refresh",
            at=at,
            usd_through=usd_through,
            usd_stale=usd_stale,
            hashes=dict(hashes),
            flags=dict(flags) | ({"revised": revised} if revised else {}),
        )
        conn.executemany(  # a stored day keeps its price: the cache is authoritative once fetched (T-304)
            "INSERT INTO price (day, currency, price, method, source, batch_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (day, currency) DO NOTHING",
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
        replaced = _existing(conn, [(p.day, p.currency) for p in rows])
        batch = _batch(
            conn,
            kind="import",
            at=at,
            usd_through=None,
            usd_stale=False,
            hashes={"upload": file_hash},
            flags={"replaced": replaced},
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
    given = [] if isinstance(days, (str, bytes)) else list(days)
    if not given or not all(type(d) is date for d in given):
        raise PriceStoreError("clearing names the days")
    wanted = sorted(set(given))
    if currency not in CURRENCIES:
        raise PriceStoreError("a price's currency is USD, EUR or GBP")
    with _refusals(), transaction(conn):
        removed = [r for r in _existing(conn, [(d, currency) for d in wanted]) if r["method"] == "import"]
        if not removed:
            raise PriceStoreError("none of those days has an imported price")
        batch = _batch(
            conn,
            kind="clear",
            at=at,
            usd_through=None,
            usd_stale=False,
            hashes={},
            flags={"currency": currency, "removed": removed},
        )
        conn.executemany(
            "DELETE FROM price WHERE day = ? AND currency = ? AND method = 'import'",
            [(d.isoformat(), currency) for d in wanted],
        )
        return batch


def fetched_through(conn: sqlite3.Connection) -> date | None:
    """The last day any refresh's reference rate priced, from the refreshes' own records: where a
    refresh's range may start (T-301). An import, which overwrites price rows, never moves it. The
    batch it comes from is checked whole."""
    row = conn.execute(
        "SELECT id, at, kind, usd_through, usd_stale, hashes, flags FROM price_batch"
        " WHERE kind = 'refresh' AND usd_through IS NOT NULL ORDER BY usd_through DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    with _damaged():
        return _stored_batch(row).usd_through


def batch(conn: sqlite3.Connection, batch_id: int) -> Batch | None:
    """The batch that wrote a price (`Price.batch`), with its hashes: a price's provenance (T-303)."""
    if isinstance(batch_id, bool) or not isinstance(batch_id, int):
        raise PriceStoreError("a batch is named by its id")
    row = conn.execute(
        "SELECT id, at, kind, usd_through, usd_stale, hashes, flags FROM price_batch WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None
    with _damaged():
        return _stored_batch(row)


def _stored_day(text: object) -> date:
    if not isinstance(text, str) or not _DAY.fullmatch(text):  # fromisoformat takes other ISO forms too
        raise PriceStoreError(_DAMAGED)
    d = date.fromisoformat(text)
    if d < FIRST_DAY:
        raise PriceStoreError(_DAMAGED)
    return d


def price_on(conn: sqlite3.Connection, day: date, currency: str) -> Price | None:
    """The stored price for `day` in `currency`, if there is one."""
    if type(day) is not date or currency not in CURRENCIES:
        raise PriceStoreError("a price is asked for by day and a supported currency")
    row = conn.execute(
        _SELECT_PRICES + " WHERE p.day = ? AND p.currency = ?",
        (day.isoformat(), currency),
    ).fetchone()
    if row is None:
        return None
    with _damaged():
        return _price(row, {})


def prices(conn: sqlite3.Connection, currency: str) -> list[Price]:
    """Every stored price in `currency`, by day."""
    if currency not in CURRENCIES:
        raise PriceStoreError("prices are asked for in a supported currency")
    rows = conn.execute(
        _SELECT_PRICES + " WHERE p.currency = ? ORDER BY p.day",
        (currency,),
    )
    checked: dict[int, Batch] = {}  # each batch checked once per read, however many prices it wrote
    with _damaged():
        return [_price(r, checked) for r in rows]


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
    if not isinstance(flags, Mapping) or not all(isinstance(k, str) for k in flags):
        raise PriceStoreError("a refresh's review flags are an object named by text")
    try:
        flags_json = json.dumps(dict(flags), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
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
    exponent = p.price.as_tuple().exponent
    if p.price.adjusted() > 14 or not isinstance(exponent, int) or exponent < -2:  # bounded before formatting
        raise PriceStoreError("a price is in whole cents, with at most 15 integer digits")
    if not _PRICE.fullmatch(format(p.price, "f")):
        raise PriceStoreError("a price is in whole cents, with at most 15 integer digits")
    return p


def _existing(conn: sqlite3.Connection, keys: Sequence[tuple[date, str]]) -> list[dict[str, Any]]:
    """The stored prices at `keys` (day, currency), as plain data for a batch's record of what it
    replaced or removed."""
    out: list[dict[str, Any]] = []
    for day, currency in keys:
        row = conn.execute(
            "SELECT price, method, source, batch_id FROM price WHERE day = ? AND currency = ?",
            (day.isoformat(), currency),
        ).fetchone()
        if row is not None:
            price, method, source, batch_id = row
            out.append(
                {
                    "day": day.isoformat(),
                    "currency": currency,
                    "price": price,
                    "method": method,
                    "source": source,
                }
                | {"batch": batch_id}
            )
    return out


def _covered(source: str, hashes: Mapping[str, str], kind: str) -> bool:
    """Whether the hashes name every source a price came from: a converted price's source joins its
    parts with `*` (`prices.fx`). An import's prices come from its one uploaded file."""
    if kind == "import":
        return set(hashes) == {"upload"}
    return all(part in hashes for part in source.split("*"))


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
    except (
        ValueError,
        TypeError,
        ArithmeticError,
        InvalidOperation,
        KeyError,
        RecursionError,
        PriceStoreError,
    ):
        raise PriceStoreError(_DAMAGED) from None


def _price(r: Sequence[Any], checked: dict[int, Batch]) -> Price:
    day, currency, text, method, source, batch_id, *batch_row = r
    if batch_row[0] is None:  # the batch it names is gone
        raise PriceStoreError(_DAMAGED)
    if batch_id not in checked:
        checked[batch_id] = _stored_batch(batch_row)  # the whole batch is checked, not only its kind
    stored_batch = checked[batch_id]
    kind = stored_batch.kind
    if not isinstance(source, str) or not _covered(source, stored_batch.hashes, kind):
        raise PriceStoreError(_DAMAGED)
    if not isinstance(day, str) or not _DAY.fullmatch(day) or not isinstance(text, str):
        raise PriceStoreError(_DAMAGED)
    if not _PRICE.fullmatch(text):  # before Decimal: a huge exponent would cost memory to format
        raise PriceStoreError(_DAMAGED)
    if (method == "import") != (
        kind == "import"
    ) or kind == "clear":  # a price and the batch it says wrote it
        raise PriceStoreError(_DAMAGED)
    p = Price(_stored_day(day), currency, Decimal(text), method, source, batch_id)
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
    through = None if usd_through is None else _stored_day(usd_through)
    if through is not None and through > moment.astimezone(UTC).date():  # priced before the refresh ran
        raise PriceStoreError(_DAMAGED)
    if kind == "refresh" and through is None and not usd_stale:
        raise PriceStoreError(_DAMAGED)
    hash_map, flag_map = json.loads(hashes), json.loads(flags)
    if not isinstance(hash_map, dict) or not isinstance(flag_map, dict):
        raise PriceStoreError(_DAMAGED)
    if (kind == "clear") != (not hash_map):  # a refresh or an import records hashes, a clear none
        raise PriceStoreError(_DAMAGED)
    if kind != "refresh" and (through is not None or usd_stale):
        raise PriceStoreError(_DAMAGED)
    if kind == "import" and set(hash_map) != {"upload"}:
        raise PriceStoreError(_DAMAGED)
    if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id < 1:
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
    "batch",
    "clear_import",
    "fetched_through",
    "last_refresh",
    "price_on",
    "prices",
    "store_import",
    "store_refresh",
]

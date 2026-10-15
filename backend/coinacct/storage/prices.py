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
  changes a stored one only to replace Bitstamp's fill with the reference rate once Coin Metrics has
  the day (ADR 0039, `upgraded`), or a display price (`display_updated`). Otherwise it never changes
  a stored day: a source that later serves a different value for a stored day (a
  revision, or a tampered feed, T-303) is recorded in the batch's flags (`revised`, old and new),
  for review, and the stored price stands. So is a day refilled after the user cleared an import,
  against the fetched value that import replaced; and a fetched value that differs from the user's
  imported price is recorded as `kept_import`. Both keys are the module's own: a caller can't set them.
- **The user's own prices stand.** A refresh never touches one the user imported (method `import`):
  an import is the fallback for a source that went wrong (T-304). An import replaces whatever the
  day had. A display price for a day whose USD price was imported stays the fetched one (display
  only, never a tax figure).
  `clear_import` removes the user's imported prices for some days, recorded as a batch, so the next
  refresh fills them again.
- **Fetching never depends on imports** (T-301): a refresh downloads every source's whole history
  today; `fetched_through`, the refreshes' own record of the last day priced, is the only start a
  future incremental refresh may use, since imported prices may cover the user's own trading days.
- **Values.** A price is a `Decimal` above zero in whole cents, at most 15 integer digits, as
  `prices.DailyPrice` rounds it; a day is a UTC date from Bitcoin's first block on; the currency is
  USD (tax figures) or EUR or GBP (display only).
- **Reads are checked** (T-408): a stored row the module wouldn't write is refused with the remedy,
  never half-read.

The module reads no clock: the caller passes the batch's time.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from coinacct.storage.db import DbBusy, DbError, transaction

log = logging.getLogger(__name__)

CURRENCIES: Final = ("USD", "EUR", "GBP")
METHODS: Final = ("reference", "typical", "fx", "import")
FIRST_DAY: Final = date(2009, 1, 3)  # Bitcoin's first block
MAX_SOURCE: Final = 200
_PRICE: Final = re.compile(r"(?:0|[1-9][0-9]{0,14})\.[0-9]{2}", re.ASCII)
_DAY: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_HASH: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)
# a refresh can't mend a stored day or batch (both are kept as stored), so the remedy is the backup
_DAMAGED: Final = "a stored price is damaged (T-408): restore the user DB from a backup"
# the source family each fetched method comes from (ADR 0039): Coin Metrics' reference rate, Bitstamp's
# typical price, an ECB conversion (`prices.coinmetrics`, `prices.bitstamp`, `prices.fx`)
_FAMILY: Final = {"reference": "coinmetrics", "typical": "bitstamp", "fx": "ecb"}
# a batch records only complete days, before its own UTC date: a write of a later one is refused (a
# clock set back can cause it, though the bound doesn't detect every clock change)
_EARLIER: Final = "a batch records only days before its own UTC date: check the system clock"
_REFRESH_FLAGS: Final = ("revised", "kept_import", "upgraded", "display_updated")  # the module's own
# the review flags a refresh's caller records (`services.price_cache.flags_of`): three lists of
# records, and two maps holding a list of records per currency (T-303)
REVIEW_LISTS: Final = ("mismatches", "reference_gaps", "reference_outliers")
REVIEW_PER_CURRENCY: Final = ("gaps", "outliers")

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


def _read[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """A reader whose SQLite errors are the module's fixed messages too (`_sqlite_refusal`)."""

    @functools.wraps(fn)
    def reading(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as e:
            raise _sqlite_refusal(e, "read from") from None

    return reading


def _sqlite_refusal(e: sqlite3.Error, how: str) -> DbError:
    """A SQLite error as a fixed message, with only its error code in the log (its text can name the
    constraint or the file): busy is `DbBusy`; a damaged file names the backup (T-408); anything else
    (a full disk, an I/O error) says the DB can't be used now."""
    code = str(getattr(e, "sqlite_errorname", None) or type(e).__name__)
    log.warning("prices couldn't be %s the user DB: %s", how, code)
    if code.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return DbBusy("the user DB is busy; try again in a moment")
    if code.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")):
        return PriceStoreError(_DAMAGED)
    return PriceStoreError("the user DB can't be used right now; see the log")


def store_refresh(  # noqa: PLR0912, PLR0913 - a refresh's prices and what it recorded, each checked
    conn: sqlite3.Connection,
    prices: Iterable[Price],
    *,
    hashes: Mapping[str, str],
    usd_through: date | None,
    usd_stale: bool,
    flags: Mapping[str, Any],
    at: datetime,
    before_commit: Callable[[], bool] = lambda: False,
) -> int | None:
    """Store a refresh's prices and its batch, all or nothing; imported prices are kept. Returns the
    batch's id, or None when `before_commit`, asked last, before the commit, says to roll back (a
    refresh cancelled while it was being stored). The commit runs inside the module's refusals, so a
    disk that fails there is a fixed refusal too."""
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
    reference = [p.day for p in rows if p.currency == "USD" and p.method == "reference"]
    if usd_through != (max(reference) if reference else None):
        raise PriceStoreError("the reference rate's last day is the last reference price the refresh stores")
    if not isinstance(flags, Mapping) or set(_REFRESH_FLAGS) & set(flags):
        raise PriceStoreError("a refresh's revisions are the module's own record")
    bound = _bound(at)
    if bound is not None and any(p.day >= bound for p in rows):
        raise PriceStoreError(_EARLIER)
    if not _review_flags(flags, before=bound):
        raise PriceStoreError("a refresh records every kind of review flag, each as a list of records")
    if any("*" in k for k in hashes):
        raise PriceStoreError("a source's name has no '*'")
    if not all(_covered(p.source, hashes, "refresh") for p in rows):
        raise PriceStoreError("each fetched price's source has its SHA-256 in the refresh")
    _distinct(rows)
    try:
        return _write_refresh(conn, rows, hashes, usd_through, usd_stale, flags, at, before_commit)
    except _RolledBack:
        return None


class _RolledBack(Exception):
    """`before_commit` asked to roll the refresh back."""


def _write_refresh(  # noqa: PLR0913, PLR0917 - store_refresh's checked arguments
    conn: sqlite3.Connection,
    rows: list[Price],
    hashes: Mapping[str, str],
    usd_through: date | None,
    usd_stale: bool,
    flags: Mapping[str, Any],
    at: datetime,
    before_commit: Callable[[], bool],
) -> int:
    with _refusals(), transaction(conn):
        found = _revisions(conn, rows)
        batch = _batch(
            conn,
            kind="refresh",
            at=at,
            usd_through=usd_through,
            usd_stale=usd_stale,
            hashes=dict(hashes),
            flags=(recorded := dict(flags) | found),
        )
        _readable("refresh", recorded, batch, at, dict(hashes))
        conn.executemany(  # a stored day keeps its price unless `_replaces` says so (T-304, ADR 0039)
            "INSERT INTO price (day, currency, price, method, source, batch_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (day, currency) DO UPDATE SET price = excluded.price, method = excluded.method,"
            " source = excluded.source, batch_id = excluded.batch_id"
            " WHERE price.method != 'import' AND (price.currency != 'USD'"
            " OR (price.method = 'typical' AND excluded.method = 'reference'))",
            [_row(p, batch) for p in rows],
        )
        if before_commit():
            raise _RolledBack
        return batch


def store_import(conn: sqlite3.Connection, prices: Iterable[Price], *, file_hash: str, at: datetime) -> int:
    """Store the user's imported prices and their batch, with the uploaded file's SHA-256, all or
    nothing, replacing whatever each day had. Returns the batch's id."""
    rows = [_checked(p) for p in prices]
    if not rows or any(p.method != "import" for p in rows):
        raise PriceStoreError("an import stores the user's own prices")
    if not isinstance(file_hash, str) or not _HASH.fullmatch(file_hash):
        raise PriceStoreError("an import records its file's SHA-256")
    bound = _bound(at)
    if bound is not None and any(p.day >= bound for p in rows):
        raise PriceStoreError(_EARLIER)
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
            flags=(recorded := {"replaced": replaced}),
        )
        _readable("import", recorded, batch, at, {"upload": file_hash})
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
    bound = _bound(at)
    with _refusals(), transaction(conn):
        removed = [r for r in _existing(conn, [(d, currency) for d in wanted]) if r["method"] == "import"]
        if not removed:
            raise PriceStoreError("none of those days has an imported price")
        if bound is not None and removed[-1]["day"] >= bound.isoformat():  # in order of day (`_existing`)
            raise PriceStoreError(_EARLIER)
        batch = _batch(
            conn,
            kind="clear",
            at=at,
            usd_through=None,
            usd_stale=False,
            hashes={},
            flags=(recorded := {"currency": currency, "removed": removed}),
        )
        _readable("clear", recorded, batch, at, {})
        conn.executemany(
            "DELETE FROM price WHERE day = ? AND currency = ? AND method = 'import'",
            [(d.isoformat(), currency) for d in wanted],
        )
        return batch


@_read
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


@_read
def refresh_status(conn: sqlite3.Connection) -> tuple[Batch | None, date | None]:
    """The last refresh's batch and `fetched_through`, read in one snapshot on a reader connection."""
    conn.execute("BEGIN")
    try:
        out = last_refresh(conn), fetched_through(conn)
    except BaseException:
        if conn.in_transaction:  # SQLite may have ended it itself after an error
            with contextlib.suppress(sqlite3.Error):  # the first error is the one that counts
                conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK")  # a read only: nothing to commit
    return out


@_read
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
        return _provenance(conn, _stored_batch(row))


def _stored_day(text: object) -> date:
    if not isinstance(text, str) or not _DAY.fullmatch(text):  # fromisoformat takes other ISO forms too
        raise PriceStoreError(_DAMAGED)
    d = date.fromisoformat(text)
    if d < FIRST_DAY:
        raise PriceStoreError(_DAMAGED)
    return d


@_read
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


@_read
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


@_read
def last_refresh(conn: sqlite3.Connection) -> Batch | None:
    """The latest refresh's batch: its hashes, the reference rate's state and its review flags."""
    row = conn.execute(
        "SELECT id, at, kind, usd_through, usd_stale, hashes, flags FROM price_batch"
        " WHERE kind = 'refresh' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    with _damaged():
        return _provenance(conn, _stored_batch(row))


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


def _bound(at: object) -> date | None:
    """The UTC date of a batch's moment, if it is a timezone-aware one (`_batch` refuses any other)."""
    return at.astimezone(UTC).date() if isinstance(at, datetime) and at.utcoffset() is not None else None


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
    if p.method in _FAMILY and p.source.split("*")[0].split(":")[0] != _FAMILY[p.method]:
        raise PriceStoreError("a fetched price's source is the one its method names (ADR 0039)")
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
    """The stored prices at `keys` (day, currency), each checked with its batch as a read is (T-408),
    as plain data for a batch's record of what it replaced or removed: a damaged price is refused, not
    overwritten or removed."""
    out: list[dict[str, Any]] = []
    checked: dict[int, Batch] = {}
    for day, currency in keys:
        row = conn.execute(
            _SELECT_PRICES + " WHERE p.day = ? AND p.currency = ?", (day.isoformat(), currency)
        ).fetchone()
        if row is None:
            continue
        with _damaged():
            stored = _price(row, checked)
        out.append(
            {
                "day": day.isoformat(),
                "currency": currency,
                "price": format(stored.price, "f"),
                "method": stored.method,
            }
            | {"source": stored.source, "batch": stored.batch}
        )
    return out


def _revisions(conn: sqlite3.Connection, rows: Sequence[Price]) -> dict[str, list[dict[str, Any]]]:
    """What a refresh found that differs from the cache, each old value with the new:
    - `upgraded`: a stored USD typical price (Bitstamp's fill, ADR 0039) the reference rate now has,
      which the refresh replaces;
    - `display_updated`: a stored EUR or GBP price (display only) the refresh replaces;
    - `revised`: a stored USD price the source now serves otherwise, or a cleared day refilled unlike
      what was fetched before, which is kept as stored and shown for review (T-303, T-304);
    - `kept_import`: a day the user imported that the source disagrees with, kept as the user's."""
    stored = {(r["day"], r["currency"]): r for r in _existing(conn, [(p.day, p.currency) for p in rows])}
    earlier = _fetched_before(conn)  # fetched values imports replaced: a cleared day's last fetch
    found: dict[str, list[dict[str, Any]]] = {k: [] for k in _REFRESH_FLAGS}
    for p in rows:
        key, new = (p.day.isoformat(), p.currency), (format(p.price, "f"), p.method, p.source)
        fetched = {"fetched": new[0], "fetched_method": p.method, "fetched_source": p.source}
        old = stored.get(key) or earlier.get(key)
        if old is None or (old["price"], old["method"], old["source"]) == new:
            continue
        if key not in stored:
            # refilled after a clear, unlike what was fetched before: a display price is a display update
            found["revised" if p.currency == "USD" else "display_updated"].append(old | fetched)
        elif old["method"] == "import":
            if old["price"] != new[0]:
                found["kept_import"].append(old | fetched)
        elif _replaces(old["currency"], old["method"], p.method):
            found["upgraded" if p.currency == "USD" else "display_updated"].append(old | fetched)
        else:
            found["revised"].append(old | fetched)
    return found


_SNAPSHOT_KEYS: Final = frozenset({"day", "currency", "price", "method", "source", "batch"})


def _check_flags(
    kind: str, flags: dict[str, Any], *, batch_id: int, before: date, hashes: Mapping[str, str]
) -> None:
    """A batch's flags hold the record its kind always writes (T-408): a refresh its own four lists and
    the caller's five review kinds (`_review_flags`), an import the prices it replaced, a clear its
    currency and the imported prices in it that it removed. Every day is before the batch's own UTC date,
    every price recorded names an earlier batch, and what a refresh fetched names a source it hashed."""
    lists = _lists(kind, flags, before=before)
    seen: set[tuple[object, object]] = set()
    for name, records in lists.items():
        if not isinstance(records, list):
            raise PriceStoreError(_DAMAGED)
        for r in records:
            if not _snapshot(r, kind=name, batch_id=batch_id, before=before, hashes=hashes):
                raise PriceStoreError(_DAMAGED)
            if name == "removed" and (r["method"] != "import" or r["currency"] != flags["currency"]):
                raise PriceStoreError(_DAMAGED)  # a clear removes imported prices in its one currency
            key = (r["day"], r["currency"])
            if key in seen:  # a priced day is recorded once, under one kind
                raise PriceStoreError(_DAMAGED)
            seen.add(key)


def _lists(kind: str, flags: dict[str, Any], *, before: date) -> dict[str, object]:
    """The lists of recorded prices a batch's flags hold, by its kind, once the flags hold exactly
    its kind's keys."""
    if kind == "refresh":
        if set(flags) != {*REVIEW_LISTS, *REVIEW_PER_CURRENCY, *_REFRESH_FLAGS}:
            raise PriceStoreError(_DAMAGED)  # a kind missing or extra: never "nothing to review"
        if not _review_flags(flags, before=before):
            raise PriceStoreError(_DAMAGED)
        return {k: flags.get(k) for k in _REFRESH_FLAGS}
    if kind == "import":
        if set(flags) != {"replaced"}:
            raise PriceStoreError(_DAMAGED)
        return {"replaced": flags.get("replaced")}
    if set(flags) != {"currency", "removed"} or flags["currency"] not in CURRENCIES or not flags["removed"]:
        raise PriceStoreError(_DAMAGED)
    return {"removed": flags.get("removed")}


# each review kind's record, as `services.price_cache.flags_of` writes it from `prices`' Mismatch, Gap
# and Outlier: its fields, each a day or a price as text
_REVIEW_RECORDS: Final = {
    "mismatches": {"day": "day", "reference": "price", "typical": "price"},
    "reference_gaps": {"first": "day", "last": "day"},
    "reference_outliers": {"day": "day", "previous": "price", "price": "price"},
    "gaps": {"first": "day", "last": "day"},
    "outliers": {"day": "day", "previous": "price", "price": "price"},
}


def _review_flags(flags: Mapping[str, Any], *, before: date | None = None) -> bool:
    """The caller's review flags as a refresh records them: every kind, each in its shape (a list, or
    a list per currency for `gaps` and `outliers`), every record with exactly its kind's fields, each a
    real day from Bitcoin's first block on or a price of at least a cent in the form the module writes,
    each list's records in order of their day, gaps apart (a priced day between two), as
    `prices.check` makes them, and every day before `before` (the batch's UTC date), if given."""

    def record(kind: str, r: object) -> bool:
        fields = _REVIEW_RECORDS[kind]
        if not isinstance(r, dict) or set(r) != set(fields):
            return False
        for name, form in fields.items():
            if not (_is_day(r[name], before) if form == "day" else _is_price(r[name])):
                return False
        return "first" not in r or r["first"] <= r["last"]

    def records(kind: str, value: object) -> bool:
        if not isinstance(value, list) or not all(record(kind, r) for r in value):
            return False
        starts = [r.get("day", r.get("first")) for r in value]
        if any(a >= b for a, b in itertools.pairwise(starts)):
            return False  # out of order, or a day twice
        return (
            all(
                date.fromisoformat(a["last"]) + timedelta(days=1) < date.fromisoformat(b["first"])
                for a, b in itertools.pairwise(value)
            )
            if "first" in _REVIEW_RECORDS[kind]
            else True
        )

    if set(flags) - set(_REFRESH_FLAGS) != {*REVIEW_LISTS, *REVIEW_PER_CURRENCY}:
        return False
    if not all(records(k, flags[k]) for k in REVIEW_LISTS):
        return False
    return all(
        isinstance(m := flags[k], dict)
        and set(m) == set(CURRENCIES)
        and all(records(k, v) for v in m.values())
        for k in REVIEW_PER_CURRENCY
    )


_FETCHED_KEYS: Final = frozenset({"fetched", "fetched_method", "fetched_source"})


def _is_day(value: object, before: date | None = None) -> bool:
    """A real UTC day, written as the module writes it, from Bitcoin's first block on, and before
    `before` if given."""
    if not isinstance(value, str) or _DAY.fullmatch(value) is None:
        return False
    try:
        day = date.fromisoformat(value)
    except ValueError:  # 2025-02-30 has the form of a day but isn't one
        return False
    return day >= FIRST_DAY and (before is None or day < before)


def _is_price(value: object) -> bool:
    """A price of at least a cent, written as the module writes it (`_PRICE`)."""
    return (
        isinstance(value, str) and _PRICE.fullmatch(value) is not None and Decimal(value) >= Decimal("0.01")
    )


def _source(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_SOURCE


def _snapshot(  # noqa: PLR0911 - one rule per kind
    r: object, *, kind: str, batch_id: int, before: date, hashes: Mapping[str, str]
) -> bool:
    """A recorded price as the module writes it, by the list it is in: exactly its fields (a refresh's
    own records also hold what the source now serves, from a source the batch hashed), each in its
    form, each price by a method its currency may have from its method's source family (`_checked`),
    an earlier batch, and the rule its kind follows (`_revisions`)."""
    fetched = kind in _REFRESH_FLAGS
    keys = _SNAPSHOT_KEYS | _FETCHED_KEYS if fetched else _SNAPSHOT_KEYS
    if not isinstance(r, dict) or set(r) != keys:
        return False
    if not (
        _is_day(r["day"], before)
        and r["currency"] in CURRENCIES
        and _is_price(r["price"])
        and r["method"] in METHODS
        and _source(r["source"])
        and _priced_by(r["currency"], r["method"], r["source"])
        and isinstance(r["batch"], int)
        and not isinstance(r["batch"], bool)
        and 1 <= r["batch"] < batch_id
    ):
        return False
    if not fetched:
        return True
    method = r["fetched_method"]
    if not (_is_price(r["fetched"]) and method in _FAMILY and _source(r["fetched_source"])):
        return False  # a refresh fetches no import
    if not _priced_by(r["currency"], method, r["fetched_source"]):
        return False
    if not _covered(r["fetched_source"], hashes, "refresh"):
        return False  # what the source now serves came from a source this refresh hashed
    old, new = (r["price"], r["method"], r["source"]), (r["fetched"], method, r["fetched_source"])
    if kind == "kept_import":
        return bool(r["method"] == "import" and r["price"] != r["fetched"])
    if r["method"] == "import" or old == new:
        return False
    if kind == "upgraded":
        return bool(r["currency"] == "USD" and r["method"] == "typical" and method == "reference")
    if kind == "display_updated":
        return bool(r["currency"] != "USD")
    return bool(r["currency"] == "USD")  # revised


def _priced_by(currency: str, method: str, source: str) -> bool:
    """A price's method and source as `_checked` holds them: a USD price is never a conversion, and a
    fetched price's source is its method's family (ADR 0039)."""
    if currency == "USD" and method == "fx":
        return False
    return method not in _FAMILY or source.split("*", 1)[0].split(":", 1)[0] == _FAMILY[method]


def _replaces(currency: str, stored_method: str, fetched_method: str) -> bool:
    """Whether a refresh replaces a stored fetched price: a display price always; a USD price only
    from Bitstamp's fill to the reference rate (ADR 0039); never the user's own."""
    if stored_method == "import":
        return False
    if currency != "USD":
        return True
    return stored_method == "typical" and fetched_method == "reference"


def _fetched_before(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """The last fetched value each import replaced, by (day, currency), from the import batches' own
    records: what a day held before the user's price, which a clear later removed. Every import batch
    is checked whole, so a damaged one refuses every refresh with the backup remedy (fail closed,
    T-408), even one whose days the refresh doesn't touch."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT id, at, kind, usd_through, usd_stale, hashes, flags FROM price_batch"
        " WHERE kind = 'import' ORDER BY id"
    )
    for row in rows:
        with _damaged():  # the whole batch is checked, so damage there is refused as damage
            stored = _provenance(conn, _stored_batch(row))
        for r in stored.flags["replaced"]:
            if r["method"] != "import":
                out[(r["day"], r["currency"])] = r
    return out


_RECORDS: Final = {"refresh": _REFRESH_FLAGS, "import": ("replaced",), "clear": ("removed",)}


def _provenance(conn: sqlite3.Connection, stored: Batch) -> Batch:
    """A checked batch whose recorded prices each hold to the batch they name, as a stored price does
    (`_price`): a refresh or an import, never a clear; an imported price exactly when it is an import;
    and a source its hashes cover (T-303, T-408). Each named batch is read once."""
    named: dict[int, tuple[object, object]] = {}
    for name in _RECORDS[stored.kind]:
        for r in stored.flags[name]:
            if r["batch"] not in named:
                row = conn.execute(
                    "SELECT kind, hashes FROM price_batch WHERE id = ?", (r["batch"],)
                ).fetchone()
                if row is None:  # the batch it names is gone
                    raise PriceStoreError(_DAMAGED)
                named[r["batch"]] = (row[0], json.loads(row[1]))
            kind, hashes = named[r["batch"]]
            if (
                kind not in ("refresh", "import")
                or not isinstance(hashes, dict)
                or (r["method"] == "import") != (kind == "import")
                or not _covered(r["source"], hashes, kind)
            ):
                raise PriceStoreError(_DAMAGED)
    return stored


def _covered(source: str, hashes: Mapping[str, str], kind: str) -> bool:
    """Whether the hashes name every source a price came from: a converted price's source joins its
    parts with `*` (`prices.fx`). An import's prices come from its one uploaded file."""
    if kind == "import":
        return set(hashes) == {"upload"}
    return all(part in hashes for part in source.split("*"))


def _readable(kind: str, flags: Mapping[str, Any], batch: int, at: datetime, hashes: dict[str, str]) -> None:
    """Refuse, before the commit, a record that the read (`_check_flags`) would call damage: the write
    and the read hold the same rules, so a store the module wrote always reads back."""
    try:
        _check_flags(kind, dict(flags), batch_id=batch, before=at.astimezone(UTC).date(), hashes=hashes)
    except PriceStoreError:
        raise PriceStoreError("the change would leave a price record the store can't read back") from None


def _distinct(rows: Sequence[Price]) -> None:
    if len({(p.day, p.currency) for p in rows}) != len(rows):
        raise PriceStoreError("a batch prices each day once per currency")


@contextmanager
def _refusals() -> Iterator[None]:
    """The schema's refusals, and any other SQLite error while writing or committing, as the module's
    fixed messages (`_sqlite_refusal`)."""
    try:
        yield
    except sqlite3.IntegrityError:
        raise PriceStoreError("the prices break a rule of the price table") from None
    except sqlite3.Error as e:
        raise _sqlite_refusal(e, "written to") from None


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
    if _stored_day(day) >= stored_batch.at.astimezone(UTC).date():  # a batch prices only complete days
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
    if through is not None and through >= moment.astimezone(UTC).date():  # a complete day, before the batch's
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
    _check_flags(kind, flag_map, batch_id=batch_id, before=moment.astimezone(UTC).date(), hashes=hash_map)
    if not all(
        isinstance(k, str)
        and 1 <= len(k) <= MAX_SOURCE
        and "*" not in k
        and isinstance(v, str)
        and _HASH.fullmatch(v)
        for k, v in hash_map.items()
    ):
        raise PriceStoreError(_DAMAGED)
    return Batch(batch_id, moment, kind, through, bool(usd_stale), hash_map, flag_map)


__all__ = [
    "CURRENCIES",
    "FIRST_DAY",
    "METHODS",
    "REVIEW_LISTS",
    "REVIEW_PER_CURRENCY",
    "Batch",
    "Price",
    "PriceStoreError",
    "batch",
    "clear_import",
    "fetched_through",
    "last_refresh",
    "price_on",
    "prices",
    "refresh_status",
    "store_import",
    "store_refresh",
]

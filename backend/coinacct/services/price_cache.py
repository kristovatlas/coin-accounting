"""The price cache: a refresh's result or the user's CSV, stored (PLAN §6, M5; ADR 0039; THREAT_MODEL
T-301, T-303, T-304).

- **A refresh** (`PriceCache.refresh`) downloads and checks every source (`services.prices.refresh`),
  then stores every series it returned in one batch with each source's SHA-256, the reference rate's
  last day and staleness, and the review flags as plain data (`storage.prices.store_refresh`). A
  failure anywhere stores nothing (T-304); a cancel, even one that arrives while the batch is being
  stored, stores nothing and returns None (the batch commits only after a last check). A cancel that
  arrives after that commit loses to it: the job wiring must report such a job done, not cancelled.
  It is meant to run as a job on the job worker (architecture §3); the job wiring and the route are
  the next slice,
  which must carry a refusal's fixed message to the user (the job worker keeps only an error's class
  today), so a stored-data refusal still names its remedy.
- **An import** (`PriceCache.import_csv`) parses the user's file (`prices.csv_import`), refuses it
  whole on any bad row, and stores it with the file's SHA-256 (T-303).
- The clock is the caller's: `now` gives the batch's moment, and its UTC date bounds which days are
  complete.

Refusals are `ImportRefused` with a fixed message: the parsers' and storage's own messages never
repeat a value, and a stored-data refusal names its remedy (T-408).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from coinacct.prices import DailyPrice, PriceError, content_hash, csv_import
from coinacct.prices.fetch import Proxy
from coinacct.services import prices as price_service
from coinacct.services.imports import ImportRefused, db_errors
from coinacct.storage import prices as store
from coinacct.storage.db import Connection, transaction


def _plain(value: object) -> object:
    """A review flag as JSON's plain data: days as ISO text, amounts as decimal text."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def flags_of(r: price_service.Refreshed) -> dict[str, Any]:
    """A refresh's review flags (T-303), as stored with its batch."""
    return {
        "mismatches": [_plain(asdict(m)) for m in r.mismatches],
        "reference_gaps": [_plain(asdict(g)) for g in r.reference_gaps],
        "reference_outliers": [_plain(asdict(o)) for o in r.reference_outliers],
        "gaps": {c: [_plain(asdict(g)) for g in gs] for c, gs in r.gaps.items()},
        "outliers": {c: [_plain(asdict(o)) for o in os] for c, os in r.outliers.items()},
    }


def _stored(prices: Sequence[DailyPrice]) -> list[store.Price]:
    return [store.Price(p.day, cast(store.Currency, p.currency), p.price, p.method, p.source) for p in prices]


def _moment(now: Callable[[], datetime]) -> datetime:
    """The batch's moment, refused before any download or parse if it has no zone: its UTC date bounds
    the complete days."""
    moment = now()
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ImportRefused("the price cache needs a timezone-aware time")
    return moment


log = logging.getLogger(__name__)


class _Cancelled(Exception):
    """A cancel that arrived while the refresh was being stored: the transaction is rolled back."""


class PriceCache:
    """The price cache over the app's writer connection."""

    def __init__(
        self,
        conn: Connection,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        refresh: Callable[..., price_service.Refreshed | None] = price_service.refresh,
    ) -> None:
        self._conn = conn
        self._now = now
        self._refresh = refresh

    def refresh(self, proxy: Proxy | None, cancelled: threading.Event) -> int | None:
        """Download, check and store every source; the batch's id, or None if cancelled."""
        moment = _moment(self._now)
        result = self._refresh(proxy, cancelled, lambda: moment.astimezone(UTC).date())
        if result is None or cancelled.is_set():
            return None
        rows = _stored(result.usd) + [p for series in result.display.values() for p in _stored(series)]
        try:
            with _refusals(), transaction(self._conn):  # the store's own transaction nests in this one
                batch = store.store_refresh(
                    self._conn,
                    rows,
                    hashes=result.hashes,
                    usd_through=result.usd_through,
                    usd_stale=result.usd_stale,
                    flags=flags_of(result),
                    at=moment,
                )
                if cancelled.is_set():  # a cancel while storing rolls the whole batch back
                    raise _Cancelled
        except _Cancelled:
            return None
        return batch

    def import_csv(self, data: bytes) -> int:
        """Store the user's price CSV whole, with its SHA-256; the batch's id."""
        moment = _moment(self._now)
        try:
            series = csv_import.parse(data, moment.astimezone(UTC).date())
        except PriceError:
            raise ImportRefused(
                "the price file can't be used: check it against the documented format"
            ) from None
        rows = [p for s in series.values() for p in _stored(s)]
        with _refusals():
            return store.store_import(self._conn, rows, file_hash=content_hash(data), at=moment)


@contextmanager
def _refusals() -> Iterator[None]:
    """Storage's refusals, with their fixed messages, and a busy DB, as the services' (`db_errors`)."""
    with db_errors():
        try:
            yield
        except store.PriceStoreError as e:  # its fixed message never repeats a value (T-408)
            raise ImportRefused(str(e)) from None
        except sqlite3.DatabaseError as e:  # disk full, I/O, a damaged file: SQLite's text stays out
            log.warning("the price cache couldn't use the user DB: %s", type(e).__name__)
            raise ImportRefused("the user DB can't be used right now; see the log") from None


__all__ = ["PriceCache", "flags_of"]

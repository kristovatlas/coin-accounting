"""The price cache (PLAN §6, M5; T-303, T-304): a refresh's result or the user's CSV, stored. A fake
refresh stands in for the downloads, so nothing touches the network. Synthetic prices only."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.prices import DailyPrice, Gap, Mismatch, Outlier, PriceError, content_hash
from coinacct.services.imports import Busy, ImportRefused
from coinacct.services.price_cache import PriceCache
from coinacct.services.prices import Refreshed
from coinacct.storage import prices as store
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import DbBusy, open_db

NOW = datetime(2025, 1, 10, 12, tzinfo=UTC)
H = "ab" * 32
REF = "coinmetrics:PriceUSD"
FX = "ecb:eurofxref-hist"


def usd(day: str, price: str, method: Any = "reference", source: str = REF) -> DailyPrice:
    return DailyPrice(date.fromisoformat(day), "USD", Decimal(price), method, source)


def result(**kw: Any) -> Refreshed:
    base: dict[str, Any] = {
        "usd": [
            usd("2025-01-01", "93000.12"),
            usd("2025-01-02", "94000.00", "typical", "bitstamp:ohlc:btcusd"),
        ],
        "mismatches": [Mismatch(date(2025, 1, 1), Decimal("93000.12"), Decimal("80000.00"))],
        "reference_gaps": [Gap(date(2025, 1, 2), date(2025, 1, 9))],
        "reference_outliers": [Outlier(date(2025, 1, 1), Decimal("40000.00"), Decimal("93000.12"))],
        "display": {
            "EUR": [DailyPrice(date(2025, 1, 1), "EUR", Decimal("90000.10"), "fx", f"{FX}*{REF}")],
            "GBP": [],
        },
        "gaps": {"USD": [Gap(date(2025, 1, 3), date(2025, 1, 9))], "EUR": [], "GBP": []},
        "outliers": {
            "USD": [],
            "EUR": [Outlier(date(2025, 1, 1), Decimal("1.00"), Decimal("90000.10"))],
            "GBP": [],
        },
        "hashes": {REF: H, "bitstamp:ohlc:btcusd": H, FX: H},
        "usd_through": date(2025, 1, 1),
        "usd_stale": True,
    }
    return Refreshed(**(base | kw))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def cache(
    conn: sqlite3.Connection, refreshed: Refreshed | None, seen: list[date] | None = None
) -> PriceCache:
    def fake(proxy: object, cancelled: threading.Event, today: Any) -> Refreshed | None:
        if seen is not None:
            seen.append(today())
        return refreshed

    return PriceCache(conn, now=lambda: NOW, refresh=fake)


def test_a_refresh_stores_every_series_with_its_hashes_and_flags_t303(conn: sqlite3.Connection) -> None:
    seen: list[date] = []
    batch = cache(conn, result(), seen).refresh(None, threading.Event())
    assert seen == [date(2025, 1, 10)]  # the refresh's complete days end before the batch's UTC date
    assert [(p.day.isoformat(), p.price, p.method) for p in store.prices(conn, "USD")] == [
        ("2025-01-01", Decimal("93000.12"), "reference"),
        ("2025-01-02", Decimal("94000.00"), "typical"),
    ]
    assert [p.source for p in store.prices(conn, "EUR")] == [f"{FX}*{REF}"]
    b = store.batch(conn, batch or 0)
    assert b is not None and b.hashes == {REF: H, "bitstamp:ohlc:btcusd": H, FX: H}
    assert (b.usd_through, b.usd_stale) == (date(2025, 1, 1), True)
    assert b.flags["mismatches"] == [{"day": "2025-01-01", "reference": "93000.12", "typical": "80000.00"}]
    assert b.flags["reference_gaps"] == [{"first": "2025-01-02", "last": "2025-01-09"}]
    assert b.flags["reference_outliers"] == [
        {"day": "2025-01-01", "previous": "40000.00", "price": "93000.12"}
    ]
    assert b.flags["gaps"] == {"USD": [{"first": "2025-01-03", "last": "2025-01-09"}], "EUR": [], "GBP": []}
    assert b.flags["outliers"]["EUR"] == [{"day": "2025-01-01", "previous": "1.00", "price": "90000.10"}]


def test_a_cancelled_refresh_stores_nothing(conn: sqlite3.Connection) -> None:
    assert cache(conn, None).refresh(None, threading.Event()) is None
    cancelled = threading.Event()
    cancelled.set()
    assert cache(conn, result()).refresh(None, cancelled) is None
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (0,)


def test_a_refresh_storage_refuses_is_a_fixed_refusal_t304(conn: sqlite3.Connection) -> None:
    with pytest.raises(ImportRefused) as refused:
        cache(conn, result(hashes={REF: H})).refresh(None, threading.Event())  # a source left unhashed
    assert "SHA-256" in str(refused.value)
    assert conn.execute("SELECT count(*) FROM price").fetchone() == (0,)


CSV = b"date,currency,price\n2024-01-02,USD,44950.12\n2024-01-02,EUR,41000.00\n"


def test_an_imported_csv_is_stored_whole_with_its_hash(conn: sqlite3.Connection) -> None:
    batch = cache(conn, None).import_csv(CSV)
    assert [
        (p.currency, p.price, p.method) for p in store.prices(conn, "USD") + store.prices(conn, "EUR")
    ] == [
        ("USD", Decimal("44950.12"), "import"),
        ("EUR", Decimal("41000.00"), "import"),
    ]
    b = store.batch(conn, batch)
    assert b is not None and b.hashes == {"upload": content_hash(CSV)}


@pytest.mark.parametrize(
    "data",
    [
        b"date,currency,price\n2024-01-02,USD,0.00\n",  # no price
        b"date,currency,price\n2025-01-10,USD,1.00\n",  # the day isn't over
        b"day,currency,price\n2024-01-02,USD,1.00\n",  # not the documented header
    ],
)
def test_a_bad_csv_is_refused_whole_without_echoing_it_t304(conn: sqlite3.Connection, data: bytes) -> None:
    with pytest.raises(ImportRefused) as refused:
        cache(conn, None).import_csv(data)
    assert str(refused.value) == "the price file can't be used: check it against the documented format"
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (0,)


def test_a_failing_disk_is_a_fixed_refusal(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_disk(*args: object) -> object:
        raise sqlite3.OperationalError("disk I/O error")  # as SQLite raises mid-write on a failing disk

    monkeypatch.setattr(store, "_row", failing_disk)
    with pytest.raises(ImportRefused) as refused:
        cache(conn, result()).refresh(None, threading.Event())
    assert str(refused.value) == "the user DB can't be used right now; see the log"
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (0,)


EAST = datetime(2025, 1, 10, 20, tzinfo=timezone(timedelta(hours=-5)))  # 2025-01-11 01:00 UTC


def test_the_complete_days_end_at_the_moments_utc_date(conn: sqlite3.Connection) -> None:
    seen: list[date] = []

    def fake(proxy: object, cancelled: threading.Event, today: Any) -> None:
        seen.append(today())

    PriceCache(conn, now=lambda: EAST, refresh=fake).refresh(None, threading.Event())
    assert seen == [date(2025, 1, 11)]
    imports = PriceCache(conn, now=lambda: EAST)
    imports.import_csv(b"date,currency,price\n2025-01-10,USD,1.00\n")  # over in UTC
    with pytest.raises(ImportRefused):
        imports.import_csv(b"date,currency,price\n2025-01-11,USD,1.00\n")  # not over yet


def test_a_moment_without_a_zone_is_refused_before_any_download(conn: sqlite3.Connection) -> None:
    calls: list[object] = []

    def counting(*args: object) -> None:
        calls.append(args)

    naive = PriceCache(conn, now=lambda: datetime(2025, 1, 10, 12), refresh=counting)
    with pytest.raises(ImportRefused):
        naive.refresh(None, threading.Event())
    with pytest.raises(ImportRefused):
        naive.import_csv(CSV)
    assert calls == []


def test_a_failed_download_stores_nothing_and_reaches_the_caller_t304(conn: sqlite3.Connection) -> None:
    def failing(*args: object) -> None:
        raise PriceError("a source came back damaged")

    with pytest.raises(PriceError):
        PriceCache(conn, now=lambda: NOW, refresh=failing).refresh(None, threading.Event())
    assert conn.execute(
        "SELECT (SELECT count(*) FROM price), (SELECT count(*) FROM price_batch)"
    ).fetchone() == (0, 0)


def test_a_busy_db_is_busy(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*args: object, **kw: object) -> None:
        raise DbBusy("the user DB is busy; try again in a moment")

    monkeypatch.setattr(store, "store_refresh", busy)
    with pytest.raises(Busy):
        cache(conn, result()).refresh(None, threading.Event())


def test_a_cancel_while_storing_rolls_the_batch_back(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = threading.Event()
    real = store._revisions

    def cancel_mid_write(*args: Any) -> Any:
        found = real(*args)
        cancelled.set()  # the user cancels while the batch is being written, before the commit
        return found

    monkeypatch.setattr(store, "_revisions", cancel_mid_write)
    assert cache(conn, result()).refresh(None, cancelled) is None
    assert conn.execute(
        "SELECT (SELECT count(*) FROM price), (SELECT count(*) FROM price_batch)"
    ).fetchone() == (0, 0)

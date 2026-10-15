"""The price cache (PLAN §6, M5; T-303, T-304): a refresh's result or the user's CSV, stored. A fake
refresh stands in for the downloads, so nothing touches the network. Synthetic prices only."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.prices import DailyPrice, Gap, Mismatch, content_hash
from coinacct.services.imports import ImportRefused
from coinacct.services.price_cache import PriceCache
from coinacct.services.prices import Refreshed
from coinacct.storage import prices as store
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db

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
        "reference_outliers": [],
        "display": {
            "EUR": [DailyPrice(date(2025, 1, 1), "EUR", Decimal("90000.10"), "fx", f"{FX}*{REF}")],
            "GBP": [],
        },
        "gaps": {"USD": [], "EUR": [], "GBP": []},
        "outliers": {"USD": [], "EUR": [], "GBP": []},
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
    assert "2024" not in str(refused.value) and "USD" not in str(refused.value)
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (0,)

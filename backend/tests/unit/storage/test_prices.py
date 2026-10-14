"""Daily prices in the user DB (PLAN §2, §6; ADR 0039; T-303, T-304, T-408). Synthetic prices only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.storage import accounts as ac
from coinacct.storage import events as ev
from coinacct.storage import prices as pr
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from coinacct.storage.migrations import STEPS

AT = datetime(2026, 10, 14, 12, tzinfo=UTC)
D = Decimal
HASH = "ab" * 32


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def p(
    day: str, price: str, currency: str = "USD", method: Any = "reference", source: str = "coinmetrics"
) -> pr.Price:
    return pr.Price(date.fromisoformat(day), currency, D(price), method, source)


def refresh(conn: sqlite3.Connection, *rows: pr.Price, **kw: Any) -> int:
    args: dict[str, Any] = {
        "hashes": {"coinmetrics": HASH},
        "usd_through": date(2025, 1, 2),
        "usd_stale": False,
    }
    args |= {"flags": {"mismatches": []}, "at": AT} | kw
    return pr.store_refresh(conn, rows, **args)


def test_a_refresh_is_stored_with_its_batch_and_read_back(conn: sqlite3.Connection) -> None:
    batch = refresh(
        conn,
        p("2025-01-01", "93000.12"),
        p("2025-01-02", "94000.00"),
        p("2025-01-01", "90000.10", "EUR", "fx", "ecb"),
    )
    assert pr.prices(conn, "USD") == [p("2025-01-01", "93000.12"), p("2025-01-02", "94000.00")]
    assert pr.price_on(conn, date(2025, 1, 1), "EUR") == p("2025-01-01", "90000.10", "EUR", "fx", "ecb")
    assert pr.price_on(conn, date(2025, 1, 3), "USD") is None
    last = pr.last_refresh(conn)
    assert last == pr.Batch(
        batch, AT, "refresh", date(2025, 1, 2), False, {"coinmetrics": HASH}, {"mismatches": []}
    )


def test_a_later_refresh_replaces_fetched_prices_but_never_imported_ones(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94000.00"))
    pr.store_import(
        conn, [p("2025-01-02", "1.00", method="import", source="csv-import")], file_hash=HASH, at=AT
    )
    second = refresh(conn, p("2025-01-01", "93000.99"), p("2025-01-02", "94000.50"), usd_stale=True)
    assert pr.prices(conn, "USD") == [
        p("2025-01-01", "93000.99"),  # the fetched price, replaced
        p("2025-01-02", "1.00", method="import", source="csv-import"),  # the user's own price stands
    ]
    last = pr.last_refresh(conn)
    assert last is not None and (last.id, last.usd_stale) == (second, True)


def test_an_import_replaces_whatever_the_day_had(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    pr.store_import(
        conn, [p("2025-01-01", "92000.00", method="import", source="csv-import")], file_hash=HASH, at=AT
    )
    assert pr.prices(conn, "USD") == [p("2025-01-01", "92000.00", method="import", source="csv-import")]
    assert pr.last_refresh(conn) is not None and pr.last_refresh(conn).kind == "refresh"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "bad",
    [
        p("2025-01-02", "0.00"),
        p("2025-01-02", "-1.00"),
        p("2025-01-02", "1.001"),
        p("2025-01-02", "1E+3"),
        p("2025-01-02", "1000000000000000.00"),  # 16 integer digits
        p("2008-12-31", "1.00"),  # before Bitcoin's first block
        p("2025-01-02", "1.00", currency="JPY"),
        p("2025-01-02", "1.00", method="guess"),
        p("2025-01-02", "1.00", source=""),
        p("2025-01-02", "1.00", method="import"),  # a refresh never stores an imported price
        p("2025-01-02", "1.00", method="fx", source="ecb"),  # a USD price is never a conversion (ADR 0039)
        p("2025-01-01", "2.00"),  # the same day twice in one batch
    ],
)
def test_a_refresh_with_any_bad_price_stores_nothing_t304(conn: sqlite3.Connection, bad: pr.Price) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "1.00"), bad)
    assert pr.prices(conn, "USD") == [p("2025-01-01", "93000.12")]  # the cache stays as it was
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (1,)


@pytest.mark.parametrize(
    "kw",
    [
        {"hashes": {"coinmetrics": "not-a-hash"}},
        {"usd_stale": 1},
        {"usd_through": "2025-01-02"},
        {"flags": {"x": float("nan")}},
        {"at": datetime(2026, 10, 14, 12)},  # no zone
        {"flags": {"error": ValueError("boom")}},  # not plain data: never stored as its text
        {"usd_through": date(2008, 1, 1)},
        {"usd_through": datetime(2025, 1, 2, tzinfo=UTC)},  # a moment, not a day
        {"hashes": {}},  # a refresh records its sources' hashes
        {"usd_through": None},  # a reference rate with no priced day, not marked stale
    ],
)
def test_a_refresh_with_a_bad_record_stores_nothing(conn: sqlite3.Connection, kw: dict[str, Any]) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12"), **kw)
    assert conn.execute("SELECT count(*) FROM price").fetchone() == (0,)


def test_an_import_stores_only_the_users_own_prices(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        pr.store_import(conn, [p("2025-01-01", "1.00")], file_hash=HASH, at=AT)
    with pytest.raises(pr.PriceStoreError):
        pr.store_import(conn, [], file_hash=HASH, at=AT)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '1.5', 'reference', 's', 1)",
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '01.50', 'reference', 's', 1)",
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '1.2.34', 'reference', 's', 1)",
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '0.00', 'reference', 's', 1)",
        "INSERT INTO price VALUES ('2025-01-01', 'JPY', '1.50', 'reference', 's', 1)",
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '1.50', 'guess', 's', 1)",
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '1.50', 'reference', 's', 99)",  # no such batch
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '1.50', 'fx', 's', 1)",  # USD is never converted
        # an import's price in a refresh's batch
        "INSERT INTO price VALUES ('2025-01-01', 'USD', '1.50', 'import', 's', 1)",
        "UPDATE price SET method = 'import'",  # a fetched price passed off as the user's
        # a reference rate with no priced day, not marked stale
        "INSERT INTO price_batch (at, kind, usd_stale, hashes, flags)"
        " VALUES ('2026-10-14T12:00:00+00:00', 'refresh', 0, '{\"s\": \"x\"}', '{}')",
        "INSERT INTO price_batch (at, kind, usd_stale, hashes, flags)"
        " VALUES ('2026-10-14T12:00:00+00:00', 'import', 0, '{}', '{}')",  # an import records its file
        "INSERT INTO price_batch (at, kind, usd_stale, hashes, flags)"
        " VALUES ('2026-10-14T12:00:00+00:00', 'import', 1, '{}', '{}')",  # only a refresh is stale
    ],
)
def test_the_schema_refuses_what_the_module_would(conn: sqlite3.Connection, sql: str) -> None:
    refresh(conn, p("2025-01-02", "1.00"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE price SET source = ''",
        "UPDATE price SET day = '2025-13-45'",
        "UPDATE price SET day = '2008-01-01'",
        "UPDATE price_batch SET at = 'not a moment at all!'",
        'UPDATE price_batch SET hashes = \'{"coinmetrics": "x"}\'',
        "UPDATE price_batch SET at = '2026-10-14T17:00:00+05:00'",  # not as the module writes it
        "UPDATE price_batch SET usd_through = '2008-01-01'",
        "UPDATE price SET method = 'fx'",  # a USD price converted
    ],
)
def test_a_damaged_row_is_refused_when_read_t408(conn: sqlite3.Connection, damage: str) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    conn.execute("PRAGMA ignore_check_constraints = ON")  # damage that bypasses the module and the schema
    conn.execute(damage)
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    if damage.startswith("UPDATE price_batch"):
        with pytest.raises(pr.PriceStoreError, match="damaged"):
            pr.last_refresh(conn)
        return
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")
    if "day =" not in damage:  # a moved day is no longer found by its old one
        with pytest.raises(pr.PriceStoreError, match="damaged"):
            pr.price_on(conn, date(2025, 1, 1), "USD")


def test_an_import_records_its_files_hash_t303(conn: sqlite3.Connection) -> None:
    pr.store_import(
        conn, [p("2025-01-01", "1.00", method="import", source="csv-import")], file_hash=HASH, at=AT
    )
    assert conn.execute("SELECT kind, hashes FROM price_batch").fetchall() == [
        ("import", f'{{"upload": "{HASH}"}}')
    ]
    with pytest.raises(pr.PriceStoreError):
        pr.store_import(
            conn, [p("2025-01-02", "1.00", method="import", source="csv-import")], file_hash="x", at=AT
        )


def test_an_empty_refresh_is_refused_t304(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn)
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (0,)


def test_clearing_an_import_lets_the_next_refresh_fill_the_day_again(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94000.00"))
    pr.store_import(
        conn, [p("2025-01-02", "1.00", method="import", source="csv-import")], file_hash=HASH, at=AT
    )
    pr.clear_import(conn, [date(2025, 1, 1), date(2025, 1, 2)], "USD", at=AT)
    assert pr.prices(conn, "USD") == [p("2025-01-01", "93000.12")]  # a fetched day is left as it was
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94000.50"))
    assert pr.price_on(conn, date(2025, 1, 2), "USD") == p("2025-01-02", "94000.50")
    kinds = [k for (k,) in conn.execute("SELECT kind FROM price_batch ORDER BY id")]
    assert kinds == ["refresh", "import", "clear", "refresh"]
    with pytest.raises(pr.PriceStoreError):
        pr.clear_import(conn, [], "USD", at=AT)


def test_the_fetch_start_never_depends_on_imported_prices_t301(conn: sqlite3.Connection) -> None:
    assert pr.fetched_through(conn) is None
    refresh(conn, p("2025-01-01", "93000.12"))
    pr.store_import(
        conn, [p("2025-06-01", "1.00", method="import", source="csv-import")], file_hash=HASH, at=AT
    )
    assert pr.fetched_through(conn) == date(2025, 1, 1)


def test_a_batchs_time_is_stored_in_utc(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"), at=datetime.fromisoformat("2026-10-14T17:00:00+05:00"))
    last = pr.last_refresh(conn)
    assert last is not None and last.at.isoformat() == "2026-10-14T12:00:00+00:00"


def test_the_upgrade_from_schema_12_keeps_every_row_adr0038(tmp_path: Path) -> None:
    d = tmp_path / "old"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:12]))
    tables = ("tax_account", "event", "identification", "change_log", "setting")
    try:
        record_chain(old, "regtest")
        wallet = ac.add_tax_account(old, "Wallet", "self_custody")
        sale = ev.add_event(
            old, ev.Event("sell", wallet, datetime(2025, 1, 2, tzinfo=UTC), 10, D("1.00")), at=AT.isoformat()
        )
        ev.identify(old, sale, (ev.Choice("e1", 10),), datetime(2025, 1, 2, tzinfo=UTC), at=AT.isoformat())
        old.execute("INSERT INTO setting VALUES ('time_zone', 'Europe/Berlin')")
        before = [old.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables]  # noqa: S608 - constant names
    finally:
        old.close()
    conn = open_db(data)
    try:
        assert [conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables] == before  # noqa: S608
        assert conn.execute("PRAGMA user_version").fetchone() == (len(STEPS),) == (13,)
        refresh(conn, p("2025-01-01", "93000.12"))
        pr.store_import(
            conn, [p("2025-01-02", "1.00", method="import", source="csv-import")], file_hash=HASH, at=AT
        )
        assert len(pr.prices(conn, "USD")) == 2
    finally:
        conn.close()


def test_a_price_read_back_must_match_the_kind_of_batch_that_wrote_it_t408(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    pr.store_import(
        conn, [p("2025-01-02", "1.00", method="import", source="csv-import")], file_hash=HASH, at=AT
    )
    conn.execute(
        "DROP TRIGGER price_keeps_its_kind_of_batch"
    )  # damage that bypasses the module and the schema
    conn.execute("UPDATE price SET batch_id = 2 WHERE day = '2025-01-01'")  # a fetched price in an import
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.price_on(conn, date(2025, 1, 1), "USD")


def test_a_refresh_with_no_reference_day_is_recorded_stale(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"), usd_through=None, usd_stale=True)
    last = pr.last_refresh(conn)
    assert last is not None and (last.usd_through, last.usd_stale) == (None, True)

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
from coinacct.storage.db import DbBusy, open_db, open_reader
from coinacct.storage.db import transaction as db_transaction
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
    day: str, price: str, currency: Any = "USD", method: Any = "reference", source: str = "coinmetrics"
) -> pr.Price:
    return pr.Price(date.fromisoformat(day), currency, D(price), method, source)


def bypass(conn: sqlite3.Connection) -> None:
    """Damage that bypasses the module and the schema: its CHECKs off, and its guarding triggers gone."""
    conn.execute("PRAGMA ignore_check_constraints = ON")
    for trigger in (
        "price_changes_with_its_batch",
        "price_batch_is_append_only_update",
        "price_batch_is_append_only_delete",
        "price_fetched_is_never_removed",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def review(**kw: Any) -> dict[str, Any]:
    """A refresh's review flags with every kind present, as the price cache records them."""
    empty: dict[str, list[Any]] = {"USD": [], "EUR": [], "GBP": []}
    return {
        "mismatches": [],
        "reference_gaps": [],
        "reference_outliers": [],
        "gaps": empty,
        "outliers": empty,
    } | kw


def refresh(conn: sqlite3.Connection, *rows: pr.Price, **kw: Any) -> int:
    reference = [r.day for r in rows if r.currency == "USD" and r.method == "reference"]
    args: dict[str, Any] = {
        "hashes": {"coinmetrics": HASH, "ecb": HASH, "bitstamp": HASH},
        "usd_stale": not reference,
    }
    args |= {"usd_through": max(reference) if reference else None, "flags": review(), "at": AT} | kw
    batch = pr.store_refresh(conn, rows, **args)
    assert batch is not None
    return batch


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
        batch,
        AT,
        "refresh",
        date(2025, 1, 2),
        False,
        {"coinmetrics": HASH, "ecb": HASH, "bitstamp": HASH},
        review(revised=[], kept_import=[], upgraded=[], display_updated=[]),
    )


def test_a_later_refresh_keeps_stored_days_and_records_revisions_t304(conn: sqlite3.Connection) -> None:
    first = refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94000.00"))
    imp = [p("2025-01-02", "1.00", method="import", source="csv-import")]
    pr.store_import(conn, imp, file_hash=HASH, at=AT)
    second = refresh(
        conn,
        p("2025-01-01", "93000.99"),
        p("2025-01-02", "94000.50"),
        p("2025-01-03", "95000.00"),
        usd_stale=True,
    )
    assert pr.prices(conn, "USD") == [
        p("2025-01-01", "93000.12"),  # the cache is authoritative once fetched
        p("2025-01-02", "1.00", method="import", source="csv-import"),  # the user's own price stands
        p("2025-01-03", "95000.00"),  # a new day is added
    ]
    last = pr.last_refresh(conn)
    assert last is not None and (last.id, last.usd_stale) == (second, True)
    assert last.flags["revised"] == [  # the source's new value for a stored day, kept for review
        {"day": "2025-01-01", "currency": "USD", "price": "93000.12", "method": "reference"}
        | {"source": "coinmetrics", "batch": first, "fetched": "93000.99", "fetched_method": "reference"}
        | {"fetched_source": "coinmetrics"}
    ]


def test_a_refresh_that_agrees_records_no_revision(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94000.00"))
    last = pr.last_refresh(conn)
    assert last is not None and all(last.flags[k] == [] for k in ("revised", "kept_import", "upgraded"))


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
        {"flags": review(mismatches=[{"x": float("nan")}])},
        {"at": datetime(2026, 10, 14, 12)},  # no zone
        {
            "flags": review(mismatches=[{"error": ValueError("boom")}])
        },  # not plain data: never stored as its text
        {"flags": {"mismatches": []}},  # a kind missing
        {"flags": review(extra=[])},  # a kind no refresh writes
        {"flags": review(gaps={"USD": []})},  # a currency missing
        {"flags": review(mismatches=[5])},  # an entry that isn't a record
        {"flags": review(mismatches=[{}])},  # a record without its fields
        {
            "flags": review(
                mismatches=[{"day": "2025-01-01", "reference": "1.00", "typical": "1.00", "x": "1"}]
            )
        },
        {"flags": review(reference_gaps=[{"first": "2025-01-02", "last": "2025-01-01"}])},  # out of order
        {"flags": review(reference_outliers=[{"day": "2025-01-01", "previous": "1", "price": "2.00"}])},
        {
            "flags": review(
                reference_gaps=[
                    {"first": "2025-01-01", "last": "2025-01-05"},
                    {"first": "2025-01-04", "last": "2025-01-06"},
                ]
            )
        },  # overlapping gaps
        {"flags": review(mismatches=[{"day": "2025-02-29", "reference": "1.00", "typical": "1.00"}])},
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
        "UPDATE price_batch SET usd_through = '20250102'",  # another ISO form, never the module's
        "UPDATE price_batch SET hashes = '{}'",  # a refresh without its hashes
        "UPDATE price SET day = '20250101'",
        "UPDATE price SET method = 'fx'",  # a USD price converted
    ],
)
def test_a_damaged_row_is_refused_when_read_t408(conn: sqlite3.Connection, damage: str) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    bypass(conn)  # damage that bypasses the module and the schema
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
    refresh(conn, p("2025-01-01", "93000.12"), usd_through=date(2025, 1, 1))
    for day in ("2025-01-01", "2025-06-01"):  # over the last fetched day, and after it
        imp = [p(day, "1.00", method="import", source="csv-import")]
        pr.store_import(conn, imp, file_hash=HASH, at=AT)
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
    refresh(
        conn,
        p("2025-01-01", "93000.12", method="typical", source="bitstamp"),
        usd_through=None,
        usd_stale=True,
    )
    last = pr.last_refresh(conn)
    assert last is not None and (last.usd_through, last.usd_stale) == (None, True)


def test_an_import_and_a_clear_record_what_they_replaced_and_removed(conn: sqlite3.Connection) -> None:
    fetched = refresh(conn, p("2025-01-01", "93000.12"))
    first = pr.store_import(
        conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT
    )
    second = pr.store_import(
        conn, [p("2025-01-01", "2.00", method="import", source="csv")], file_hash=HASH, at=AT
    )
    cleared = pr.clear_import(conn, [date(2025, 1, 1)], "USD", at=AT)
    row = {"day": "2025-01-01", "currency": "USD"}
    first_b, second_b, cleared_b = (pr.batch(conn, b) for b in (first, second, cleared))
    assert first_b is not None and first_b.flags == {
        "replaced": [
            row | {"price": "93000.12", "method": "reference", "source": "coinmetrics", "batch": fetched}
        ]
    }
    assert second_b is not None and second_b.flags == {
        "replaced": [row | {"price": "1.00", "method": "import", "source": "csv", "batch": first}]
    }
    assert cleared_b is not None and cleared_b.flags == {
        "currency": "USD",
        "removed": [row | {"price": "2.00", "method": "import", "source": "csv", "batch": second}],
    }


def test_each_price_names_the_batch_that_wrote_it_t303(conn: sqlite3.Connection) -> None:
    fetched = refresh(conn, p("2025-01-01", "93000.12"))
    imported = pr.store_import(
        conn, [p("2025-01-02", "1.00", method="import", source="csv")], file_hash=HASH, at=AT
    )
    assert [x.batch for x in pr.prices(conn, "USD")] == [fetched, imported]
    stored = pr.price_on(conn, date(2025, 1, 2), "USD")
    assert stored is not None and stored.batch == imported
    b = pr.batch(conn, imported)
    assert b is not None and b.hashes == {"upload": HASH}
    assert pr.batch(conn, 999) is None


def test_clearing_one_currency_leaves_the_others(conn: sqlite3.Connection) -> None:
    usd = p("2025-01-01", "1.00", method="import", source="csv")
    eur = p("2025-01-01", "2.00", "EUR", "import", "csv")
    pr.store_import(conn, [usd, eur], file_hash=HASH, at=AT)
    pr.clear_import(conn, [date(2025, 1, 1)], "EUR", at=AT)
    assert pr.prices(conn, "USD") == [usd]
    assert pr.prices(conn, "EUR") == []


@pytest.mark.parametrize(
    "days", [[datetime(2025, 1, 1, tzinfo=UTC)], [date(2025, 1, 1), "2025-01-02"], "2025-01-01"]
)
def test_clearing_refuses_anything_but_days(conn: sqlite3.Connection, days: Any) -> None:
    with pytest.raises(pr.PriceStoreError):
        pr.clear_import(conn, days, "USD", at=AT)
    with pytest.raises(pr.PriceStoreError):
        pr.clear_import(conn, [date(2025, 1, 1)], "JPY", at=AT)


def test_a_price_is_asked_for_by_a_day_not_a_moment(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        pr.price_on(conn, datetime(2025, 1, 1, tzinfo=UTC), "USD")


def test_no_price_comes_from_a_clear_batch(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    pr.store_import(conn, [p("2025-01-05", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    cleared = pr.clear_import(conn, [date(2025, 1, 5)], "USD", at=AT)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price VALUES ('2025-01-02', 'USD', '1.50', 'reference', 's', ?)", (cleared,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE price SET batch_id = ?", (cleared,))
    conn.execute(
        "DROP TRIGGER price_keeps_its_kind_of_batch"
    )  # damage that bypasses the module and the schema
    conn.execute("UPDATE price SET batch_id = ?", (cleared,))
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")


def test_an_empty_hash_object_is_refused_however_spelled(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price_batch (at, kind, usd_through, usd_stale, hashes, flags)"
            " VALUES ('2026-10-14T12:00:00+00:00', 'refresh', '2025-01-01', 0, '{ }', '{}')"
        )


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE price_batch SET at = 'not a moment at all!'",
        'UPDATE price_batch SET hashes = \'{"coinmetrics": "x"}\'',
    ],
)
def test_a_price_whose_batch_is_damaged_is_refused_t408(conn: sqlite3.Connection, damage: str) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    bypass(conn)  # damage that bypasses the module and the schema
    conn.execute(damage)
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.price_on(conn, date(2025, 1, 1), "USD")


def test_a_price_whose_batch_is_gone_is_refused_not_hidden_t408(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    bypass(conn)  # damage that bypasses the module and the schema
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM price_batch")
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")


def test_a_fetched_price_names_a_hashed_source_t303(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12", source="unhashed"))
    refresh(conn, p("2025-01-01", "90000.10", "EUR", "fx", "ecb*coinmetrics"))  # a conversion's parts
    assert len(pr.prices(conn, "EUR")) == 1


def test_a_reference_day_after_the_refresh_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12"), usd_through=date(2026, 10, 15))


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE price SET price = '1E+999999999'",  # refused before it is parsed, never formatted
        "UPDATE price SET source = 'unhashed'",
    ],
)
def test_a_damaged_price_is_refused_before_it_costs_anything_t408(
    conn: sqlite3.Connection, damage: str
) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    bypass(conn)  # damage that bypasses the module and the schema
    conn.execute(damage)
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")


@pytest.mark.parametrize("through", ["20250102", "2026-10-15"])
def test_a_damaged_fetch_start_is_refused_t301(conn: sqlite3.Connection, through: str) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    bypass(conn)
    conn.execute("UPDATE price_batch SET usd_through = ?", (through,))
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.fetched_through(conn)


def test_each_batch_is_checked_once_per_read(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh(conn, *(p(f"2025-01-{d:02}", "93000.12") for d in range(1, 21)))
    calls: list[object] = []
    real = pr._stored_batch

    def counting(row: Any) -> pr.Batch:
        calls.append(row)
        return real(row)

    monkeypatch.setattr(pr, "_stored_batch", counting)
    assert len(pr.prices(conn, "USD")) == 20
    assert len(calls) == 1


def test_a_clear_that_matches_nothing_is_refused(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    with pytest.raises(pr.PriceStoreError):
        pr.clear_import(conn, [date(2025, 1, 1)], "USD", at=AT)
    assert conn.execute("SELECT count(*) FROM price_batch").fetchone() == (1,)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE price_batch SET usd_stale = 1 WHERE kind = 'import'",
        'UPDATE price_batch SET hashes = \'{"other": "' + "ab" * 32 + "\"}' WHERE kind = 'import'",
    ],
)
def test_an_import_batch_is_held_to_its_own_rules_on_read_t408(conn: sqlite3.Connection, damage: str) -> None:
    pr.store_import(conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    bypass(conn)
    conn.execute(damage)
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")


def test_a_price_changes_only_with_a_new_batch_and_batches_stay(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE price SET price = '1.00'")  # the same batch, a new value
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE price_batch SET usd_stale = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM price_batch")


def test_the_reference_day_is_the_last_reference_price_stored(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12"), usd_through=date(2025, 1, 5))  # past the cached days
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12"), usd_through=None, usd_stale=True)


def test_a_damaged_price_is_refused_not_replaced_or_cleared_t408(conn: sqlite3.Connection) -> None:
    pr.store_import(conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    bypass(conn)
    conn.execute("UPDATE price SET source = ''")
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.store_import(conn, [p("2025-01-01", "2.00", method="import", source="csv")], file_hash=HASH, at=AT)
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.clear_import(conn, [date(2025, 1, 1)], "USD", at=AT)


def test_a_source_that_disagrees_with_an_import_is_recorded(conn: sqlite3.Connection) -> None:
    pr.store_import(conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    refresh(conn, p("2025-01-01", "93000.12"))
    last = pr.last_refresh(conn)
    assert last is not None and [(r["price"], r["fetched"]) for r in last.flags["kept_import"]] == [
        ("1.00", "93000.12")
    ]
    assert pr.prices(conn, "USD")[0].price == D("1.00")  # the user's own price stands


def test_a_day_refilled_after_a_clear_is_compared_with_its_last_fetch_t303(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    pr.store_import(conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    pr.clear_import(conn, [date(2025, 1, 1)], "USD", at=AT)
    refresh(conn, p("2025-01-01", "99999.99"))  # the source now serves another value
    last = pr.last_refresh(conn)
    assert last is not None and [(r["price"], r["fetched"]) for r in last.flags["revised"]] == [
        ("93000.12", "99999.99")
    ]


@pytest.mark.parametrize("flags", [{"revised": []}, {"kept_import": []}])
def test_a_caller_cant_write_the_modules_own_record(conn: sqlite3.Connection, flags: dict[str, Any]) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12"), flags=flags)


def test_a_source_name_has_no_star(conn: sqlite3.Connection) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12"), hashes={"coinmetrics": HASH, "ecb*x": HASH})


def test_a_fetched_price_is_never_removed_and_never_moves_back(conn: sqlite3.Connection) -> None:
    first = refresh(conn, p("2025-01-01", "93000.12"))
    refresh(conn, p("2025-01-02", "94000.00"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM price WHERE method != 'import'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE price SET batch_id = ?, price = '1.00' WHERE day = '2025-01-02'", (first,))


def test_bitstamps_fill_gives_way_to_the_reference_rate_adr0039(conn: sqlite3.Connection) -> None:
    """Coin Metrics publishes a day late: a refresh fills it with Bitstamp's typical price, and the next
    refresh, which has the reference rate for it, replaces the fill and records that it did."""
    refresh(
        conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94100.00", method="typical", source="bitstamp")
    )
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-02", "94000.00"))
    assert pr.price_on(conn, date(2025, 1, 2), "USD") == p("2025-01-02", "94000.00")
    last = pr.last_refresh(conn)
    assert last is not None and [(r["price"], r["method"], r["fetched"]) for r in last.flags["upgraded"]] == [
        ("94100.00", "typical", "94000.00")
    ]
    assert last.flags["revised"] == []
    refresh(conn, p("2025-01-02", "94200.00", method="typical", source="bitstamp"))  # never back to the fill
    assert pr.price_on(conn, date(2025, 1, 2), "USD") == p("2025-01-02", "94000.00")


def test_a_display_price_is_updated_and_recorded(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-01", "90000.10", "EUR", "fx", "ecb*coinmetrics"))
    refresh(conn, p("2025-01-01", "93000.12"), p("2025-01-01", "90000.55", "EUR", "fx", "ecb*coinmetrics"))
    assert [x.price for x in pr.prices(conn, "EUR")] == [D("90000.55")]
    last = pr.last_refresh(conn)
    assert last is not None and [r["fetched"] for r in last.flags["display_updated"]] == ["90000.55"]


@pytest.mark.parametrize(
    ("method", "source"),
    [("reference", "bitstamp"), ("typical", "coinmetrics"), ("reference", "ecb*coinmetrics")],
)
def test_a_fetched_price_comes_from_the_source_its_method_names_adr0039(
    conn: sqlite3.Connection, method: str, source: str
) -> None:
    with pytest.raises(pr.PriceStoreError):
        refresh(conn, p("2025-01-01", "93000.12", method=method, source=source))


@pytest.mark.parametrize(
    "flags",
    ["{}", '{"replaced": [{"day": "2025-01-01"}]}'],
)
def test_an_import_batch_keeps_its_record_on_read_t408(conn: sqlite3.Connection, flags: str) -> None:
    pr.store_import(conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    bypass(conn)
    conn.execute("UPDATE price_batch SET flags = ? WHERE kind = 'import'", (flags,))
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.prices(conn, "USD")


def test_an_imported_price_is_removed_only_by_a_recorded_clear(conn: sqlite3.Connection) -> None:
    pr.store_import(conn, [p("2025-01-01", "1.00", method="import", source="csv")], file_hash=HASH, at=AT)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM price")
    pr.clear_import(conn, [date(2025, 1, 1)], "USD", at=AT)
    assert pr.prices(conn, "USD") == []


def test_a_refresh_over_a_damaged_day_names_the_backup_remedy_t408(conn: sqlite3.Connection) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    bypass(conn)
    conn.execute("UPDATE price SET source = ''")
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="restore the user DB from a backup"):
        refresh(conn, p("2025-01-01", "93000.12"))


def test_before_commit_rolls_the_refresh_back(conn: sqlite3.Connection) -> None:
    hashes = {"coinmetrics": HASH}
    rows = [p("2025-01-01", "93000.12")]
    kw: dict[str, Any] = {"usd_through": date(2025, 1, 1), "usd_stale": False, "flags": review(), "at": AT}
    assert pr.store_refresh(conn, rows, hashes=hashes, before_commit=lambda: True, **kw) is None
    assert conn.execute(
        "SELECT (SELECT count(*) FROM price), (SELECT count(*) FROM price_batch)"
    ).fetchone() == (0, 0)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("SQLITE_FULL", "the user DB can't be used right now; see the log"),
        ("SQLITE_IOERR_FSYNC", "the user DB can't be used right now; see the log"),
        ("SQLITE_CORRUPT", "a stored price is damaged (T-408): restore the user DB from a backup"),
        ("SQLITE_NOTADB", "a stored price is damaged (T-408): restore the user DB from a backup"),
    ],
)
def test_a_failed_commit_is_a_fixed_refusal_with_only_its_code_logged(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: str,
    message: str,
) -> None:
    real = db_transaction

    class failing_commit:
        def __init__(self, c: sqlite3.Connection) -> None:
            self.inner = real(c)

        def __enter__(self) -> None:
            self.inner.__enter__()

        def __exit__(self, kind: Any, exc: Any, tb: Any) -> bool:
            self.inner.__exit__(ValueError, ValueError(), None)  # roll back what was written
            e = sqlite3.OperationalError("disk I/O error at /secret/path")  # as COMMIT fails
            e.sqlite_errorname = code
            raise e

    monkeypatch.setattr(pr, "transaction", failing_commit)
    with pytest.raises(pr.PriceStoreError) as refused:
        refresh(conn, p("2025-01-01", "93000.12"))
    assert str(refused.value) == message
    assert code in caplog.text and "/secret/path" not in caplog.text


def test_a_busy_commit_is_busy(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    real = db_transaction

    class busy_commit:
        def __init__(self, c: sqlite3.Connection) -> None:
            self.inner = real(c)

        def __enter__(self) -> None:
            self.inner.__enter__()

        def __exit__(self, kind: Any, exc: Any, tb: Any) -> bool:
            self.inner.__exit__(ValueError, ValueError(), None)
            e = sqlite3.OperationalError("database is locked")
            e.sqlite_errorname = "SQLITE_BUSY"
            raise e

    monkeypatch.setattr(pr, "transaction", busy_commit)
    with pytest.raises(DbBusy):
        refresh(conn, p("2025-01-01", "93000.12"))


def test_the_refresh_status_is_one_snapshot(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that commits between the status's two reads isn't half seen."""
    refresh(conn, p("2025-01-01", "93000.12"))
    reader = open_reader(open_data_dir(str(tmp_path / "data")))
    real = pr.last_refresh

    def then_a_refresh_commits(c: sqlite3.Connection) -> pr.Batch | None:
        last = real(c)
        refresh(conn, p("2025-01-02", "94000.00"), at=datetime(2026, 10, 15, 12, tzinfo=UTC))  # on the writer
        return last

    monkeypatch.setattr(pr, "last_refresh", then_a_refresh_commits)
    try:
        last, through = pr.refresh_status(reader)
    finally:
        reader.close()
    assert last is not None and (last.usd_through, through) == (date(2025, 1, 1), date(2025, 1, 1))


@pytest.mark.parametrize(
    "damage",
    [
        "json_remove(flags, '$.revised[0].fetched')",  # a revision without what the source now serves
        "json_set(flags, '$.revised[0].extra', 1)",  # a field the module never writes
        "json_set(flags, '$.revised[0].fetched', '0.00')",
    ],
)
def test_a_damaged_revision_record_is_refused_on_read_t408(conn: sqlite3.Connection, damage: str) -> None:
    refresh(conn, p("2025-01-01", "93000.12"))
    refresh(conn, p("2025-01-01", "93000.99"))  # the source serves another value: a revision
    bypass(conn)
    conn.execute(f"UPDATE price_batch SET flags = {damage} WHERE id = 2")  # noqa: S608 - a constant
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(pr.PriceStoreError, match="damaged"):
        pr.last_refresh(conn)

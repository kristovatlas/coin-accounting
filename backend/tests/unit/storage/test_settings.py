"""The user's settings in the user DB (PLAN §2; THREAT_MODEL T-503). Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coinacct.storage import accounts as ac
from coinacct.storage import events as ev
from coinacct.storage import settings
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from coinacct.storage.migrations import STEPS

AT = "2026-10-14T12:00:00+00:00"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


def test_the_time_zone_is_utc_until_set(conn: sqlite3.Connection) -> None:
    assert settings.time_zone_name(conn) == "UTC"
    assert settings.time_zone(conn) == ZoneInfo("UTC")


def test_setting_the_time_zone(conn: sqlite3.Connection) -> None:
    settings.set_time_zone(conn, "America/New_York")
    settings.set_time_zone(conn, "Europe/Berlin")
    assert settings.time_zone(conn) == ZoneInfo("Europe/Berlin")
    assert conn.execute("SELECT key, value FROM setting").fetchall() == [("time_zone", "Europe/Berlin")]


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Not/A_Zone",
        "../../etc/passwd",
        "/usr/share/zoneinfo/UTC",
        "America/New York",
        "a" * 65,
        5,
        "localtime",  # the machine's own zone: not the same setting everywhere
        "posixrules",
        "Factory",
        "right/UTC",  # the leap-second variant
        "posix/America/New_York",
        "america/new_york",  # a case-insensitive filesystem would load it
        "US/Eastern",  # a backward-compatibility alias, missing from some systems
        "Etc/GMT+5",  # sign-inverted: UTC-5
        "EST5EDT",
        "GMT",
    ],
)
def test_a_name_that_isnt_a_zone_is_refused_without_echoing_it(
    conn: sqlite3.Connection, name: object
) -> None:
    with pytest.raises(settings.SettingsError) as refused:
        settings.set_time_zone(conn, name)  # type: ignore[arg-type]
    if isinstance(name, str) and name:
        assert name not in str(refused.value)
    assert settings.time_zone_name(conn) == "UTC"


def test_the_schema_refuses_an_unknown_setting(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO setting (key, value) VALUES ('proxy', 'x')")


@pytest.mark.parametrize("stored", ["Not/A_Zone", "localtime", "x;y"])
def test_a_damaged_stored_zone_is_refused_when_read_t408(conn: sqlite3.Connection, stored: str) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES ('time_zone', ?)", (stored,))
    for read in (settings.time_zone_name, settings.time_zone):
        with pytest.raises(settings.SettingsError, match="set the time zone again") as refused:
            read(conn)
        assert stored not in str(refused.value)


@pytest.mark.parametrize("value", ["", "x" * 65])
def test_the_schema_bounds_a_settings_value(conn: sqlite3.Connection, value: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO setting (key, value) VALUES ('time_zone', ?)", (value,))


def test_the_upgrade_from_schema_11_keeps_every_row_adr0038(tmp_path: Path) -> None:
    d = tmp_path / "old"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:11]))
    try:
        record_chain(old, "regtest")
        wallet = ac.add_tax_account(old, "Wallet", "self_custody")
        bought = ev.add_event(
            old, ev.Event("buy", wallet, datetime(2025, 1, 2, tzinfo=UTC), 10, Decimal("1.00")), at=AT
        )
        before = [
            old.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608 - constant names
            for table in ("tax_account", "event", "change_log")
        ]
    finally:
        old.close()
    conn = open_db(data)
    try:
        after = [
            conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608 - constant names
            for table in ("tax_account", "event", "change_log")
        ]
        assert after == before
        assert conn.execute("PRAGMA user_version").fetchone() == (len(STEPS),)
        assert settings.time_zone_name(conn) == "UTC"
        settings.set_time_zone(conn, "Europe/Berlin")
        assert ev.events(conn)[0].id == bought
    finally:
        conn.close()


@pytest.mark.parametrize(
    "name", ["America/Argentina/Buenos_Aires", "America/Port-au-Prince", "Asia/Kolkata", "Pacific/Kiritimati"]
)
def test_canonical_zones_of_every_shape_are_accepted(conn: sqlite3.Connection, name: str) -> None:
    settings.set_time_zone(conn, name)
    assert settings.time_zone(conn) == ZoneInfo(name)


def test_every_accepted_name_fits_the_pattern_and_the_schema() -> None:
    zones = settings._zones()
    assert len(zones) > 100 and "UTC" in zones
    assert all(settings._ZONE_NAME.fullmatch(z) and len(z) <= 64 for z in zones)


def test_without_a_zone_table_only_utc_is_accepted(tmp_path: Path) -> None:
    assert settings._canonical((str(tmp_path),)) == frozenset()
    (tmp_path / "zone1970.tab").write_text("# a comment\nUS\t+404251-0740023\tAmerica/New_York\tEastern\n")
    assert settings._canonical((str(tmp_path),)) == {"America/New_York"}

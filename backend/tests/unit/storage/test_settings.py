"""The user's settings in the user DB (PLAN §2; THREAT_MODEL T-503). Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

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


def test_every_place_this_machine_has_fits_the_pattern_and_the_schema() -> None:
    places = [n for n in available_timezones() if n.split("/", 1)[0] in settings._AREAS]
    assert all(settings._ZONE_NAME.fullmatch(n) and len(n) <= 64 for n in places)


@pytest.mark.parametrize("name", ["Europe/Amsterdam", "Europe/Oslo", "America/St_Thomas"])
def test_zones_merged_into_links_upstream_are_still_accepted(conn: sqlite3.Connection, name: str) -> None:
    settings.set_time_zone(conn, name)  # tzdata 2022b made these links; they are still the users' places
    assert settings.time_zone(conn) == ZoneInfo(name)


@pytest.mark.parametrize("error", [ZoneInfoNotFoundError, ValueError, OSError])
def test_a_zone_that_wont_load_is_refused_with_a_fixed_message(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def broken(name: str) -> ZoneInfo:
        raise error("a damaged or missing file")

    monkeypatch.setattr(settings, "ZoneInfo", broken)
    with pytest.raises(settings.SettingsError, match="doesn't have that zone"):
        settings.set_time_zone(conn, "America/New_York")
    conn.execute("INSERT INTO setting (key, value) VALUES ('time_zone', 'America/New_York')")
    with pytest.raises(settings.SettingsError, match="set the time zone again"):
        settings.time_zone(conn)


def test_an_alias_is_refused_with_the_rule() -> None:
    with pytest.raises(settings.SettingsError, match="UTC or a place"):
        settings._zone("US/Eastern")

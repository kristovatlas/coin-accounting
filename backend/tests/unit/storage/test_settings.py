"""The user's settings in the user DB (PLAN §2; THREAT_MODEL T-503). Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coinacct.storage import settings
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db


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
    ["", "Not/A_Zone", "../../etc/passwd", "/usr/share/zoneinfo/UTC", "America/New York", "a" * 65, 5],
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

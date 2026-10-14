"""The user's settings in the user DB (PLAN §2; THREAT_MODEL T-503): read with their defaults, and
changed. A change isn't in the change log, whose kinds PLAN §2 lists; every report embeds the settings
it used instead (PLAN §8, T-503).

- **`time_zone`:** the IANA zone (such as `America/New_York`) an event's tax date is taken in (PLAN §7).
  Its default is `UTC` until the user sets one. A name must be a canonical zone, as the system's
  time-zone database lists them in `zone1970.tab` (IANA's list of zones, without the backward-
  compatibility aliases such as `US/Eastern` or the sign-inverted `Etc/GMT+5`), spelled as listed, or
  `UTC`; never `localtime` (the machine's own zone), so the setting means the same on every machine.
  A host whose database has no `zone1970.tab` accepts only `UTC`. It is read here, since the
  database is files (architecture §2); a restart picks up a database update. A stored name is
  checked again when read: a damaged one, or one this machine's database lacks, is refused with the
  remedy (T-408).
"""

from __future__ import annotations

import re
import sqlite3
from functools import cache
from pathlib import Path
from typing import Final
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones

from coinacct.storage.db import DbError, transaction

DEFAULT_TIME_ZONE: Final = "UTC"
# an IANA name: `UTC`, `Area/Location`, or `Area/Region/Location`; never a path
_ZONE_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,30}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,30}){0,2}")


class SettingsError(DbError):
    """A setting the app refuses, with a fixed message that never echoes the value."""


_DAMAGED: Final = "the stored time zone can't be used here (T-408): set the time zone again"


def _canonical(tzpath: tuple[str, ...]) -> frozenset[str]:
    """The zones the first `zone1970.tab` on `tzpath` lists (its third column), or none."""
    for directory in tzpath:
        table = Path(directory) / "zone1970.tab"
        if table.is_file():
            lines = table.read_text(encoding="ascii", errors="replace").splitlines()
            return frozenset(
                f[2]
                for line in lines
                if line and not line.startswith("#") and len(f := line.split("\t")) >= 3
            )
    return frozenset()


@cache
def _zones() -> frozenset[str]:
    """The canonical zones this machine's database both lists and has, and `UTC`."""
    return (_canonical(TZPATH) & available_timezones()) | {DEFAULT_TIME_ZONE}


def _zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not _ZONE_NAME.fullmatch(name):
        raise SettingsError("the time zone is an IANA name, such as America/New_York")
    if name not in _zones():
        raise SettingsError("the time zone isn't one the system's time-zone database lists")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # pragma: no cover - listed but unreadable
        raise SettingsError("the time zone isn't one the system's time-zone database has") from None


def time_zone_name(conn: sqlite3.Connection) -> str:
    """The stored time zone's name, checked as a write would check it."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'time_zone'").fetchone()
    if row is None:
        return DEFAULT_TIME_ZONE
    try:
        _zone(row[0])
    except SettingsError:
        raise SettingsError(_DAMAGED) from None
    return str(row[0])


def time_zone(conn: sqlite3.Connection) -> ZoneInfo:
    """The zone tax dates are taken in."""
    return _zone(time_zone_name(conn))


def set_time_zone(conn: sqlite3.Connection, name: str) -> None:
    """Set the time zone."""
    _zone(name)
    with transaction(conn):
        conn.execute(
            "INSERT INTO setting (key, value) VALUES ('time_zone', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (name,),
        )


__all__ = ["DEFAULT_TIME_ZONE", "SettingsError", "set_time_zone", "time_zone", "time_zone_name"]

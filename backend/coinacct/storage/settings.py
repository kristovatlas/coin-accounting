"""The user's settings in the user DB (PLAN §2; THREAT_MODEL T-503): read with their defaults, and
changed. A change isn't in the change log, whose kinds PLAN §2 lists; every report embeds the settings
it used instead (PLAN §8, T-503).

- **`time_zone`:** the IANA zone (such as `America/New_York`) an event's tax date is taken in (PLAN §7).
  Its default is `UTC` until the user sets one. A name must be a canonical zone of the system's
  time-zone database, spelled as it lists it: never `localtime` (the machine's own zone), `Factory` or
  a `posix/` or `right/` variant, so the setting means the same on every machine. It is read here,
  since the database is files (architecture §2). A stored name is checked again when read: a damaged
  one, or one this machine's database lacks, is refused with the remedy (T-408).
"""

from __future__ import annotations

import re
import sqlite3
from functools import cache
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from coinacct.storage.db import DbError, transaction

DEFAULT_TIME_ZONE: Final = "UTC"
# an IANA name: `UTC`, `Area/Location`, or `Area/Region/Location`; never a path
_ZONE_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,30}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,30}){0,2}")


class SettingsError(DbError):
    """A setting the app refuses, with a fixed message that never echoes the value."""


_NOT_ZONES: Final = frozenset({"localtime", "Factory", "posixrules"})
_DAMAGED: Final = "the stored time zone can't be used here (T-408): set the time zone again"


@cache
def _zones() -> frozenset[str]:
    """The canonical zones of the system's database (the standard library leaves out `posix/` and
    `right/`), without the names that aren't zones."""
    return frozenset(n for n in available_timezones() if n not in _NOT_ZONES) | {DEFAULT_TIME_ZONE}


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

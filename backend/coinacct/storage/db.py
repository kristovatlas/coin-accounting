"""The user DB: `<data>/db.sqlite` (architecture §6; THREAT_MODEL T-401, T-402, T-408).

- **Where:** only under the verified data directory, never through a link, as a regular file of
  mode 0600 owned by the user, on the verified device (like the config and the log file). SQLite
  creates its WAL and shared-memory files next to it, on the volume, with the database's mode.
- **Settings:** WAL journaling with `synchronous=FULL`, foreign keys on, `temp_store=MEMORY` so temp
  B-trees never touch disk (T-402), and `trusted_schema=OFF`.
- **Integrity:** `PRAGMA integrity_check` on every open; anything but `ok` refuses the DB (T-408).
- **Schema:** the numbered steps in `migrations/` (`mNNNN_name.py`, each an `SQL` string), applied in
  order, each in its own transaction, with `PRAGMA user_version` recording the last one. A DB from a
  newer app version is refused rather than guessed at.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from coinacct.storage.datadir import DataDir, DataDirError, require_device
from coinacct.storage.migrations import STEPS

DB_NAME: Final = "db.sqlite"
SIDE_FILES: Final = ("-wal", "-shm", "-journal")


class DbError(Exception):
    """The user DB can't be used. The message says why; it never contains user data."""


def migrations(steps: Sequence[tuple[int, str]] = STEPS) -> list[tuple[int, str]]:
    """The schema steps, `(version, sql)` in order. Versions must run 1, 2, 3 … with no gaps."""
    found = list(steps)
    if [v for v, _ in found] != list(range(1, len(found) + 1)):
        raise DbError("the migrations aren't numbered 1, 2, 3, … in order without gaps")
    if any(not isinstance(sql, str) or not sql.strip() for _, sql in found):
        raise DbError("a migration has no SQL")
    return found


def open_db(data_dir: DataDir, *, steps: list[tuple[int, str]] | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the user DB. The connection is in autocommit mode;
    callers group writes with `with transaction(conn):`."""
    path = data_dir.root / DB_NAME
    _create_private(path, data_dir)
    for suffix in SIDE_FILES:
        _check_side_file(Path(f"{path}{suffix}"), data_dir)
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=rw", uri=True, isolation_level=None, check_same_thread=False
        )
    except sqlite3.Error as e:
        raise DbError(f"can't open the user DB ({type(e).__name__})") from None
    try:
        for pragma in (
            "journal_mode=WAL",
            "synchronous=FULL",
            "foreign_keys=ON",
            "temp_store=MEMORY",
            "trusted_schema=OFF",
        ):
            conn.execute(f"PRAGMA {pragma}")
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise DbError("the user DB couldn't switch to WAL journaling")
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise DbError("the user DB failed its integrity check (T-408); restore it from a backup")
        migrate(conn, migrations() if steps is None else steps)
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DbError(f"the user DB can't be read ({type(e).__name__}); it may be damaged (T-408)") from None
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection, steps: list[tuple[int, str]]) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(steps):
        raise DbError("the user DB was written by a newer version of the app; update the app")
    for version, sql in steps[current:]:
        # One transaction per step, the version bump included, so a failed step leaves the DB
        # exactly at the previous version.
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;")


class transaction:
    """`BEGIN IMMEDIATE` … `COMMIT`, or `ROLLBACK` on any exception."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, kind: object, value: object, tb: object) -> None:
        self.conn.execute("ROLLBACK" if kind is not None else "COMMIT")


def _create_private(path: Path, data_dir: DataDir) -> None:
    """Create the DB file if missing, mode 0600, never through a link, and check what's there."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENXIO):
            raise DbError(f"{path} must be a regular file, not a link or a pipe (T-401)") from None
        raise DbError(f"can't open {path} ({e.strerror})") from None
    try:
        _check_fd(fd, path, data_dir)
    finally:
        os.close(fd)


def _check_side_file(path: Path, data_dir: DataDir) -> None:
    """A WAL, shared-memory or journal file, if present, must be like the DB: SQLite would follow a
    link there and write the DB's pages to wherever it points."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise DbError(f"{path} must be a regular file, not a link (T-401, T-402)")
    _check_stat(st, path, data_dir)


def _check_fd(fd: int, path: Path, data_dir: DataDir) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise DbError(f"{path} must be a regular file (T-401)")
    _check_stat(st, path, data_dir)


def _check_stat(st: os.stat_result, path: Path, data_dir: DataDir) -> None:
    try:
        require_device(path, st, data_dir.device)
    except DataDirError as e:
        raise DbError(str(e)) from None
    if st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise DbError(f"{path} must be the user's own file with mode 600 (T-401)")

"""The user DB: `<data>/db.sqlite` (architecture §6; THREAT_MODEL T-401, T-402, T-408).

- **Where:** only under the verified data directory, never through a link, as a regular file of
  mode 0600 owned by the user, on the verified device (like the config and the log file). SQLite
  creates its WAL and shared-memory files next to it, on the volume, with the database's mode.
- **Settings:** WAL journaling with `synchronous=FULL`, foreign keys on, `temp_store=MEMORY` so temp
  B-trees never touch disk (T-402; a SQLite built to ignore it is refused), `trusted_schema=OFF`,
  and SQLite's defensive mode, which blocks SQL that could corrupt the file (`writable_schema`).
- **Checked after the open, too:** SQLite opens by path, so once it has, the DB and its side files
  are checked again: the same inode, regular, private, on the verified device.
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
from collections.abc import Callable, Sequence
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


def open_db(
    data_dir: DataDir, *, steps: list[tuple[int, str]] | None = None, timeout: float = 5.0
) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the user DB. The connection is in autocommit mode;
    callers group writes with `with transaction(conn):`."""
    path = data_dir.root / DB_NAME
    inode = _create_private(path, data_dir)
    _check_side_files(path, data_dir)
    try:
        # as_uri() percent-encodes the path, so a `?`, `#` or `%` in it can't change the file or add
        # URI parameters.
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode=rw",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            timeout=timeout,
        )
    except sqlite3.Error as e:
        raise DbError(f"can't open the user DB ({type(e).__name__})") from None
    try:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        # A read makes SQLite open the file now, so the path is checked again before anything is
        # written through it (WAL journaling writes at once).
        conn.execute("PRAGMA schema_version").fetchone()
        _check_still_ours(path, inode, data_dir)
        if any(row[0] == "TEMP_STORE=0" for row in conn.execute("PRAGMA compile_options")):
            raise DbError(
                "this SQLite ignores temp_store=MEMORY, so temp data could reach plain disk (T-402)"
            )
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
        _check_still_ours(path, inode, data_dir)
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise DbError("the user DB failed its integrity check (T-408); restore it from a backup")
        migrate(conn, migrations() if steps is None else steps)
    except sqlite3.DatabaseError as e:
        conn.close()
        name = getattr(e, "sqlite_errorname", "")
        if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
            raise DbError(
                "the user DB is in use: is another copy of the app running on this data directory?"
            ) from None
        if name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")):
            raise DbError(f"the user DB can't be read ({name}); it may be damaged (T-408)") from None
        raise DbError(f"the user DB couldn't be opened or migrated ({name or type(e).__name__})") from None
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection, steps: list[tuple[int, str]]) -> None:
    steps = migrations(steps)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(steps):
        raise DbError("the user DB was written by a newer version of the app; update the app")
    if current < 0:
        raise DbError("the user DB has an impossible schema version; it may be damaged (T-408)")
    if current == len(steps):
        return
    # Foreign keys can only be switched outside a transaction. Off while the steps run, so a step can
    # rebuild a table the way SQLite documents (ALTER TABLE, "other kinds of schema changes"); every
    # reference is checked before each step commits, and they are switched back on afterwards.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for version, sql in steps[current:]:
            # One transaction per step, the version bump included, so a failed step leaves the DB
            # exactly at the previous version. The step can't end that transaction early: any
            # transaction statement after the opening BEGIN (COMMIT, END, ROLLBACK, a savepoint) is
            # refused. The `;` on its own line ends a step whose last statement has none.
            conn.set_authorizer(_one_transaction())
            try:
                conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n;\nPRAGMA user_version = {int(version)};")
                conn.set_authorizer(None)
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DbError(f"migration {version} left a dangling reference")
                conn.execute("COMMIT")
            except BaseException:
                conn.set_authorizer(None)
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise DbError("foreign keys couldn't be switched back on after the migrations")


def _one_transaction() -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    """An authorizer that allows the first transaction statement it sees and refuses every later
    one, so a migration step can't commit or roll back the runner's transaction."""
    seen = 0

    def authorize(action: int, _a: str | None, _b: str | None, _db: str | None, _trigger: str | None) -> int:
        nonlocal seen
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            seen += 1
            if seen > 1:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


class transaction:
    """`BEGIN IMMEDIATE` … `COMMIT`, or `ROLLBACK` on any exception. Nests: inside another
    transaction it is a SAVEPOINT, so helpers can be grouped into one atomic change (a reorg drops
    cache rows and moves the tip together, T-207). Every savepoint has the same name: `ROLLBACK TO`
    and `RELEASE` act on the innermost one of that name."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.nested = False

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            self.nested = True
            self.conn.execute("SAVEPOINT coinacct")
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, kind: object, value: object, tb: object) -> None:
        if self.nested:
            if kind is None:
                self.conn.execute("RELEASE coinacct")
                return
            # Undo the body's changes, but let its own exception be the one that propagates, even
            # if SQLite already rolled the whole transaction back (no savepoint left).
            try:
                self.conn.execute("ROLLBACK TO coinacct")
                self.conn.execute("RELEASE coinacct")
            except sqlite3.Error:
                pass
            return
        if kind is not None:
            # Roll back, but let the body's own exception be the one that propagates.
            if self.conn.in_transaction:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            return
        try:
            self.conn.execute("COMMIT")
        except BaseException:
            # A failed COMMIT (busy, disk full) mustn't leave the shared connection mid-transaction.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise


def _create_private(path: Path, data_dir: DataDir) -> int:
    """Create the DB file if missing, mode 0600, never through a link, and check what's there.
    Returns its inode, to confirm later that SQLite opened this file."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR):
            raise DbError(f"{path} must be a regular file, not a link, pipe or directory (T-401)") from None
        raise DbError(f"can't open {path} ({e.strerror})") from None
    try:
        return _check_fd(fd, path, data_dir)
    finally:
        os.close(fd)


def _check_side_files(path: Path, data_dir: DataDir) -> None:
    for suffix in SIDE_FILES:
        _check_side_file(Path(f"{path}{suffix}"), data_dir)


def _check_still_ours(path: Path, inode: int, data_dir: DataDir) -> None:
    """After SQLite opened the DB and made its WAL files: the path still names the file that was
    checked (same inode). A best-effort check: Python's sqlite3 can't open with SQLITE_OPEN_NOFOLLOW."""
    try:
        st = os.lstat(path)
    except OSError:
        raise DbError(f"{path} disappeared while it was opened (T-401)") from None
    if not stat.S_ISREG(st.st_mode) or st.st_ino != inode:
        raise DbError(f"{path} was replaced while it was opened (T-401)")
    _check_stat(st, path, data_dir)
    _check_side_files(path, data_dir)


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


def _check_fd(fd: int, path: Path, data_dir: DataDir) -> int:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise DbError(f"{path} must be a regular file (T-401)")
    _check_stat(st, path, data_dir)
    return st.st_ino


def _check_stat(st: os.stat_result, path: Path, data_dir: DataDir) -> None:
    try:
        require_device(path, st, data_dir.device)
    except DataDirError as e:
        raise DbError(str(e)) from None
    if st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise DbError(f"{path} must be the user's own file with mode 600 (T-401)")

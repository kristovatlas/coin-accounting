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
- **One writer, many readers** (architecture §3): `open_db` returns the one writer connection, used
  only by the job worker (and start-up and shutdown, before and after it runs) and by short API
  writes. Its `lock` serialises their transactions: `transaction`, and `hold` for a read that opens a
  transaction of its own, keep it from `BEGIN` to the end, so one thread's statements never land in
  another's transaction. Waiting for it is bounded by the connection's timeout and ends in a "busy"
  `DbError`; SQLite's own busy wait, for another process, is bounded by the same timeout after it.
  A lone statement outside a transaction isn't serialised and would see another thread's
  uncommitted rows, so API reads never use the writer.
  They use readers: `open_reader` opens a read-only connection (`mode=ro`, `query_only`) to the same,
  already-migrated file, with the same file checks, for one thread (one request) at a time.
- **Schema:** the numbered steps in `migrations/` (`mNNNN_name.py`, each an `SQL` string), applied in
  order, each in its own transaction, with `PRAGMA user_version` recording the last one. A DB from a
  newer app version is refused rather than guessed at.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from coinacct.storage.datadir import DataDir, DataDirError, require_device
from coinacct.storage.migrations import STEPS

DB_NAME: Final = "db.sqlite"

# For modules outside `storage/`, which may hold a connection but not import `sqlite3` (architecture §2).
type Connection = sqlite3.Connection
SIDE_FILES: Final = ("-wal", "-shm", "-journal")


class DbError(Exception):
    """The user DB can't be used. The message says why; it never contains user data."""


class WriterConnection(sqlite3.Connection):
    """The one writer connection (architecture §3), with the lock its transactions hold."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.lock = threading.RLock()
        self.lock_timeout = 5.0  # open_db sets the caller's timeout


def writer_lock(conn: sqlite3.Connection) -> threading.RLock | None:
    """The writer's lock; None for a connection that isn't the writer (a reader, or a test's own)."""
    return conn.lock if isinstance(conn, WriterConnection) else None


def _acquire(conn: sqlite3.Connection) -> threading.RLock | None:
    """Take the writer's lock (None on another connection), waiting at most its timeout."""
    if not isinstance(conn, WriterConnection):
        return None
    if not conn.lock.acquire(timeout=conn.lock_timeout):
        raise DbError("the user DB is busy; try again in a moment")
    return conn.lock


class hold:
    """Hold the writer's lock (nothing on another connection) for a read that opens a transaction of
    its own, such as a snapshot, so it can't start inside another thread's transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.lock: threading.RLock | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.lock = _acquire(self.conn)
        return self.conn

    def __exit__(self, kind: object, value: object, tb: object) -> None:
        if self.lock is not None:
            self.lock.release()


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
            factory=WriterConnection,
        )
    except sqlite3.Error as e:
        raise DbError(f"can't open the user DB ({type(e).__name__})") from None
    if isinstance(conn, WriterConnection):
        # RLock.acquire waits forever for a negative timeout, where SQLite's means "don't wait".
        conn.lock_timeout = max(timeout, 0.0)
    try:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        # A read makes SQLite open the file now, so the path is checked again before WAL journaling
        # is switched on. (For a DB already in WAL mode, that read may already open its side files:
        # the check after the switch covers them; best-effort, see `_check_still_ours`.)
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


def open_reader(data_dir: DataDir, *, timeout: float = 5.0) -> sqlite3.Connection:
    """A read-only connection to the user DB that `open_db` has already opened and migrated
    (architecture §3: readers use separate connections). SQLite opens it `mode=ro`, with `query_only`
    on, after the same file checks as the writer's. It never creates the DB or changes its contents;
    SQLite may create the WAL side files, which are checked like the writer's. Use it only on the
    thread that opened it."""
    path = data_dir.root / DB_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR):
            raise DbError(f"{path} must be a regular file, not a link, pipe or directory (T-401)") from None
        raise DbError(f"can't open {path} ({e.strerror})") from None
    try:
        inode = _check_fd(fd, path, data_dir)
    finally:
        os.close(fd)
    _check_side_files(path, data_dir)
    try:
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=True,  # one thread (one request) per reader: its snapshots are its own
            timeout=timeout,
        )
    except sqlite3.Error as e:
        raise DbError(f"can't open the user DB ({type(e).__name__})") from None
    try:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        for pragma in ("query_only=ON", "temp_store=MEMORY", "trusted_schema=OFF"):
            conn.execute(f"PRAGMA {pragma}")
        conn.execute("PRAGMA schema_version").fetchone()
        _check_still_ours(path, inode, data_dir)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(migrations()):
            raise DbError("the user DB was written by a newer version of the app; update the app")
        if version != len(migrations()):
            raise DbError("the user DB isn't at this app's schema version; open it with open_db first")
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DbError(
            f"the user DB can't be read ({getattr(e, 'sqlite_errorname', '') or type(e).__name__})"
        ) from None
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
        self.lock: threading.RLock | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.lock = _acquire(self.conn)  # held until __exit__: no other thread's statements join this one
        try:
            self._begin()
        except BaseException:
            if self.lock is not None:
                self.lock.release()
            raise
        return self.conn

    def __exit__(self, kind: object, value: object, tb: object) -> None:
        try:
            self._end(kind)
        finally:
            if self.lock is not None:
                self.lock.release()

    def _begin(self) -> None:
        if self.conn.in_transaction:
            self.nested = True
            self.conn.execute("SAVEPOINT coinacct")
        else:
            self.conn.execute("BEGIN IMMEDIATE")

    def _end(self, kind: object) -> None:
        if self.nested:
            if kind is None:
                self.conn.execute("RELEASE coinacct")
                return
            # Undo the body's changes, but let its own exception be the one that propagates, even
            # if SQLite already rolled the whole transaction back (no savepoint left). If the
            # savepoint can't be rolled back while the transaction is still open, the whole
            # transaction is rolled back, so the body's partial writes can never be committed; if
            # even that fails, the failure propagates instead, so no caller carries on and commits.
            try:
                self.conn.execute("ROLLBACK TO coinacct")
                self.conn.execute("RELEASE coinacct")
            except sqlite3.Error:
                if self.conn.in_transaction:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        raise DbError("a failed change couldn't be rolled back; close the user DB") from None
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

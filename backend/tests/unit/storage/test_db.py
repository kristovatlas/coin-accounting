"""The user DB: location, file checks, settings, integrity and migrations (architecture §6;
THREAT_MODEL T-401, T-402, T-408)."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from coinacct.storage import accounts, db
from coinacct.storage.chain_state import (
    Tip,
    last_tip,
    peek_recorded_chain,
    record_chain,
    recorded_chain,
    set_tip,
)
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import DB_NAME, DbError, migrate, migrations, open_db, transaction


@pytest.fixture
def dd(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


def test_the_db_is_a_private_file_under_the_data_directory_t401(dd: DataDir) -> None:
    conn = open_db(dd)
    conn.close()
    st = os.stat(dd.root / DB_NAME)
    assert st.st_mode & 0o777 == 0o600 and st.st_uid == os.getuid()


def test_the_settings_keep_temp_data_in_memory_and_use_wal_t402(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert conn.execute("PRAGMA temp_store").fetchone() == (2,)  # MEMORY
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA synchronous").fetchone() == (2,)  # FULL
        assert conn.execute("PRAGMA trusted_schema").fetchone() == (0,)
        record_chain(conn, "regtest")  # a write, so the WAL files exist while the connection is open
        for side in ("-wal", "-shm"):
            side_file = dd.root / f"{DB_NAME}{side}"
            st = side_file.stat()
            assert st.st_mode & 0o777 == 0o600 and st.st_dev == dd.device  # SQLite copies the DB's mode
    finally:
        conn.close()


def test_a_linked_db_is_refused_t401(dd: DataDir, tmp_path: Path) -> None:
    elsewhere = tmp_path / "plain-disk.sqlite"
    elsewhere.touch(mode=0o600)
    (dd.root / DB_NAME).symlink_to(elsewhere)
    with pytest.raises(DbError, match="link"):
        open_db(dd)


@pytest.mark.parametrize("side", ["-wal", "-shm", "-journal"])
def test_a_linked_side_file_is_refused_t402(dd: DataDir, tmp_path: Path, side: str) -> None:
    open_db(dd).close()
    target = tmp_path / "plain-disk-wal"
    target.touch(mode=0o600)
    link = dd.root / f"{DB_NAME}{side}"
    if link.exists():
        link.unlink()
    link.symlink_to(target)
    with pytest.raises(DbError, match="link"):
        open_db(dd)


def test_a_db_readable_by_others_is_refused_t401(dd: DataDir) -> None:
    open_db(dd).close()
    os.chmod(dd.root / DB_NAME, 0o644)
    with pytest.raises(DbError, match="mode 600"):
        open_db(dd)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_a_side_file_readable_by_others_is_refused(dd: DataDir, suffix: str) -> None:
    open_db(dd).close()
    side = dd.root / f"{DB_NAME}{suffix}"
    side.touch(mode=0o644)
    os.chmod(side, 0o644)
    with pytest.raises(DbError, match="mode 600"):
        open_db(dd)


@pytest.mark.parametrize("name", [DB_NAME, f"{DB_NAME}-wal"])
def test_a_directory_in_place_of_the_db_or_a_side_file_is_refused(dd: DataDir, name: str) -> None:
    (dd.root / name).mkdir(mode=0o700)
    with pytest.raises(DbError, match="regular file"):
        open_db(dd)


def test_a_fifo_in_place_of_the_db_is_refused(dd: DataDir) -> None:
    os.mkfifo(dd.root / DB_NAME, 0o600)
    with pytest.raises(DbError, match="regular file"):
        open_db(dd)


def test_a_damaged_db_is_refused_t408(dd: DataDir) -> None:
    path = dd.root / DB_NAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"SQLite format 3\x00" + b"\xff" * 4000)  # a header and then garbage
    os.close(fd)
    with pytest.raises(DbError, match="T-408"):
        open_db(dd)


def test_a_db_that_opens_but_fails_its_integrity_check_is_refused_t408(dd: DataDir) -> None:
    open_db(dd).close()
    # The app's own connection is defensive and refuses writable_schema; corrupt it from outside.
    conn = sqlite3.connect(dd.root / DB_NAME)
    conn.executescript("CREATE TABLE t (a INTEGER); CREATE INDEX i ON t (a); INSERT INTO t VALUES (1), (2);")
    # Make the index disagree with its table: the file is still readable, only the check catches it.
    conn.executescript(
        "PRAGMA writable_schema = ON; UPDATE sqlite_schema SET sql = 'CREATE INDEX i ON t (a DESC)' "
        "WHERE name = 'i'; PRAGMA writable_schema = OFF;"
    )
    conn.close()
    with pytest.raises(DbError, match="integrity check"):
        open_db(dd)


def test_migrations_run_once_and_record_the_version(dd: DataDir) -> None:
    conn = open_db(dd)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(migrations()) >= 1
    conn.close()
    conn = open_db(dd)  # a second open applies nothing new
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    conn.close()


def test_the_apps_connection_refuses_sql_that_could_corrupt_the_file_t408(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        conn.execute("PRAGMA writable_schema = ON")  # defensive mode makes this a no-op
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE sqlite_schema SET sql = 'x' WHERE name = 'chain_state'")
    finally:
        conn.close()


def test_a_data_directory_with_uri_characters_opens_the_right_file(tmp_path: Path) -> None:
    d = tmp_path / "a?mode=ro#b%41"
    d.mkdir(mode=0o700)
    weird = open_data_dir(str(d))
    conn = open_db(weird)
    try:
        record_chain(conn, "regtest")
    finally:
        conn.close()
    assert (d / DB_NAME).is_file() and not (tmp_path / "a").exists()


def test_a_db_swapped_after_the_checks_is_refused_t401(dd: DataDir, monkeypatch: pytest.MonkeyPatch) -> None:
    real = db._create_private

    def swap(path: Path, data_dir: DataDir) -> int:
        inode = real(path, data_dir)
        # Move the original aside rather than unlinking it, so its inode stays allocated and the
        # new file can't reuse the number (ext4 often hands a freed inode straight back).
        path.rename(path.with_name("moved-aside"))
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        return inode

    monkeypatch.setattr(db, "_create_private", swap)
    with pytest.raises(DbError, match="replaced"):
        open_db(dd)


def test_a_sqlite_that_ignores_temp_store_is_refused_t402(
    dd: DataDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Options:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def execute(self, sql: str) -> object:
            if sql == "PRAGMA compile_options":
                return [("TEMP_STORE=0",)]
            return self.conn.execute(sql)

        def __getattr__(self, name: str) -> object:
            return getattr(self.conn, name)

    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Options(real_connect(*a, **k)))
    with pytest.raises(DbError, match="temp_store"):
        open_db(dd)


def test_a_negative_schema_version_is_refused_t408(dd: DataDir) -> None:
    conn = open_db(dd)
    conn.execute("PRAGMA user_version = -1")
    conn.close()
    with pytest.raises(DbError, match="impossible schema version"):
        open_db(dd)


def test_a_failing_step_leaves_no_open_transaction(dd: DataDir) -> None:
    conn = open_db(dd)
    shipped = len(migrations())
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(
                conn, [*migrations(), (shipped + 1, "CREATE TABLE z (x INTEGER) STRICT; SELECT nosuch();")]
            )
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'z'").fetchone() == (0,)
        assert conn.execute("PRAGMA user_version").fetchone() == (shipped,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize("statement", ["COMMIT", "END", "ROLLBACK", "SAVEPOINT s", "RELEASE s", "BEGIN"])
def test_a_step_cant_end_the_runners_transaction(dd: DataDir, statement: str) -> None:
    steps = [
        (1, "CREATE TABLE a (x INTEGER) STRICT;"),
        (2, f"CREATE TABLE b (x INTEGER) STRICT; {statement}; CREATE TABLE z (x INTEGER) STRICT;"),
    ]
    with pytest.raises(DbError):
        open_db(dd, steps=steps)
    conn = open_db(dd, steps=steps[:1])
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        assert names == {"a"}  # nothing of step 2 was committed
    finally:
        conn.close()


def test_a_step_may_create_a_trigger_and_omit_its_last_semicolon(dd: DataDir) -> None:
    steps = [
        (
            1,
            "CREATE TABLE a (x INTEGER) STRICT;"
            " CREATE TRIGGER a_kept BEFORE DELETE ON a BEGIN SELECT RAISE(ABORT, 'kept'); END;"
            " CREATE TABLE b (x INTEGER) STRICT",
        )
    ]
    conn = open_db(dd, steps=steps)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name IN ('a', 'a_kept', 'b')"
        ).fetchone() == (3,)
    finally:
        conn.close()


def test_a_failed_commit_leaves_no_open_transaction(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        # A deferred foreign-key violation makes COMMIT itself fail.
        conn.executescript(
            "CREATE TABLE p (id INTEGER PRIMARY KEY) STRICT;"
            "CREATE TABLE c (p INTEGER REFERENCES p (id) DEFERRABLE INITIALLY DEFERRED) STRICT;"
        )
        with pytest.raises(sqlite3.IntegrityError), transaction(conn):
            conn.execute("INSERT INTO c VALUES (99)")
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM c").fetchone() == (0,)
    finally:
        conn.close()


def test_the_bodys_exception_wins_over_a_failed_rollback(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        with pytest.raises(KeyError), transaction(conn):
            conn.execute("ROLLBACK")  # the transaction is already gone when the body fails
            raise KeyError("the body's error")
        assert not conn.in_transaction

        class FailingRollback:
            """The connection, except that ROLLBACK fails (as it can on a disk error)."""

            def __init__(self, inner: sqlite3.Connection) -> None:
                self.inner = inner

            @property
            def in_transaction(self) -> bool:
                return self.inner.in_transaction

            def execute(self, sql: str) -> sqlite3.Cursor:
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError("disk I/O error")
                return self.inner.execute(sql)

        proxy = FailingRollback(conn)
        with pytest.raises(KeyError), transaction(proxy):  # type: ignore[arg-type]
            raise KeyError("the body's error")
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_a_nested_transactions_error_survives_a_rollback_sqlite_already_did(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        with pytest.raises(KeyError), transaction(conn), transaction(conn):
            conn.execute("ROLLBACK")  # SQLite ended everything, savepoint included
            raise KeyError("the body's error")
        assert not conn.in_transaction
    finally:
        conn.close()


def test_a_db_from_a_newer_app_is_refused(dd: DataDir) -> None:
    conn = open_db(dd)
    conn.execute(f"PRAGMA user_version = {len(migrations()) + 1}")
    conn.close()
    with pytest.raises(DbError, match="newer version"):
        open_db(dd)


def test_a_failing_migration_leaves_the_db_at_the_previous_version(dd: DataDir) -> None:
    steps = [
        (1, "CREATE TABLE a (x INTEGER) STRICT;"),
        (2, "CREATE TABLE b (x INTEGER) STRICT; SELECT nosuch();"),
    ]
    with pytest.raises(DbError):
        open_db(dd, steps=steps)
    conn = open_db(dd, steps=steps[:1])
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        assert "a" in names and "b" not in names
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([(1, "SELECT 1;"), (3, "SELECT 1;")], "gaps"),
        ([(2, "SELECT 1;")], "gaps"),
        ([(2, "SELECT 1;"), (1, "SELECT 1;")], "in order"),
        ([(1, "  ")], "no SQL"),
    ],
)
def test_migration_steps_must_be_numbered_in_order_without_gaps(
    steps: list[tuple[int, str]], message: str
) -> None:
    with pytest.raises(DbError, match=message):
        migrations(steps)


def test_the_shipped_migrations_are_well_formed() -> None:
    steps = migrations()
    assert [v for v, _ in steps] == list(range(1, len(steps) + 1))
    conn = sqlite3.connect(":memory:")
    for _, sql in steps:
        conn.executescript(sql)  # each step is valid SQL on its own


def test_a_transaction_rolls_back_on_error(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        with pytest.raises(RuntimeError), transaction(conn):
            set_tip(conn, Tip("ab" * 32, 1))
            raise RuntimeError
        assert last_tip(conn) is None
    finally:
        conn.close()


def test_transactions_nest_so_helpers_join_one_atomic_change_t207(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        with pytest.raises(RuntimeError), transaction(conn):
            set_tip(conn, Tip("ab" * 32, 1))  # a nested helper: a savepoint inside the outer one
            raise RuntimeError  # the outer change fails, so the tip must not move either
        assert last_tip(conn) is None and not conn.in_transaction
        with transaction(conn):
            set_tip(conn, Tip("ab" * 32, 1))
            with pytest.raises(KeyError), transaction(conn):
                set_tip(conn, Tip("cd" * 32, 2))
                raise KeyError  # only the inner savepoint rolls back
        assert last_tip(conn) == Tip("ab" * 32, 1)
    finally:
        conn.close()


def test_the_schema_keeps_the_recorded_chain_t206(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            conn.execute("UPDATE chain_state SET chain = 'main'")
        with pytest.raises(sqlite3.IntegrityError, match="never removed"):
            conn.execute("DELETE FROM chain_state")
        # REPLACE deletes the old row without firing a delete trigger (#163).
        for replace in ("REPLACE INTO", "INSERT OR REPLACE INTO", "INSERT OR IGNORE INTO"):
            with pytest.raises(sqlite3.IntegrityError, match="never changes"):
                conn.execute(f"{replace} chain_state (id, chain) VALUES (1, 'main')")
        assert recorded_chain(conn) == "regtest"
    finally:
        conn.close()


def test_a_busy_db_is_reported_as_in_use_not_damaged(dd: DataDir) -> None:
    open_db(dd).close()
    holder = sqlite3.connect(dd.root / DB_NAME, timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DbError, match="in use"):
            later = (len(migrations()) + 1, "CREATE TABLE later (x INTEGER) STRICT;")
            open_db(dd, steps=[*migrations(), later], timeout=0.1)
    finally:
        holder.rollback()
        holder.close()


def test_a_migration_that_leaves_a_dangling_reference_is_rolled_back(dd: DataDir) -> None:
    steps = [
        (
            1,
            "CREATE TABLE p (id INTEGER PRIMARY KEY) STRICT;"
            "CREATE TABLE c (p INTEGER REFERENCES p (id)) STRICT;",
        ),
        (2, "INSERT INTO c VALUES (7);"),  # 7 isn't in p: allowed while keys are off, caught by the check
    ]
    with pytest.raises(DbError, match="dangling reference"):
        open_db(dd, steps=steps)
    conn = open_db(dd, steps=steps[:1])
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM c").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)  # back on after migrating
    finally:
        conn.close()


# --- chain state -------------------------------------------------------------------------------


def test_a_new_db_has_no_chain_and_no_tip(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        assert recorded_chain(conn) is None and last_tip(conn) is None
    finally:
        conn.close()


def test_the_chain_is_recorded_once_and_never_changes_t206(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        record_chain(conn, "regtest")  # the same chain again is fine
        with pytest.raises(DbError, match="T-206"):
            record_chain(conn, "main")
        assert recorded_chain(conn) == "regtest"
        with pytest.raises(ValueError, match="unknown chain"):
            record_chain(conn, "dogecoin")
    finally:
        conn.close()


def test_the_tip_needs_a_recorded_chain_and_survives_a_reopen_t207(dd: DataDir) -> None:
    conn = open_db(dd)
    tip = Tip("ab" * 32, 101)
    try:
        with pytest.raises(DbError, match="chain must be recorded"):
            set_tip(conn, tip)
        record_chain(conn, "regtest")
        set_tip(conn, tip)
    finally:
        conn.close()
    conn = open_db(dd)
    try:
        assert last_tip(conn) == tip
    finally:
        conn.close()


@pytest.mark.parametrize(("blockhash", "height"), [("AB" * 32, 1), ("ab" * 31, 1), ("ab" * 32, -1)])
def test_the_schema_refuses_a_malformed_tip(dd: DataDir, blockhash: str, height: int) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        with pytest.raises(sqlite3.IntegrityError):
            set_tip(conn, Tip(blockhash, height))
        assert last_tip(conn) is None
    finally:
        conn.close()


class _Proxy:
    """A connection whose `execute` can be made to fail for chosen statements."""

    def __init__(self, inner: sqlite3.Connection, fail: set[str], foreign_keys_off: bool = False) -> None:
        self.inner = inner
        self.fail = fail
        self.foreign_keys_off = foreign_keys_off

    @property
    def in_transaction(self) -> bool:
        return self.inner.in_transaction

    def execute(self, sql: str, *args: Any) -> object:
        if sql in self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        if self.foreign_keys_off and sql == "PRAGMA foreign_keys":
            return self.inner.execute("SELECT 0")
        return self.inner.execute(sql, *args)

    def executescript(self, sql: str) -> object:
        return self.inner.executescript(sql)

    def set_authorizer(self, authorizer: object) -> None:
        self.inner.set_authorizer(authorizer)  # type: ignore[arg-type]


def test_a_nested_rollback_that_fails_rolls_back_the_whole_transaction(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        proxy = _Proxy(conn, {"ROLLBACK TO coinacct"})
        # A caller that catches the inner error and carries on can't commit the inner partial write:
        # the whole transaction is gone, so its COMMIT fails.
        with pytest.raises(sqlite3.OperationalError), transaction(conn):
            set_tip(conn, Tip("ab" * 32, 1))
            try:
                with transaction(proxy):  # type: ignore[arg-type]
                    conn.execute("UPDATE chain_state SET tip_height = 2")
                    raise KeyError("the body's error")
            except KeyError:
                pass
        assert not conn.in_transaction
        assert last_tip(conn) is None
    finally:
        conn.close()


def test_a_rollback_that_cant_be_done_at_all_is_an_error_not_a_commit(dd: DataDir) -> None:
    conn = open_db(dd)
    try:
        record_chain(conn, "regtest")
        proxy = _Proxy(conn, {"ROLLBACK TO coinacct", "ROLLBACK"})
        with pytest.raises(DbError, match="couldn't be rolled back"), transaction(conn):
            set_tip(conn, Tip("ab" * 32, 1))
            try:
                with transaction(proxy):  # type: ignore[arg-type]
                    conn.execute("UPDATE chain_state SET tip_height = 2")
                    raise KeyError("the body's error")
            except KeyError:  # the rollback failure isn't a KeyError, so it reaches the outer block
                pass
        assert not conn.in_transaction and last_tip(conn) is None
    finally:
        conn.close()


def test_the_path_is_checked_again_before_wal_is_switched_on_t401(
    dd: DataDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object) -> None:
        raise DbError("replaced")

    monkeypatch.setattr(db, "_check_still_ours", refuse)
    with pytest.raises(DbError, match="replaced"):
        open_db(dd)
    assert not (dd.root / f"{DB_NAME}-wal").exists()  # refused before journaling wrote anything


def test_foreign_keys_left_off_after_the_migrations_are_refused(dd: DataDir) -> None:
    conn = open_db(dd, steps=[])
    try:
        proxy = _Proxy(conn, set(), foreign_keys_off=True)
        with pytest.raises(DbError, match="foreign keys"):
            migrate(proxy, migrations())  # type: ignore[arg-type]
    finally:
        conn.close()


def test_peeking_at_the_recorded_chain_writes_nothing_and_never_follows_a_link_t401(
    dd: DataDir, tmp_path: Path
) -> None:
    path = dd.root / DB_NAME
    assert peek_recorded_chain(path) is None  # no DB yet
    conn = open_db(dd)
    assert peek_recorded_chain(path) is None  # a DB with no chain recorded
    record_chain(conn, "signet")
    conn.close()
    before = sorted(p.name for p in dd.root.iterdir())
    assert peek_recorded_chain(path) == "signet"
    assert sorted(p.name for p in dd.root.iterdir()) == before  # no WAL or shm files appeared
    elsewhere = tmp_path / "plain.sqlite"
    elsewhere.write_bytes(path.read_bytes())
    link = tmp_path / "link.sqlite"
    link.symlink_to(elsewhere)
    assert peek_recorded_chain(link) is None
    not_a_db = tmp_path / "not-a-db"
    not_a_db.write_text("hello")
    assert peek_recorded_chain(not_a_db) is None
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert peek_recorded_chain(empty) is None  # no chain_state table


# --- One writer, many readers (architecture §3) -----------------------------------------------------


def test_a_transaction_on_the_writer_holds_its_lock_across_threads(dd: DataDir) -> None:
    conn = open_db(dd)
    record_chain(conn, "regtest")
    order: list[str] = []
    inside, release = threading.Event(), threading.Event()

    def first() -> None:
        with transaction(conn):
            order.append("first begins")
            inside.set()
            release.wait(5)
            conn.execute("UPDATE chain_state SET tip_hash = NULL WHERE id = 1")
            order.append("first ends")

    def second() -> None:
        with transaction(conn):  # waits: it can't BEGIN, or join first's transaction, while first runs
            order.append("second begins")

    a = threading.Thread(target=first)
    a.start()
    assert inside.wait(5)
    b = threading.Thread(target=second)
    b.start()
    b.join(0.2)
    assert b.is_alive() and order == ["first begins"]
    release.set()
    a.join(5)
    b.join(5)
    assert order == ["first begins", "first ends", "second begins"]
    conn.close()


def test_the_lock_is_released_whatever_the_body_or_begin_does(dd: DataDir) -> None:
    conn = open_db(dd)
    lock = db.writer_lock(conn)
    assert lock is not None
    with pytest.raises(ValueError), transaction(conn):
        raise ValueError
    with transaction(conn), transaction(conn):  # nested on one thread: the lock is re-entrant
        pass
    other = sqlite3.connect(dd.root / DB_NAME, timeout=0)
    other.execute("BEGIN IMMEDIATE")  # another process holds the write lock: BEGIN fails
    with pytest.raises(sqlite3.OperationalError), transaction(conn):
        pass
    other.execute("ROLLBACK")
    other.close()
    assert lock.acquire(blocking=False)  # nothing still holds it
    lock.release()
    conn.close()


def test_a_snapshot_read_waits_for_the_writers_transaction(dd: DataDir) -> None:
    conn = open_db(dd)
    wallet = accounts.add_tax_account(conn, "Cold storage", "self_custody")
    inside, release, done = threading.Event(), threading.Event(), threading.Event()

    def writer() -> None:
        with transaction(conn):
            accounts.add_addresses(
                conn,
                [("0014" + "11" * 20, None)],
                entity_id=accounts.ME,
                tax_account_id=wallet,
                source="manual",
            )
            inside.set()
            release.wait(5)

    seen: list[int] = []

    def reader() -> None:
        seen.append(len(accounts.addresses(conn)))  # a snapshot read: it takes the writer's lock
        done.set()

    a = threading.Thread(target=writer)
    a.start()
    assert inside.wait(5)
    threading.Thread(target=reader).start()
    assert not done.wait(0.2)
    release.set()
    assert done.wait(5) and seen == [1]
    a.join(5)
    conn.close()


def test_a_reader_reads_committed_rows_only_and_can_never_write(dd: DataDir) -> None:
    conn = open_db(dd)
    record_chain(conn, "regtest")
    reader = db.open_reader(dd)
    try:
        assert db.writer_lock(reader) is None
        assert recorded_chain(reader) == "regtest"
        with transaction(conn):
            set_tip(conn, Tip("ab" * 32, 5))
            assert last_tip(reader) is None  # the writer's open transaction isn't visible
        assert last_tip(reader) == Tip("ab" * 32, 5)
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("UPDATE chain_state SET tip_hash = NULL WHERE id = 1")
        assert reader.execute("PRAGMA query_only").fetchone() == (1,)
        assert reader.execute("PRAGMA trusted_schema").fetchone() == (0,)
        assert reader.execute("PRAGMA temp_store").fetchone() == (2,)
    finally:
        reader.close()
        conn.close()


def test_a_reader_never_creates_or_migrates_the_db(dd: DataDir) -> None:
    with pytest.raises(DbError):
        db.open_reader(dd)
    assert not (dd.root / DB_NAME).exists()
    conn = open_db(dd, steps=migrations()[:1])  # an older schema
    conn.close()
    with pytest.raises(DbError, match="schema version"):
        db.open_reader(dd)


def test_a_reader_refuses_a_linked_or_shared_db_t401(dd: DataDir, tmp_path: Path) -> None:
    conn = open_db(dd)
    conn.close()
    path = dd.root / DB_NAME
    path.chmod(0o644)
    with pytest.raises(DbError, match="mode 600"):
        db.open_reader(dd)
    path.chmod(0o600)
    moved = tmp_path / "plain-disk.sqlite"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(DbError, match="link"):
        db.open_reader(dd)


def test_a_reader_opens_the_file_read_only(dd: DataDir) -> None:
    conn = open_db(dd)
    record_chain(conn, "regtest")
    path = dd.root / DB_NAME
    path.chmod(0o400)  # a file the user made read-only still opens for reading
    try:
        reader = db.open_reader(dd)
        assert recorded_chain(reader) == "regtest"
        reader.close()
    finally:
        path.chmod(0o600)
        conn.close()

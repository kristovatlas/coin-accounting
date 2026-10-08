"""The user DB: location, file checks, settings, integrity and migrations (architecture §6;
THREAT_MODEL T-401, T-402, T-408)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from coinacct.storage import db
from coinacct.storage.chain_state import Tip, last_tip, record_chain, recorded_chain, set_tip
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
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, [(1, "SELECT 1;"), (2, "CREATE TABLE z (x INTEGER) STRICT; SELECT nosuch();")])
        assert not conn.in_transaction
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
        assert recorded_chain(conn) == "regtest"
    finally:
        conn.close()


def test_a_busy_db_is_reported_as_in_use_not_damaged(dd: DataDir) -> None:
    open_db(dd).close()
    holder = sqlite3.connect(dd.root / DB_NAME, timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DbError, match="in use"):
            open_db(dd, steps=[(1, "SELECT 1;"), (2, "CREATE TABLE later (x INTEGER) STRICT;")], timeout=0.1)
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

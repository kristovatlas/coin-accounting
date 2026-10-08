"""Serving the app (architecture §3, §4, §6, §8.1; THREAT_MODEL T-103, T-110, T-401, T-402, T-405).

`serve` runs with a fake server that loops until it is told to stop, and with the node checks
replaced, so these tests need no node. The real thing, against a regtest node, is in
tests/integration/launcher.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coinacct import config, launcher
from coinacct.api import runtime
from coinacct.launcher import LaunchError, Prepared
from coinacct.services import discovery, startup
from coinacct.services.lifecycle import DEADLINE_SECONDS
from coinacct.services.startup import NodeStatus, StorageRefused
from coinacct.storage import datadir
from coinacct.storage.chain_state import record_chain, recorded_chain
from coinacct.storage.db import DbError, open_db
from coinacct.storage.volume import Encryption, VolumeStatus
from coinacct.storage.watchdog import Watchdog
from tests.unit.api.asgi import call
from tests.unit.services.test_startup import Node

CONFIG = '[rpc]\nhost = "127.0.0.1"\nport = 18443\nuser = "ro-client"\npassword = "serve-test-password"\n'
ONLINE = NodeStatus(online=True, chain="regtest", reasons=())
WAIT = 5.0


@pytest.fixture(autouse=True)
def no_checkout_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` reads the checkout's frontend/dist by default: point it at an empty place, so these
    tests never depend on whatever was last built there."""
    monkeypatch.setattr(launcher, "FRONTEND_DIST", tmp_path / "no-build")


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Prepared:
    monkeypatch.setattr(
        datadir, "detect", lambda root: VolumeStatus(Encryption.VERACRYPT, "test volume", "ext4")
    )
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return Prepared(
        data_dir=datadir.open_data_dir(str(d)),
        config=config.parse(CONFIG),
        needs_test_chain=False,
        open_browser=False,
    )


def with_browser(prepared: Prepared) -> Prepared:
    return Prepared(prepared.data_dir, prepared.config, prepared.needs_test_chain, open_browser=True)


class FakeServer:
    """Stands in for uvicorn: `run` marks itself started, runs `during`, then loops until
    `should_exit`."""

    def __init__(self, app: Any, events: list[str]) -> None:
        self.app, self.events = app, events
        self.should_exit = False
        self.force_exit = False
        self.started = False
        self.sockets: list[socket.socket] = []
        self.during: Callable[[FakeServer], None] = lambda server: None

    def run(self, sockets: list[socket.socket]) -> None:
        self.sockets = sockets
        self.started = True
        self.during(self)
        deadline = time.monotonic() + WAIT
        while not self.should_exit:
            assert time.monotonic() < deadline, "the server was never told to stop"
            threading.Event().wait(0.01)
        self.events.append("server stopped")


class Harness:
    def __init__(self, status: NodeStatus = ONLINE, ttl: float = 60.0) -> None:
        self.events: list[str] = []
        self.server: FakeServer | None = None
        self.runtime: runtime.Runtime | None = None
        self.announced: list[str] = []
        self.opened: list[Path] = []
        self.browser_result = True
        self.during: Callable[[FakeServer], None] = lambda server: None
        self.status = status
        self.ttl = ttl
        self.files_at_check: list[Path] = []
        self.listening_at_check: bool | None = None
        self.data: Path | None = None
        self.port: int | None = None
        self.make_watchdog: Callable[..., Watchdog] = Watchdog
        self.exits: list[int] = []
        self.opened_while_started: list[bool] = []
        self.db: sqlite3.Connection | None = None

    def check(self, *args: Any, **kwargs: Any) -> NodeStatus:
        assert self.data is not None
        self.files_at_check = list(self.data.glob(f"{launcher.BOOTSTRAP_PREFIX}*"))
        self.events.append("check")
        return self.status

    def build(self, **kwargs: Any) -> runtime.Runtime:
        self.port = kwargs["port"]
        self.listening_at_check = can_connect(kwargs["port"])
        self.runtime = runtime.build(check=self.check, ttl=self.ttl, start_jobs=self.start_jobs, **kwargs)
        return self.runtime

    def start_jobs(self, *args: Any, db: sqlite3.Connection, **kwargs: Any) -> Any:
        events = self.events
        self.db = db
        self.subjects_at_start = kwargs["subjects"]()  # what the chain jobs scan: the imports, from the DB
        self.discover = kwargs["discover"]  # how they grow descriptor windows after a sync

        class Jobs:
            stopped = True

            def stop(self) -> None:
                db.execute("SELECT 1")  # the user DB is still open (§3: jobs stop before it closes)
                events.append("chain jobs stopped")

            def request_sync(self) -> None:
                events.append("sync requested")

        self.events.append("chain jobs started")
        return Jobs()

    def make_server(self, app: Any) -> FakeServer:
        self.server = FakeServer(app, self.events)
        self.server.during = self.during
        return self.server

    def open_browser(self, path: Path) -> bool:
        # Recorded, not asserted: launch() would swallow an assertion error from its thread.
        self.opened_while_started.append(self.server is not None and self.server.started)
        self.opened.append(path)
        return self.browser_result

    def serve(self, prepared: Prepared) -> int:
        self.data = prepared.data_dir.root
        return launcher.serve(
            prepared,
            env={},
            platform="linux",
            build=self.build,
            make_server=self.make_server,
            make_watchdog=self.make_watchdog,
            open_browser=self.open_browser,
            announce=self.announced.append,
            exit_process=self.exits.append,
        )


def no_exit(code: int) -> None:
    pytest.fail(f"the process would have been ended with {code}")


def no_announcement(text: str) -> None:
    pytest.fail(f"unexpected announcement: {text}")


def can_connect(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


def launch_file(announced: list[str]) -> Path:
    match = re.fullmatch(r"coinacct: (?:launch file|open this file in your browser:) (.+)", announced[0])
    assert match
    return Path(match.group(1))


def quitting(h: Harness) -> Callable[[FakeServer], None]:
    def during(server: FakeServer) -> None:
        assert h.runtime is not None
        h.runtime.shutdown.request(runtime.QUIT_REASON)

    return during


def wait_for(condition: Callable[[], bool]) -> bool:
    for _ in range(int(WAIT / 0.01)):
        if condition():
            return True
        threading.Event().wait(0.01)
    return False


# --- Start-up order (§8.1) ---------------------------------------------------------------------


def test_quit_stops_the_server_and_exits_cleanly(prepared: Prepared) -> None:
    h = Harness()
    h.during = quitting(h)
    assert h.serve(prepared) == 0
    assert h.server is not None and h.server.should_exit
    assert h.runtime is not None and h.runtime.shutdown.reason == runtime.QUIT_REASON


def test_the_built_frontend_is_what_the_app_serves(
    prepared: Prepared, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>the build</title>")
    (dist / "assets" / "index-abc.js").write_text("export {};")
    monkeypatch.setattr(launcher, "FRONTEND_DIST", dist)
    h = Harness()
    served: dict[str, bytes] = {}

    def during(server: FakeServer) -> None:
        host = f"127.0.0.1:{h.port}"
        served["/"] = call(server.app, "GET", "/", host=host).body
        served["/assets/index-abc.js"] = call(server.app, "GET", "/assets/index-abc.js", host=host).body
        quitting(h)(server)

    h.during = during
    assert h.serve(prepared) == 0
    assert served == {"/": b"<!doctype html><title>the build</title>", "/assets/index-abc.js": b"export {};"}


def test_without_a_build_the_placeholder_is_served(prepared: Prepared) -> None:
    h = Harness()
    served: list[bytes] = []

    def during(server: FakeServer) -> None:
        served.append(call(server.app, "GET", "/", host=f"127.0.0.1:{h.port}").body)
        quitting(h)(server)

    h.during = during
    assert h.serve(prepared) == 0
    assert b'<script src="/app.js"' in served[0]


def test_nothing_listens_and_no_launch_file_exists_during_the_node_checks(prepared: Prepared) -> None:
    h = Harness()
    h.during = quitting(h)
    h.serve(prepared)
    assert h.events[0] == "check"
    assert h.files_at_check == []
    assert h.listening_at_check is False


def test_the_server_gets_a_listening_loopback_socket_t103(prepared: Prepared) -> None:
    h = Harness()
    seen: list[tuple[str, int, bool]] = []

    def look(server: FakeServer) -> None:
        host, port = server.sockets[0].getsockname()
        seen.append((host, port, can_connect(port)))
        quitting(h)(server)

    h.during = look
    h.serve(prepared)
    host, port, listening = seen[0]
    assert host == "127.0.0.1"
    assert port > 0
    assert listening  # a browser quicker than uvicorn waits in the backlog


def test_a_storage_refusal_stops_start_up_before_anything_is_served_t401(prepared: Prepared) -> None:
    def refuse(*args: Any, **kwargs: Any) -> NodeStatus:
        raise StorageRefused("--allow-unencrypted-storage is never accepted on mainnet")

    with pytest.raises(LaunchError, match="never accepted on mainnet"):
        launcher.serve(
            prepared,
            env={},
            build=functools.partial(runtime.build, check=refuse),
            announce=lambda s: None,
            exit_process=no_exit,
        )
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


def test_the_user_db_is_open_for_the_node_checks_and_closed_at_shutdown_t206(prepared: Prepared) -> None:
    h = Harness()
    seen: list[Any] = []

    def check(*args: Any, **kwargs: Any) -> NodeStatus:
        seen.append(kwargs["db"])
        assert kwargs["db"].execute("PRAGMA user_version").fetchone()[0] >= 1  # opened and migrated
        return h.status

    h.check = check  # type: ignore[method-assign]
    h.during = quitting(h)
    assert h.serve(prepared) == 0
    with pytest.raises(sqlite3.ProgrammingError):  # closed by the shutdown steps
        seen[0].execute("SELECT 1")


def test_a_user_db_that_cant_be_used_stops_start_up_before_the_node_checks_t408(prepared: Prepared) -> None:
    conn = sqlite3.connect(prepared.data_dir.root / "db.sqlite")
    conn.execute("PRAGMA user_version = 999")  # from a newer version of the app
    conn.close()
    os.chmod(prepared.data_dir.root / "db.sqlite", 0o600)
    h = Harness()
    with pytest.raises(LaunchError, match="newer version"):
        h.serve(prepared)
    assert "check" not in h.events
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


def through_the_real_checks(node: Node) -> Callable[..., NodeStatus]:
    """`startup.check_recorded` on a fake node: the launcher's DB, the real recording logic."""

    def check(*_args: Any, db: Any, volume: VolumeStatus, allow_unencrypted: bool) -> NodeStatus:
        return startup.check_recorded(
            node, db=db, volume=volume, allow_unencrypted=allow_unencrypted, sync_attempts=1
        )

    return check


def test_the_first_start_records_the_chain_and_a_later_node_on_another_chain_is_offline_t206(
    prepared: Prepared,
) -> None:
    first = Harness()
    first.check = through_the_real_checks(Node(chain="regtest"))  # type: ignore[method-assign]
    first.during = quitting(first)
    assert first.serve(prepared) == 0
    conn = open_db(prepared.data_dir)
    assert recorded_chain(conn) == "regtest"
    conn.close()

    second = Harness()
    second.check = through_the_real_checks(Node(chain="signet"))  # type: ignore[method-assign]
    second.during = quitting(second)
    assert second.serve(prepared) == 0
    assert second.runtime is not None and not second.runtime.status.online
    assert any("different chain" in r for r in second.runtime.status.reasons)
    conn = open_db(prepared.data_dir)
    assert recorded_chain(conn) == "regtest"
    conn.close()


@pytest.fixture
def unencrypted(prepared: Prepared, monkeypatch: pytest.MonkeyPatch) -> Prepared:
    monkeypatch.setattr(datadir, "detect", lambda root: VolumeStatus(Encryption.NONE, "plain disk", "ext4"))
    data = datadir.open_data_dir(str(prepared.data_dir.root))
    return Prepared(data, prepared.config, needs_test_chain=True, open_browser=False)


def test_an_existing_mainnet_db_on_plain_disk_is_refused_before_anything_writes_to_it_t401(
    unencrypted: Prepared,
) -> None:
    conn = open_db(unencrypted.data_dir)
    record_chain(conn, "main")
    conn.close()
    db_file = unencrypted.data_dir.root / "db.sqlite"
    before = db_file.read_bytes()
    h = Harness()
    with pytest.raises(LaunchError, match="never runs on unencrypted storage"):
        h.serve(unencrypted)
    assert "check" not in h.events and db_file.read_bytes() == before


def test_a_first_start_on_plain_disk_for_a_refused_chain_is_refused_and_records_nothing_t401(
    unencrypted: Prepared,
) -> None:
    h = Harness()
    h.check = through_the_real_checks(Node(chain="main"))  # type: ignore[method-assign]
    with pytest.raises(LaunchError, match="never accepted on mainnet"):
        h.serve(unencrypted)
    # The DB it created is kept (a concurrent launch may be using it), with no chain recorded.
    conn = open_db(unencrypted.data_dir)
    assert recorded_chain(conn) is None
    conn.close()


def test_a_db_that_fails_during_the_node_checks_refuses_with_its_reason(prepared: Prepared) -> None:
    def check(*args: Any, **kwargs: Any) -> NodeStatus:
        raise DbError("the user DB couldn't record its chain (SQLITE_BUSY)")

    with pytest.raises(LaunchError, match="couldn't record its chain"):
        launcher.serve(
            prepared,
            env={},
            build=functools.partial(runtime.build, check=check),
            announce=lambda s: None,
            exit_process=no_exit,
        )


def test_shutdown_during_the_node_checks_leaves_the_db_to_the_start_up_thread(prepared: Prepared) -> None:
    exits: list[int] = []
    used: list[int] = []

    def check_then_signal(*args: Any, db: Any, **kwargs: Any) -> NodeStatus:
        signal.raise_signal(signal.SIGTERM)
        assert wait_for(lambda: exits == [launcher.LAUNCH_ERROR_EXIT])  # the steps have all run
        used.append(db.execute("SELECT 1").fetchone()[0])  # still open: no ProgrammingError
        return ONLINE

    with pytest.raises(LaunchError, match="SIGTERM"):
        launcher.serve(
            prepared,
            env={},
            build=functools.partial(runtime.build, check=check_then_signal),
            announce=no_announcement,
            exit_process=exits.append,
        )
    assert used == [1]


def test_offline_mode_still_serves(prepared: Prepared) -> None:
    h = Harness(status=NodeStatus(online=False, chain=None, reasons=("the node can't be reached: refused",)))
    h.during = quitting(h)
    assert h.serve(prepared) == 0
    assert h.runtime is not None and not h.runtime.status.online


# --- The launch file (§4, T-110) ---------------------------------------------------------------


def test_the_launch_file_is_private_announced_and_removed_on_quit_t110(prepared: Prepared) -> None:
    h = Harness()
    seen: list[tuple[bool, int]] = []

    def look(server: FakeServer) -> None:
        path = launch_file(h.announced)
        seen.append((path.exists(), stat.S_IMODE(path.stat().st_mode)))
        quitting(h)(server)

    h.during = look
    h.serve(prepared)
    path = launch_file(h.announced)
    assert path.parent == prepared.data_dir.root  # no XDG_RUNTIME_DIR in this environment
    assert seen == [(True, 0o600)]
    assert not path.exists()
    assert h.opened == []


def test_claiming_the_session_removes_the_launch_file_t110(prepared: Prepared) -> None:
    h = Harness()
    gone: list[bool] = []

    def claim(server: FakeServer) -> None:
        path = launch_file(h.announced)
        token = re.search(r"#bootstrap=([A-Za-z0-9_-]+)", path.read_text())
        assert token and h.runtime
        assert isinstance(h.runtime.sessions.claim(token.group(1)), str)
        gone.append(not path.exists())
        quitting(h)(server)

    h.during = claim
    h.serve(prepared)
    assert gone == [True]


def test_the_launch_file_is_removed_when_the_token_expires_t110(prepared: Prepared) -> None:
    h = Harness(ttl=0.05)
    gone: list[bool] = []

    def wait_for_expiry(server: FakeServer) -> None:
        path = launch_file(h.announced)
        gone.append(wait_for(lambda: not path.exists()))
        quitting(h)(server)

    h.during = wait_for_expiry
    h.serve(prepared)
    assert gone == [True]


def test_the_launch_file_is_removed_when_start_up_fails_after_writing_it(prepared: Prepared) -> None:
    h = Harness()
    written: list[bool] = []

    def broken_announce(text: str) -> None:  # runs right after the launch file is written
        written.append(bool(list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*"))))
        raise RuntimeError("can't print the path")

    h.data = prepared.data_dir.root
    with pytest.raises(RuntimeError, match="print the path"):
        launcher.serve(
            prepared,
            env={},
            platform="linux",
            build=h.build,
            make_server=h.make_server,
            announce=broken_announce,
            exit_process=no_exit,
        )
    assert written == [True]
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []
    assert h.port is not None and not can_connect(h.port)


# --- The browser --------------------------------------------------------------------------------


def test_the_browser_is_opened_with_the_file_path_only_once_the_server_runs(prepared: Prepared) -> None:
    h = Harness()

    def wait_then_quit(server: FakeServer) -> None:
        assert wait_for(lambda: bool(h.opened))
        quitting(h)(server)

    h.during = wait_then_quit
    h.serve(with_browser(prepared))
    assert h.opened_while_started == [True]  # never before the server runs
    assert len(h.opened) == 1
    assert h.opened[0].name.startswith(launcher.BOOTSTRAP_PREFIX)
    assert h.announced == []


def test_the_real_opener_passes_only_the_file_uri_t103(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The token lives inside the 0600 file; the browser's argv (readable by other users) gets only
    # the file's URI, never the http URL with the #bootstrap fragment.
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    def no_other_way(*args: object, **kwargs: object) -> bool:
        raise AssertionError("open_in_browser must use webbrowser.open, so this test can stand in for it")

    monkeypatch.setattr("webbrowser.open", fake_open)
    for name in ("open_new", "open_new_tab", "get"):
        monkeypatch.setattr(f"webbrowser.{name}", no_other_way)  # never a real browser, even after a refactor
    path = tmp_path / f"{launcher.BOOTSTRAP_PREFIX}example.html"
    assert launcher.open_in_browser(path) is True
    assert opened == [path.as_uri()]
    assert opened[0].startswith("file://")
    assert "#" not in opened[0] and "bootstrap=" not in opened[0]


def test_if_no_browser_opens_the_path_is_printed(prepared: Prepared) -> None:
    h = Harness()
    h.browser_result = False

    def wait_then_quit(server: FakeServer) -> None:
        assert wait_for(lambda: bool(h.announced))
        quitting(h)(server)

    h.during = wait_then_quit
    h.serve(with_browser(prepared))
    assert h.announced[0].startswith("coinacct: open this file in your browser: ")


# --- Shutdown (§3, §6, T-405) -------------------------------------------------------------------


def test_later_steps_run_only_after_the_server_has_stopped(
    prepared: Prepared, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness()
    real_clear = datadir.clear_tmp

    def clear(data: datadir.DataDir) -> None:
        h.events.append("clear tmp")
        real_clear(data)

    monkeypatch.setattr(datadir, "clear_tmp", clear)
    h.during = quitting(h)
    assert h.serve(prepared) == 0
    assert h.events.index("server stopped") < h.events.index("clear tmp")


def test_the_temp_directory_is_cleared_at_shutdown(prepared: Prepared) -> None:
    h = Harness()

    def leave_a_temp_file(server: FakeServer) -> None:
        (prepared.data_dir.tmp / "upload.part").write_text("x")
        quitting(h)(server)

    h.during = leave_a_temp_file
    assert h.serve(prepared) == 0
    assert list(prepared.data_dir.tmp.iterdir()) == []


def test_a_lost_volume_shuts_the_app_down_through_the_watchdog_t405(prepared: Prepared) -> None:
    h = Harness()

    class LosingWatchdog(Watchdog):
        def problem(self) -> str | None:
            # Lost once the server runs; until then the volume is fine.
            if h.server is not None and h.server.started:
                return "the data directory is gone (volume dismounted?)"
            return None

    h.make_watchdog = functools.partial(LosingWatchdog, interval=0.01)
    assert h.serve(prepared) == 0  # the fake server stops only when the shutdown steps stop it
    assert h.runtime is not None
    assert h.runtime.shutdown.reason == "the data directory is gone (volume dismounted?)"


def test_a_volume_lost_during_the_node_checks_ends_the_start_up_t405(prepared: Prepared) -> None:
    # The watchdog runs before the node checks (architecture §8.1). The volume goes missing while
    # the (slow) checks run: the coordinator runs its steps at once, under its deadline, and its
    # last step ends the process; nothing listens and no launch file is written.
    h = Harness()
    gone = threading.Event()
    reported = threading.Event()

    class Watching(Watchdog):
        def problem(self) -> str | None:
            return "the data directory is gone (volume dismounted?)" if gone.is_set() else None

    def report_then(reason: str, on_lost: Any) -> None:
        on_lost(reason)
        reported.set()

    def make(data: datadir.DataDir, on_lost: Callable[[str], None]) -> Watchdog:
        return Watching(data, lambda reason: report_then(reason, on_lost), interval=0.01)

    def slow_check(*args: Any, **kwargs: Any) -> NodeStatus:
        gone.set()
        assert reported.wait(WAIT)  # the watchdog noticed while the checks were still running
        # The check is still "running": the process is ended without waiting for it.
        assert wait_for(lambda: h.exits == [launcher.LAUNCH_ERROR_EXIT])
        return ONLINE

    h.make_watchdog = make
    with pytest.raises(LaunchError, match="volume dismounted"):
        launcher.serve(
            prepared,
            env={},
            build=functools.partial(runtime.build, check=slow_check),
            make_watchdog=h.make_watchdog,
            announce=h.announced.append,
            exit_process=h.exits.append,
        )
    assert h.exits == [launcher.LAUNCH_ERROR_EXIT]
    assert h.announced == []
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_a_signal_during_the_node_checks_runs_the_steps_and_ends_the_start_up(
    prepared: Prepared, signum: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The handlers are installed before the node checks: no KeyboardInterrupt, no default action.
    exits: list[int] = []
    cleared: list[bool] = []
    real_clear = datadir.clear_tmp

    def clear(data: datadir.DataDir) -> None:
        cleared.append(True)
        real_clear(data)

    monkeypatch.setattr(datadir, "clear_tmp", clear)

    def check_then_signal(*args: Any, **kwargs: Any) -> NodeStatus:
        signal.raise_signal(signum)
        assert wait_for(lambda: exits == [launcher.LAUNCH_ERROR_EXIT])
        return ONLINE

    before = signal.getsignal(signum)
    with pytest.raises(LaunchError, match=signal.Signals(signum).name):
        launcher.serve(
            prepared,
            env={},
            build=functools.partial(runtime.build, check=check_then_signal),
            announce=no_announcement,
            exit_process=exits.append,
        )
    assert cleared == [True]  # the shutdown steps ran
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []
    assert signal.getsignal(signum) == before


def test_shutdown_asked_for_after_the_checks_ends_a_stalled_start_up_t405(prepared: Prepared) -> None:
    # Between the node checks and uvicorn's start, the main thread can block (making the server,
    # writing the launch file on a hung volume). A request then runs the steps, and the last step
    # ends the process instead of leaving it running with no deadline.
    h = Harness()

    def make_server(app: Any) -> FakeServer:
        assert h.runtime is not None
        h.runtime.shutdown.request("the data directory is gone (volume dismounted?)")
        assert wait_for(lambda: h.exits == [launcher.LAUNCH_ERROR_EXIT])  # "blocked" until ended
        return h.make_server(app)

    h.data = prepared.data_dir.root
    with pytest.raises(LaunchError, match="volume dismounted"):
        launcher.serve(
            prepared,
            env={},
            build=h.build,
            make_server=make_server,
            announce=h.announced.append,
            exit_process=h.exits.append,
        )
    assert h.exits == [launcher.LAUNCH_ERROR_EXIT]
    assert h.announced == []
    assert h.port is not None and not can_connect(h.port)
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


def test_a_stalled_launch_file_write_is_ended_too_t405(prepared: Prepared) -> None:
    # The same window, later: the request lands while the launch file is announced.
    h = Harness()

    def stalled_announce(text: str) -> None:
        assert h.runtime is not None
        h.runtime.shutdown.request("received SIGTERM")
        assert wait_for(lambda: h.exits == [launcher.LAUNCH_ERROR_EXIT])

    h.data = prepared.data_dir.root
    with pytest.raises(LaunchError, match="SIGTERM"):
        launcher.serve(
            prepared,
            env={},
            platform="linux",
            build=h.build,
            make_server=h.make_server,
            announce=stalled_announce,
            exit_process=h.exits.append,
        )
    assert h.exits == [launcher.LAUNCH_ERROR_EXIT]
    assert h.server is not None and not h.server.started  # uvicorn never ran
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


def test_a_really_removed_data_directory_shuts_down_cleanly_and_says_why_t405(
    prepared: Prepared, capfd: pytest.CaptureFixture[str]
) -> None:
    # The real thing, not a patched problem(): the directory goes away while serving. The temp
    # directory step leaves the path alone (it isn't the verified directory any more), every step
    # succeeds, and the reason reaches stderr, since the log file was on the lost volume.
    h = Harness()
    h.make_watchdog = functools.partial(Watchdog, interval=0.01)

    def remove(server: FakeServer) -> None:
        shutil.rmtree(prepared.data_dir.root)

    h.during = remove
    assert h.serve(prepared) == 0
    assert h.runtime is not None
    assert h.runtime.shutdown.reason == "the data directory is gone (volume dismounted?)"
    assert "shutting down: the data directory is gone" in capfd.readouterr().err
    assert h.exits == []


def test_a_volume_found_lost_right_after_the_checks_stops_start_up(prepared: Prepared) -> None:
    # Lost between two watchdog ticks: the synchronous check after the node checks catches it.
    class LostNow(Watchdog):
        def problem(self) -> str | None:
            return "the data directory was replaced or remounted"

    with pytest.raises(LaunchError, match="replaced or remounted"):
        launcher.serve(
            prepared,
            env={},
            build=functools.partial(
                runtime.build, check=lambda *a, **k: ONLINE, start_jobs=lambda *a, **k: None
            ),
            make_watchdog=functools.partial(LostNow, interval=3600),
            announce=lambda s: None,
            exit_process=no_exit,
        )
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


def test_a_server_that_stops_by_itself_still_runs_the_shutdown_steps(prepared: Prepared) -> None:
    h = Harness()

    def stop(server: FakeServer) -> None:
        server.should_exit = True

    h.during = stop
    assert h.serve(prepared) == 0
    assert h.runtime is not None and h.runtime.shutdown.reason == "the server stopped"
    assert not launch_file(h.announced).exists()


def test_the_chain_jobs_start_online_and_stop_after_the_server_before_the_db_closes(
    prepared: Prepared,
) -> None:
    h = Harness()

    def stop(server: FakeServer) -> None:
        server.should_exit = True

    h.during = stop
    assert h.serve(prepared) == 0
    assert h.events.index("check") < h.events.index("chain jobs started")
    assert h.events.index("server stopped") < h.events.index("chain jobs stopped")
    assert h.runtime is not None and h.runtime.start_chain_jobs is not None
    assert h.subjects_at_start == []  # nothing imported yet
    assert h.discover is discovery.extend_windows


def test_a_chain_job_that_never_stops_keeps_the_db_open(
    prepared: Prepared, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness()
    real = h.start_jobs

    def stuck(*args: Any, **kwargs: Any) -> Any:
        jobs = real(*args, **kwargs)
        type(jobs).stopped = False  # a job stuck on the node past its stop timeout
        return jobs

    monkeypatch.setattr(h, "start_jobs", stuck)

    def stop(server: FakeServer) -> None:
        server.should_exit = True

    h.during = stop
    assert h.serve(prepared) == 0
    db = h.db
    assert db is not None
    db.execute("SELECT 1")  # not closed under the job


def test_no_chain_jobs_start_when_shutdown_is_asked_for_during_start_up(prepared: Prepared) -> None:
    h = Harness()
    real = h.build

    def build_then_quit(**kwargs: Any) -> runtime.Runtime:
        rt = real(**kwargs)
        rt.shutdown.request("SIGTERM during start-up")
        return rt

    h.build = build_then_quit  # type: ignore[method-assign]
    with pytest.raises(LaunchError):
        h.serve(prepared)
    assert "chain jobs started" not in h.events  # §8.1: background chain work only after every check


def test_offline_mode_starts_no_chain_jobs(prepared: Prepared) -> None:
    h = Harness(status=NodeStatus(online=False, chain=None, reasons=("the node is down",)))

    def stop(server: FakeServer) -> None:
        server.should_exit = True

    h.during = stop
    assert h.serve(prepared) == 0
    assert "chain jobs started" not in h.events and "chain jobs stopped" not in h.events


# --- The real server's settings and signals -----------------------------------------------------


def test_uvicorn_runs_without_proxy_headers_server_header_access_log_or_websockets() -> None:
    cfg = launcher.uvicorn_config(object())
    assert cfg.proxy_headers is False  # X-Forwarded-* from a local client must not rewrite the client
    assert cfg.server_header is False
    assert cfg.access_log is False
    assert cfg.log_config is None  # only the redacting handler (T-403)
    assert cfg.ws == "none"
    assert cfg.lifespan == "off"


def test_uvicorn_gives_up_on_unfinished_requests_at_shutdown_t405() -> None:
    cfg = launcher.uvicorn_config(object())
    assert cfg.timeout_graceful_shutdown is not None
    assert cfg.timeout_graceful_shutdown < launcher.SERVER_STOP_SECONDS


def test_the_server_stop_wait_fits_inside_the_shutdown_deadline() -> None:
    assert launcher.SERVER_STOP_SECONDS < DEADLINE_SECONDS


def test_the_loopback_socket_is_bound_to_127_0_0_1_and_not_yet_listening_t103() -> None:
    sock = launcher.loopback_socket()
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
        assert not can_connect(port)
    finally:
        sock.close()


def test_uvicorn_installs_no_signal_handlers_of_its_own() -> None:
    server = launcher.Server(launcher.uvicorn_config(object()))
    before = signal.getsignal(signal.SIGTERM)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signals_ask_the_coordinator_and_a_second_one_forces_the_exit(signum: int) -> None:
    requests: list[str] = []

    class Target:
        force_exit = False

    target = Target()
    before = signal.getsignal(signum)

    def force() -> None:
        target.force_exit = True

    with launcher.shutdown_signals(requests.append, force):
        signal.raise_signal(signum)
        assert requests == [f"received {signal.Signals(signum).name}"]
        assert not target.force_exit
        signal.raise_signal(signum)
        assert target.force_exit
    assert signal.getsignal(signum) == before


def test_main_reports_a_launch_error_and_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert launcher.main(["--data-dir", str(tmp_path / "missing")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("coinacct: ")


def test_main_parses_no_browser() -> None:
    assert launcher.parse_options(["--no-browser"], {}).open_browser is False
    assert launcher.parse_options([], {}).open_browser is True

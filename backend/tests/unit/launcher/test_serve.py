"""Serving the app (architecture §3, §4, §6, §8.1; THREAT_MODEL T-103, T-110, T-401, T-402, T-405).

`serve` runs with a fake server that loops until it is told to stop, and with the node checks
replaced, so these tests need no node. The real thing, against a regtest node, is in
tests/integration/launcher.
"""

from __future__ import annotations

import functools
import os
import re
import signal
import socket
import stat
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coinacct import config, launcher
from coinacct.api import runtime
from coinacct.launcher import LaunchError, Prepared
from coinacct.services.lifecycle import DEADLINE_SECONDS
from coinacct.services.startup import NodeStatus, StorageRefused
from coinacct.storage import datadir
from coinacct.storage.volume import Encryption, VolumeStatus
from coinacct.storage.watchdog import Watchdog

CONFIG = '[rpc]\nhost = "127.0.0.1"\nport = 18443\nuser = "ro-client"\npassword = "serve-test-password"\n'
ONLINE = NodeStatus(online=True, chain="regtest", reasons=())
WAIT = 5.0


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

    def check(self, *args: Any, **kwargs: Any) -> NodeStatus:
        assert self.data is not None
        self.files_at_check = list(self.data.glob(f"{launcher.BOOTSTRAP_PREFIX}*"))
        self.events.append("check")
        return self.status

    def build(self, **kwargs: Any) -> runtime.Runtime:
        self.port = kwargs["port"]
        self.listening_at_check = can_connect(kwargs["port"])
        self.runtime = runtime.build(check=self.check, ttl=self.ttl, **kwargs)
        return self.runtime

    def make_server(self, app: Any) -> FakeServer:
        self.server = FakeServer(app, self.events)
        self.server.during = self.during
        return self.server

    def open_browser(self, path: Path) -> bool:
        assert self.server is not None and self.server.started  # never before the server runs
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
        )


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
            prepared, env={}, build=functools.partial(runtime.build, check=refuse), announce=lambda s: None
        )
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


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
    class BrokenWatchdog(Watchdog):
        def start(self) -> None:
            raise RuntimeError("can't start the watchdog")

    h = Harness()
    h.make_watchdog = BrokenWatchdog
    with pytest.raises(RuntimeError, match="watchdog"):
        h.serve(prepared)
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
    assert len(h.opened) == 1
    assert h.opened[0].name.startswith(launcher.BOOTSTRAP_PREFIX)
    assert h.announced == []


def test_if_no_browser_opens_the_path_is_printed(prepared: Prepared) -> None:
    h = Harness()
    h.browser_result = False

    def wait_then_quit(server: FakeServer) -> None:
        assert wait_for(lambda: bool(h.announced))
        quitting(h)(server)

    h.during = wait_then_quit
    h.serve(with_browser(prepared))
    assert h.announced[0].startswith("coinacct: open this file in your browser: ")


def test_the_browser_doesnt_inherit_the_volumes_temp_directory_t402(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str | None, str | None]] = []

    def fake_open(url: str) -> bool:
        seen.append((os.environ.get("TMPDIR"), os.environ.get("SQLITE_TMPDIR")))
        return True

    monkeypatch.setenv("TMPDIR", "/volume/data/tmp")
    monkeypatch.setenv("SQLITE_TMPDIR", "/volume/data/tmp")
    monkeypatch.setattr(webbrowser, "open", fake_open)
    assert launcher.open_in_browser(Path("/volume/data/coinacct-bootstrap-x.html"))
    assert seen == [(None, None)]
    assert os.environ["TMPDIR"] == "/volume/data/tmp"  # put back for the app itself


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
    class LosingWatchdog(Watchdog):
        def problem(self) -> str | None:
            return "the data directory is gone (volume dismounted?)"

    h = Harness()
    h.make_watchdog = functools.partial(LosingWatchdog, interval=0.01)
    assert h.serve(prepared) == 0  # the fake server stops only when the shutdown steps stop it
    assert h.runtime is not None
    assert h.runtime.shutdown.reason == "the data directory is gone (volume dismounted?)"


def test_a_server_that_stops_by_itself_still_runs_the_shutdown_steps(prepared: Prepared) -> None:
    h = Harness()

    def stop(server: FakeServer) -> None:
        server.should_exit = True

    h.during = stop
    assert h.serve(prepared) == 0
    assert h.runtime is not None and h.runtime.shutdown.reason == "the server stopped"
    assert not launch_file(h.announced).exists()


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
    with launcher.shutdown_signals(requests.append, target, lambda: bool(requests)):
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

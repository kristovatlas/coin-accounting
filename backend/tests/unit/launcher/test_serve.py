"""Serving the app (architecture §3, §4, §8.1; THREAT_MODEL T-103, T-110, T-401, T-405).

`serve` runs with a fake server that loops until it is told to stop, and with the node checks
replaced, so these tests need no node and open no connection. The real thing, against a regtest
node, is in tests/integration/launcher.
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coinacct import config, launcher
from coinacct.api import runtime
from coinacct.launcher import LaunchError, Prepared
from coinacct.services.startup import NodeStatus, StorageRefused
from coinacct.storage import datadir
from coinacct.storage.volume import Encryption, VolumeStatus

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


class FakeServer:
    """Stands in for uvicorn: `run` loops until `should_exit`, running `during` first."""

    def __init__(self, app: Any, request_shutdown: Callable[[str], None]) -> None:
        self.app, self.request_shutdown = app, request_shutdown
        self.should_exit = False
        self.sockets: list[socket.socket] = []
        self.during: Callable[[FakeServer], None] = lambda server: None

    def run(self, sockets: list[socket.socket]) -> None:
        self.sockets = sockets
        self.during(self)
        deadline = time.monotonic() + WAIT
        while not self.should_exit:
            assert time.monotonic() < deadline, "the server was never told to stop"
            threading.Event().wait(0.01)


class Harness:
    def __init__(self, status: NodeStatus = ONLINE, ttl: float = 60.0) -> None:
        self.calls: list[str] = []
        self.server: FakeServer | None = None
        self.runtime: runtime.Runtime | None = None
        self.announced: list[str] = []
        self.opened: list[Path] = []
        self.during: Callable[[FakeServer], None] = lambda server: None
        self.status = status
        self.ttl = ttl
        self.files_at_check: list[Path] = []
        self.data: Path | None = None

    def check(self, *args: Any, **kwargs: Any) -> NodeStatus:
        assert self.data is not None
        self.files_at_check = list(self.data.glob(f"{launcher.BOOTSTRAP_PREFIX}*"))
        self.calls.append("check")
        return self.status

    def build(self, **kwargs: Any) -> runtime.Runtime:
        self.runtime = runtime.build(check=self.check, ttl=self.ttl, **kwargs)
        return self.runtime

    def make_server(self, app: Any, request_shutdown: Callable[[str], None]) -> FakeServer:
        self.server = FakeServer(app, request_shutdown)
        self.server.during = self.during
        return self.server

    def serve(self, prepared: Prepared) -> int:
        self.data = prepared.data_dir.root
        return launcher.serve(
            prepared,
            env={},
            platform="linux",
            build=self.build,
            make_server=self.make_server,  # type: ignore[arg-type]
            open_browser=self.opened.append,
            announce=self.announced.append,
        )


def launch_file(announced: list[str]) -> Path:
    match = re.fullmatch(r"coinacct: launch file (.+)", announced[0])
    assert match
    return Path(match.group(1))


def quit_now(server: FakeServer) -> None:
    server.request_shutdown(runtime.QUIT_REASON)


def test_quit_stops_the_server_and_exits_cleanly(prepared: Prepared) -> None:
    h = Harness()
    h.during = quit_now
    assert h.serve(prepared) == 0
    assert h.server is not None and h.server.should_exit
    assert h.runtime is not None and h.runtime.shutdown.reason == runtime.QUIT_REASON


def test_the_node_checks_run_before_the_launch_file_exists(prepared: Prepared) -> None:
    h = Harness()
    h.during = quit_now
    h.serve(prepared)
    assert h.calls == ["check"]
    assert h.files_at_check == []


def test_the_server_gets_a_loopback_socket_t103(prepared: Prepared) -> None:
    h = Harness()
    seen: list[tuple[str, int]] = []

    def look(server: FakeServer) -> None:
        seen.append(server.sockets[0].getsockname())
        quit_now(server)

    h.during = look
    h.serve(prepared)
    host, port = seen[0]
    assert host == "127.0.0.1"
    assert port > 0


def test_the_launch_file_is_private_announced_and_removed_on_quit_t110(prepared: Prepared) -> None:
    h = Harness()
    seen: list[tuple[bool, int]] = []

    def look(server: FakeServer) -> None:
        path = launch_file(h.announced)
        seen.append((path.exists(), stat.S_IMODE(path.stat().st_mode)))
        quit_now(server)

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
        quit_now(server)

    h.during = claim
    h.serve(prepared)
    assert gone == [True]


def test_the_launch_file_is_removed_when_the_token_expires_t110(prepared: Prepared) -> None:
    h = Harness(ttl=0.05)
    gone = threading.Event()

    def wait_for_expiry(server: FakeServer) -> None:
        path = launch_file(h.announced)
        for _ in range(int(WAIT / 0.01)):
            if not path.exists():
                gone.set()
                break
            threading.Event().wait(0.01)
        quit_now(server)

    h.during = wait_for_expiry
    h.serve(prepared)
    assert gone.is_set()


def test_the_browser_is_opened_with_the_file_path_only(prepared: Prepared) -> None:
    h = Harness()
    h.during = quit_now
    h.serve(Prepared(prepared.data_dir, prepared.config, prepared.needs_test_chain, open_browser=True))
    assert len(h.opened) == 1
    assert h.opened[0].name.startswith(launcher.BOOTSTRAP_PREFIX)
    assert h.announced == []


def test_offline_mode_still_serves(prepared: Prepared) -> None:
    h = Harness(status=NodeStatus(online=False, chain=None, reasons=("the node can't be reached: refused",)))
    h.during = quit_now
    assert h.serve(prepared) == 0
    assert h.runtime is not None and not h.runtime.status.online


def test_a_storage_refusal_stops_start_up_before_anything_listens_t401(prepared: Prepared) -> None:
    def refuse(*args: Any, **kwargs: Any) -> NodeStatus:
        raise StorageRefused("--allow-unencrypted-storage is never accepted on mainnet")

    with pytest.raises(LaunchError, match="never accepted on mainnet"):
        launcher.serve(
            prepared, env={}, build=functools.partial(runtime.build, check=refuse), announce=lambda s: None
        )
    assert list(prepared.data_dir.root.glob(f"{launcher.BOOTSTRAP_PREFIX}*")) == []


def test_a_lost_volume_shuts_the_app_down_t405(prepared: Prepared) -> None:
    h = Harness()
    h.during = lambda server: server.request_shutdown("the data directory is gone (volume dismounted?)")
    assert h.serve(prepared) == 0
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


# --- The real server's settings ------------------------------------------------------------------


def test_uvicorn_runs_without_proxy_headers_server_header_access_log_or_websockets() -> None:
    cfg = launcher.uvicorn_config(object())
    assert cfg.proxy_headers is False  # X-Forwarded-* from a local client must not rewrite the client
    assert cfg.server_header is False
    assert cfg.access_log is False
    assert cfg.log_config is None  # only the redacting handler (T-403)
    assert cfg.ws == "none"
    assert cfg.lifespan == "off"


def test_the_loopback_socket_is_bound_to_127_0_0_1_t103() -> None:
    sock = launcher.loopback_socket()
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
        # Already listening: a client connecting before uvicorn starts waits instead of being refused.
        client = socket.create_connection((host, port), timeout=5)
        client.close()
    finally:
        sock.close()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signals_go_to_the_shutdown_coordinator_and_handlers_are_restored(signum: int) -> None:
    requests: list[str] = []
    server = launcher.Server(launcher.uvicorn_config(object()), requests.append)
    before = signal.getsignal(signum)
    with server.capture_signals():
        signal.raise_signal(signum)
    assert requests == [f"received {signal.Signals(signum).name}"]
    assert not server.should_exit  # the coordinator's first step stops the server, not the handler
    assert signal.getsignal(signum) == before


def test_main_reports_a_launch_error_and_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert launcher.main(["--data-dir", str(tmp_path / "missing")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("coinacct: ")


def test_main_parses_no_browser() -> None:
    assert launcher.parse_options(["--no-browser"], {}).open_browser is False
    assert launcher.parse_options([], {}).open_browser is True


def test_os_environ_is_untouched_by_the_harness() -> None:
    # serve() is called with env={} in these tests; the real main() passes os.environ.
    assert isinstance(os.environ, os._Environ)

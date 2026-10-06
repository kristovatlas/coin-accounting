"""The real launcher against a regtest node (architecture §3, §4, §8.1; THREAT_MODEL T-101, T-103, T-110,
T-405).

Each test starts `python -m coinacct.launcher --no-browser` in its own process, reads the launch
file it announces, and talks to it over loopback HTTP as the browser would: claim the session, read
the status, quit. This is the whole start-up and shutdown path, with uvicorn and real signals.
"""

from __future__ import annotations

import http.client
import json
import re
import select
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.regtest import RegtestNode, regtest_node

BACKEND = Path(__file__).resolve().parents[3]
WAIT = 60


def write_config(data: Path, node: RegtestNode, port: int | None = None) -> None:
    text = (
        f'[rpc]\nhost = "127.0.0.1"\nport = {port or node.port}\n'
        f'user = "{node.app_user}"\npassword = "{node.app_password}"\n'
    )
    (data / "config.toml").write_text(text)
    (data / "config.toml").chmod(0o600)


class App:
    def __init__(self, data: Path) -> None:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(data.parent), "PYTHONPATH": str(BACKEND)}
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv; this interpreter
            [
                sys.executable,
                "-m",
                "coinacct.launcher",
                "--data-dir",
                str(data),
                "--allow-unencrypted-storage",
                "--no-browser",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], WAIT)
        if not ready:
            self.proc.kill()
            pytest.fail(f"the launcher announced nothing within {WAIT} s")
        line = self.proc.stdout.readline()
        match = re.fullmatch(r"coinacct: launch file (.+)\n", line)
        assert match, f"no launch file announced: {line!r} {self.stderr()}"
        self.launch_file = Path(match.group(1))
        page = self.launch_file.read_text()
        found = re.search(r"http://127\.0\.0\.1:(\d+)/#bootstrap=([A-Za-z0-9_-]+)", page)
        assert found
        self.port, self.token = int(found.group(1)), found.group(2)

    def stderr(self) -> str:
        if self.proc.poll() is None:
            return ""
        assert self.proc.stderr is not None
        return str(self.proc.stderr.read())

    def request(
        self, method: str, path: str, body: object = None, session: str | None = None, host: str | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if session:
            headers["Authorization"] = f"Bearer {session}"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def claim(self) -> str:
        status, _, body = self.request("POST", "/api/session", {"bootstrap": self.token})
        assert status == 200, body
        return str(json.loads(body)["session"])

    def wait(self) -> int:
        return self.proc.wait(timeout=WAIT)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def app(data: Path, node: RegtestNode) -> Iterator[App]:
    write_config(data, node)
    running = App(data)
    yield running
    if running.proc.poll() is None:
        running.proc.kill()
        running.proc.wait(timeout=WAIT)


def test_the_app_starts_online_serves_the_session_and_quits_cleanly(app: App) -> None:
    assert app.launch_file.exists()
    session = app.claim()
    assert not app.launch_file.exists()  # removed on claim (T-110)
    status, headers, body = app.request("GET", "/api/status", session=session)
    assert status == 200
    assert json.loads(body) == {"online": True, "chain": "regtest", "reasons": []}
    assert "server" not in headers  # no uvicorn Server header
    assert headers["content-security-policy"].startswith("default-src 'self'")
    status, _, _ = app.request("POST", "/api/quit", {}, session=session)
    assert status == 202
    assert app.wait() == 0, app.stderr()


def test_sigterm_shuts_down_cleanly_t405(app: App) -> None:
    app.claim()
    app.proc.send_signal(signal.SIGTERM)
    assert app.wait() == 0, app.stderr()


def test_a_second_claim_and_a_wrong_host_are_refused_t101_t110(app: App) -> None:
    session = app.claim()
    status, _, body = app.request("POST", "/api/session", {"bootstrap": app.token})
    assert status == 409
    assert json.loads(body) == {"error": "Session already claimed. Restart the app."}
    status, _, _ = app.request("GET", "/api/status", session=session, host=f"localhost:{app.port}")
    assert status == 421
    app.proc.send_signal(signal.SIGINT)
    assert app.wait() == 0, app.stderr()


def test_the_server_listens_on_loopback_only_t103(app: App) -> None:
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["/usr/bin/env", "ss", "-ltnH", f"sport = :{app.port}"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        pytest.skip("ss is not available")
    listeners = [line.split()[3] for line in out.stdout.splitlines() if line.strip()]
    assert listeners == [f"127.0.0.1:{app.port}"]
    app.proc.send_signal(signal.SIGTERM)
    assert app.wait() == 0


def test_a_node_failing_a_check_means_offline_mode_not_a_failed_start(data: Path, tmp_path: Path) -> None:
    # The node answers (so the chain is known to be regtest) but has no rpcwhitelist (T-203).
    with regtest_node(tmp_path / "open-node", whitelist=None) as open_node:
        write_config(data, open_node)
        running = App(data)
        try:
            session = running.claim()
            status, _, body = running.request("GET", "/api/status", session=session)
            result = json.loads(body)
            assert status == 200
            assert result["online"] is False
            assert result["chain"] == "regtest"
            assert any("rpcwhitelist isn't active" in reason for reason in result["reasons"])
        finally:
            running.proc.send_signal(signal.SIGTERM)
            assert running.wait() == 0, running.stderr()


def test_unencrypted_storage_with_an_unreachable_node_is_refused_t401(data: Path, node: RegtestNode) -> None:
    # Nothing shows the chain is a test chain, so --allow-unencrypted-storage can't apply.
    write_config(data, node, port=1)  # nothing listens there
    env = {"PATH": "/usr/bin:/bin", "HOME": str(data.parent), "PYTHONPATH": str(BACKEND)}
    done = subprocess.run(  # noqa: S603 - fixed argv; this interpreter
        [
            sys.executable,
            "-m",
            "coinacct.launcher",
            "--data-dir",
            str(data),
            "--allow-unencrypted-storage",
            "--no-browser",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=WAIT,
        check=False,
    )
    assert done.returncode == 2
    assert "never accepted on mainnet, or when the chain isn't known yet (T-401)" in done.stderr
    assert done.stdout == ""  # nothing was announced, and nothing listened
    assert list(data.glob("coinacct-bootstrap-*")) == []


def test_the_log_is_written_to_the_data_volume(app: App, data: Path) -> None:
    app.claim()
    app.proc.send_signal(signal.SIGTERM)
    assert app.wait() == 0
    logs = list((data / "logs").glob("*"))
    assert logs
    text = "".join(p.read_text() for p in logs)
    assert "serving on 127.0.0.1:" in text
    assert app.token not in text  # T-403/T-110: the launch token is never logged


def test_a_stalled_request_cant_keep_the_app_running_after_shutdown_t405(app: App) -> None:
    # A local client sends headers promising a body and never sends it. Shutdown must still finish:
    # uvicorn gives up on unfinished requests, and the coordinator's deadline backs that up.
    stalled = socket.create_connection(("127.0.0.1", app.port), timeout=30)
    try:
        stalled.sendall(
            f"POST /api/session HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{".encode()
        )
        started = time.monotonic()
        app.proc.send_signal(signal.SIGTERM)
        assert app.wait() == 0, app.stderr()
        assert time.monotonic() - started < 15
    finally:
        stalled.close()

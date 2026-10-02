"""Start a throwaway regtest `bitcoind` configured the way the app requires (ADR 0004):

- the app's own `rpcauth` user, limited by `rpcwhitelist` to the T-203 read-only methods
- a separate harness user with full access, for mining and other test setup
- `txindex`, `blockfilterindex` and `txospenderindex` on
- loopback RPC only, and no P2P networking at all (the socket guard allows loopback only)

Each option can be turned off to build the misconfigured nodes the checks must reject. The binary is
the pinned, verified one from `make test-tools` (ENGINEERING §2.3).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[2]
BITCOIND: Final = REPO / ".toolchain" / "bin" / "bitcoind"

APP_USER: Final = "ro-client"
HARNESS_USER: Final = "harness"
# THREAT_MODEL T-203: what the app's rpcauth user may call.
APP_WHITELIST: Final = (
    "getblockchaininfo",
    "getnetworkinfo",
    "getindexinfo",
    "getblockcount",
    "getbestblockhash",
    "getblockhash",
    "getblockheader",
    "getblock",
    "getrawtransaction",
    "gettxout",
    "gettxspendingprevout",
    "scanblocks",
    "getdescriptoractivity",
    "getchaintips",
    "deriveaddresses",
    "getdescriptorinfo",
)
INDEXES: Final = ("txindex", "blockfilterindex", "txospenderindex")
# P2WSH(OP_TRUE) on regtest, from Bitcoin Core's functional tests (test_framework/address.py). Coins
# mined to it are spendable by anyone, so it is fine as a public test fixture.
OP_TRUE_ADDRESS: Final = "bcrt1qft5p2uhsdcdc3l2ua4ap5qqfg4pjaqlp250x7us7a8qqhrxrxfsqseac85"
STARTUP_TIMEOUT: Final = 60.0
READY_LINE: Final = "init message: Done loading"


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegtestNode:
    datadir: Path
    port: int
    app_user: str
    app_password: str
    process: subprocess.Popen[str]

    def admin(self, method: str, params: Sequence[Any] = ()) -> Any:
        """Call any method as the harness user (test setup only; never the app's path)."""
        return _admin_call(self.port, self._harness_password, method, list(params))

    def mine(self, blocks: int) -> list[str]:
        result = self.admin("generatetoaddress", [blocks, OP_TRUE_ADDRESS])
        assert isinstance(result, list)
        return result

    @property
    def _harness_password(self) -> str:
        return (self.datadir / "harness.password").read_text().strip()

    @property
    def debug_log(self) -> Path:
        return self.datadir / "regtest" / "debug.log"


def verify_pinned_bitcoind() -> None:
    """The binary must be the pinned, unmodified one (ENGINEERING §2.3)."""
    if not BITCOIND.exists():
        raise HarnessError("regtest bitcoind is missing: run 'make test-tools'")
    result = subprocess.run(  # noqa: S603 - fixed argv, our own verifier
        [sys.executable, str(REPO / "scripts" / "toolchain.py"), "verify", "bitcoind"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"bitcoind isn't the pinned install: {result.stderr.strip()}")


def rpcauth_line(user: str, password: str) -> str:
    # The format of Core's share/rpcauth/rpcauth.py: user:salt$HMAC-SHA256(salt, password).
    salt = secrets.token_hex(16)
    digest = hmac.new(salt.encode(), password.encode(), hashlib.sha256).hexdigest()
    return f"{user}:{salt}${digest}"


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def regtest_node(
    datadir: Path,
    *,
    whitelist: Iterable[str] | None = APP_WHITELIST,
    indexes: Iterable[str] = INDEXES,
    extra_args: Sequence[str] = (),
) -> Iterator[RegtestNode]:
    """Run a node for the duration of the block. `whitelist=None` omits rpcwhitelist entirely."""
    verify_pinned_bitcoind()
    datadir.mkdir(parents=True, exist_ok=True)
    app_password, harness_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    (datadir / "harness.password").write_text(harness_password)
    port = free_loopback_port()
    # Credentials go in bitcoin.conf, not argv: other local users can read a process's arguments.
    conf = [
        "regtest=1",
        "server=1",
        "[regtest]",
        f"rpcport={port}",
        "rpcbind=127.0.0.1",
        "rpcallowip=127.0.0.1",
        f"rpcauth={rpcauth_line(APP_USER, app_password)}",
        f"rpcauth={rpcauth_line(HARNESS_USER, harness_password)}",
        # No P2P at all: nothing listens, nothing connects, no seeds, no Tor control, no discovery.
        "listen=0",
        "connect=0",
        "dnsseed=0",
        "fixedseeds=0",
        "listenonion=0",
        "discover=0",
        "networkactive=0",
        "printtoconsole=1",
    ]
    conf += [f"{index}=1" for index in indexes]
    if whitelist is not None:
        conf.append(f"rpcwhitelist={APP_USER}:{','.join(whitelist)}")
        # Once any rpcwhitelist is set, Core gives every other user an empty whitelist unless
        # rpcwhitelistdefault=0. The harness user needs full access for mining; the app's user
        # keeps its own whitelist either way.
        conf.append("rpcwhitelistdefault=0")
    (datadir / "bitcoin.conf").write_text("\n".join(conf) + "\n")
    (datadir / "bitcoin.conf").chmod(0o600)
    process = subprocess.Popen(  # noqa: S603 - fixed argv; the pinned binary
        [str(BITCOIND), f"-datadir={datadir}", *extra_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={"HOME": str(datadir), "PATH": "/usr/bin:/bin"},
    )
    try:
        _wait_until_ready(process)
        yield RegtestNode(datadir, port, APP_USER, app_password, process)
    finally:
        _stop(process)


def _wait_until_ready(process: subprocess.Popen[str]) -> None:
    ready, lines = threading.Event(), list[str]()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            if READY_LINE in line:
                ready.set()
        ready.set()  # the process exited

    threading.Thread(target=read, daemon=True).start()
    if not ready.wait(STARTUP_TIMEOUT) or process.poll() is not None:
        _stop(process)
        tail = "".join(lines[-15:])
        raise HarnessError(f"regtest bitcoind didn't start:\n{tail}")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=STARTUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _admin_call(port: int, password: str, method: str, params: list[Any]) -> Any:
    token = base64.b64encode(f"{HARNESS_USER}:{password}".encode()).decode()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(
            "POST", "/", body, {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        )
        response = conn.getresponse()
        reply = json.loads(response.read())
    finally:
        conn.close()
    if reply.get("error"):
        raise HarnessError(f"{method}: {reply['error']}")
    return reply["result"]


def datadir_files(datadir: Path) -> Iterator[Path]:
    for root, _, files in os.walk(datadir):
        for name in files:
            yield Path(root) / name

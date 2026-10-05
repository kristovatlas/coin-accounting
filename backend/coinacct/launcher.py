"""Process start-up (architecture §1, §3, §4, §8.1). Runs in the same process as the server.

This file holds everything that happens before the web server starts:

1. **Hardening first** (T-404): no core dumps (`RLIMIT_CORE=0`), not dumpable or ptrace-attachable by
   other processes of this user on Linux (`PR_SET_DUMPABLE=0`), and a 077 umask so every file
   the app creates is private.
2. **Options:** `--data-dir` or `COINACCT_DATA_DIR` (no default, T-401), `--allow-unencrypted-storage`
   and, on macOS, `--confirm-encrypted-volume` (T-401's per-path confirmation).
3. **Storage:** verify the data directory and its volume; temp files go to `<data>/tmp` (T-402).
4. **Logging:** the redacting file handler, and exception hooks for the main thread and every other
   thread, so tracebacks are redacted too (T-403).
5. **Config:** read `<data>/config.toml` through `storage/` and parse it with `config.py`.

Then `serve` (§3, §4, §8.1):

6. **Node checks** through `api.runtime` → `services/` → `chain/`, before anything listens. A node
   problem means offline mode; the deferred storage policy (T-401) can still stop start-up here.
7. **Listen** on a socket bound to `127.0.0.1` with a port the OS picks (T-103), and run uvicorn
   in-process on it: no proxy headers, no `Server` header, no access log, no WebSockets.
8. **Shutdown wiring:** SIGINT, SIGTERM, the dismount watchdog (T-405) and Quit all go to one
   `Shutdown` coordinator, whose steps stop the server and the watchdog, remove the bootstrap file
   and flush the logs. uvicorn's own signal handling is replaced, so a signal never skips them.
9. **Launch:** write the bootstrap file (§4, T-110), delete it on claim or after 60 s, and open it
   in the default browser (or print its path with `--no-browser`, for the E2E harness).
"""

from __future__ import annotations

import argparse
import ctypes
import html
import logging
import os
import resource
import secrets
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import webbrowser
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import uvicorn

from coinacct import config
from coinacct.api import runtime
from coinacct.storage import config_file, datadir, volume
from coinacct.storage.logfile import open_log_handler
from coinacct.storage.watchdog import Watchdog

log = logging.getLogger(__name__)

PR_SET_DUMPABLE: Final = 4  # <linux/prctl.h>
DATA_DIR_ENV: Final = "COINACCT_DATA_DIR"
BOOTSTRAP_PREFIX: Final = "coinacct-bootstrap-"
LISTEN_BACKLOG: Final = 64


class LaunchError(Exception):
    """Start-up can't continue. The message says what to fix and holds no secrets."""


@dataclass(frozen=True)
class Options:
    data_dir: str | None
    allow_unencrypted_storage: bool
    confirm_encrypted_volume: bool
    open_browser: bool = True


@dataclass(frozen=True)
class Prepared:
    data_dir: datadir.DataDir
    config: config.Config
    # True when the volume isn't encrypted and --allow-unencrypted-storage was given: the app must
    # still refuse to run unless the node turns out to be on regtest, signet or testnet (T-401).
    needs_test_chain: bool
    open_browser: bool = True


def harden(platform: str = sys.platform) -> None:
    """T-404. Fails closed: if a step can't be applied, start-up stops."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise LaunchError(f"prctl(PR_SET_DUMPABLE, 0) failed: {os.strerror(ctypes.get_errno())}")
    os.umask(0o077)


def parse_options(argv: Sequence[str], env: Mapping[str, str]) -> Options:
    parser = argparse.ArgumentParser(prog="coinacct", allow_abbrev=False)
    parser.add_argument(
        "--data-dir", help=f"the data directory on your encrypted volume (or set {DATA_DIR_ENV})"
    )
    parser.add_argument(
        "--allow-unencrypted-storage",
        action="store_true",
        help="regtest, signet and testnet only: allow a data directory that isn't encrypted",
    )
    parser.add_argument(
        "--confirm-encrypted-volume",
        action="store_true",
        help="macOS: confirm once that the data directory is on an encrypted volume",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="don't open the browser; print the launch file's path instead (for the E2E harness)",
    )
    args = parser.parse_args(list(argv))
    return Options(
        data_dir=args.data_dir or env.get(DATA_DIR_ENV) or None,
        allow_unencrypted_storage=args.allow_unencrypted_storage,
        confirm_encrypted_volume=args.confirm_encrypted_volume,
        open_browser=not args.no_browser,
    )


def use_temp_dir(tmp: Path) -> None:
    """T-402: anything that spills to a temp file (uploads, SQLite) lands on the volume."""
    os.environ["TMPDIR"] = str(tmp)
    os.environ["SQLITE_TMPDIR"] = str(tmp)
    tempfile.tempdir = str(tmp)


def install_logging(handler: logging.Handler) -> None:
    """T-403: one redacting handler for everything, and exception hooks that log through it."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    # A record no handler takes (a logger with propagate=False, as libraries set up) would otherwise
    # go to logging.lastResort: stderr, unredacted.
    logging.lastResort = handler
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)

    def excepthook(kind: type[BaseException], value: BaseException, tb: types.TracebackType | None) -> None:
        logging.getLogger("coinacct.crash").critical("unhandled exception", exc_info=(kind, value, tb))
        sys.stderr.write("coinacct: unexpected error; details are in the log on the data volume\n")

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        name = args.thread.name if args.thread else "?"
        crash = logging.getLogger("coinacct.crash")
        if args.exc_value is None:
            crash.critical("thread %s ended with %s", name, args.exc_type.__name__)
        else:
            crash.critical(
                "unhandled exception in thread %s",
                name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def prepare(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    hardening: Callable[[], None] = harden,
    platform: str = sys.platform,
) -> Prepared:
    hardening()
    options = parse_options(argv, env)
    try:
        data = datadir.open_data_dir(options.data_dir)
    except datadir.DataDirError as e:
        raise LaunchError(str(e)) from None
    if options.confirm_encrypted_volume:
        if platform != "darwin":
            raise LaunchError(
                "--confirm-encrypted-volume is for macOS; on Linux the volume is detected (T-401)"
            )
        volume.write_confirmation(data.root)
        data = datadir.open_data_dir(options.data_dir)
    refusal = datadir.storage_refusal(data.volume, None, options.allow_unencrypted_storage)
    needs_test_chain = False
    if refusal is not None:
        if not options.allow_unencrypted_storage:
            raise LaunchError(refusal)
        needs_test_chain = True  # decided once the node's chain is known
    use_temp_dir(data.tmp)
    datadir.clear_tmp(data)
    install_logging(open_log_handler(data))
    if data.volume.lacks_permission_bits:
        log.warning(
            "the data directory's filesystem (%s) has no permission bits; file modes can't protect it",
            data.volume.fs_type,
        )
    if needs_test_chain:
        log.warning(
            "unencrypted storage allowed for now; it will be refused unless the node is on a test chain"
        )
    try:
        parsed = config.parse(config_file.read_config_text(data))
    except (config.ConfigError, config_file.ConfigFileError) as e:
        raise LaunchError(str(e)) from None
    return Prepared(
        data_dir=data, config=parsed, needs_test_chain=needs_test_chain, open_browser=options.open_browser
    )


def bootstrap_dir(env: Mapping[str, str], data: datadir.DataDir, platform: str = sys.platform) -> Path:
    """Architecture §4: on Linux, `$XDG_RUNTIME_DIR` (per-user, RAM-backed) when it's safe; macOS,
    and Linux without it, use `<data>` on the volume."""
    runtime = env.get("XDG_RUNTIME_DIR") if platform.startswith("linux") else None
    if runtime:
        path = Path(runtime)
        try:
            st = path.lstat()
        except OSError:
            st = None
        if st and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077:
            return path
    return data.root


def new_bootstrap_token() -> str:
    return secrets.token_urlsafe(32)


def write_bootstrap_file(directory: Path, port: int, token: str) -> Path:
    """Architecture §4: an HTML file (mode 0600) that sends the browser to the app with the one-time
    token in the URL fragment, which is never sent to a server or in a Referer. No script."""
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    url = html.escape(f"http://127.0.0.1:{port}/#bootstrap={token}", quote=True)
    page = (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="0; url={url}">'
        "<title>Coin Accounting</title></head><body>Opening Coin Accounting…</body></html>\n"
    )
    path = directory / f"{BOOTSTRAP_PREFIX}{secrets.token_hex(8)}.html"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def loopback_socket() -> socket.socket:
    """T-103: a socket bound to 127.0.0.1 only, on a port the OS picks. Bound here and handed to
    uvicorn, so the port is known before the bootstrap file is written and can't be taken first.
    It listens already, so a browser that is quicker than uvicorn's start waits in the backlog
    instead of being refused."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(LISTEN_BACKLOG)
    except OSError:
        sock.close()
        raise
    return sock


class Server(uvicorn.Server):
    """uvicorn's server, with its signal handling replaced: SIGINT and SIGTERM ask the shutdown
    coordinator, whose first step stops this server. uvicorn would otherwise stop by itself and then
    re-raise the signal, ending the process before the DB is closed and the logs flushed."""

    def __init__(self, config: uvicorn.Config, request_shutdown: Callable[[str], None]) -> None:
        super().__init__(config)
        self._request_shutdown = request_shutdown

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        def handler(signum: int, frame: types.FrameType | None) -> None:
            self._request_shutdown(f"received {signal.Signals(signum).name}")

        previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)


def uvicorn_config(app: Any) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        log_config=None,  # our redacting handler only (T-403)
        access_log=False,  # request lines would put paths and timing in the log for nothing
        proxy_headers=False,  # nothing sits in front; X-Forwarded-* must not rewrite the client
        server_header=False,
        date_header=False,
        lifespan="off",
        ws="none",
        http="h11",
        loop="asyncio",
    )


def remove_file(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def print_flushed(text: str) -> None:
    """stdout is a pipe under the E2E harness, where print() would wait in a buffer."""
    print(text, flush=True)  # noqa: T201 - the one line --no-browser promises


def open_in_browser(path: Path) -> None:
    """§4: the browser gets the file's path only; the token is inside the 0600 file."""
    webbrowser.open(path.as_uri())


def serve(  # noqa: PLR0913 - the parts are injectable for the tests
    prepared: Prepared,
    *,
    env: Mapping[str, str],
    platform: str = sys.platform,
    build: Callable[..., runtime.Runtime] = runtime.build,
    make_server: Callable[[Any, Callable[[str], None]], Server] = lambda app, req: Server(
        uvicorn_config(app), req
    ),
    open_browser: Callable[[Path], None] = open_in_browser,
    announce: Callable[[str], None] = print_flushed,
) -> int:
    """Run the app until it shuts down. Returns the process exit code."""
    data = prepared.data_dir
    sock = loopback_socket()
    port = sock.getsockname()[1]
    token = new_bootstrap_token()
    launch_file: list[Path] = []

    def remove_launch_file() -> None:
        for path in launch_file:
            remove_file(path)

    try:
        rt = build(
            port=port,
            bootstrap_token=token,
            rpc=prepared.config.rpc,
            volume=data.volume,
            allow_unencrypted=prepared.needs_test_chain,
            on_claimed=remove_launch_file,
        )
    except runtime.StorageRefused as e:
        sock.close()
        raise LaunchError(str(e)) from None

    server = make_server(rt.app, rt.shutdown.request)
    watchdog = Watchdog(data, rt.shutdown.request)

    def stop_server() -> None:
        server.should_exit = True

    rt.shutdown.add_step("stop the server", stop_server)
    rt.shutdown.add_step("stop the watchdog", watchdog.stop)
    rt.shutdown.add_step("remove the launch file", remove_launch_file)
    rt.shutdown.add_step("flush the logs", flush_logs)

    launch_file.append(write_bootstrap_file(bootstrap_dir(env, data, platform), port, token))
    expiry = threading.Timer(max(0.0, rt.sessions.expires_at - time.monotonic()), remove_launch_file)
    expiry.daemon = True
    expiry.start()
    watchdog.start()
    log.info("serving on 127.0.0.1:%d (%s)", port, "online" if rt.status.online else "offline mode")
    if prepared.open_browser:
        open_browser(launch_file[0])
    else:
        announce(f"coinacct: launch file {launch_file[0]}")

    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
        expiry.cancel()
        rt.shutdown.request("the server stopped")  # a no-op if shutdown already started
    return 0 if rt.shutdown.wait() else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        prepared = prepare(sys.argv[1:] if argv is None else argv, os.environ)
        return serve(prepared, env=os.environ)
    except LaunchError as e:
        sys.stderr.write(f"coinacct: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

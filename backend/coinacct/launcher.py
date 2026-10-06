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

6. **Node checks** through `api.runtime` → `services/` → `chain/`, with the dismount watchdog
   (T-405) already running and before anything listens. A node problem means offline mode; the
   deferred storage policy (T-401), or a volume lost meanwhile, can still stop start-up here.
7. **Listen** on a socket bound to `127.0.0.1` with a port the OS picks (T-103), and run uvicorn
   in-process on it: no proxy headers, no `Server` header, no access log, no WebSockets.
8. **Shutdown wiring:** SIGINT, SIGTERM, the watchdog and Quit all go to one `Shutdown`
   coordinator, whose steps stop the server (waiting for it, within the deadline) and the
   watchdog, remove the bootstrap file, clear `<data>/tmp` and flush the logs. uvicorn's own signal
   handling is replaced, so a signal never skips them; a second signal forces uvicorn's exit.
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
GRACEFUL_SECONDS: Final = 5  # uvicorn's wait for open requests at shutdown
SERVER_STOP_SECONDS: Final = 10.0  # under services.lifecycle.DEADLINE_SECONDS (15)
BROWSER_WAIT_SECONDS: Final = 30.0
TEMP_ENV: Final = ("TMPDIR", "SQLITE_TMPDIR")


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
    It doesn't listen yet: `serve` calls `listen` once the node checks have passed (§8.1)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
    except OSError:
        sock.close()
        raise
    return sock


class Server(uvicorn.Server):
    """uvicorn's server without its own signal handling: `serve` installs handlers for the whole
    run that ask the shutdown coordinator, whose first step stops this server. uvicorn would
    otherwise stop by itself and then re-raise the signal, ending the process before the shutdown
    steps run."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


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
        # A request that never finishes (a trickled body) must not keep the app running after
        # shutdown was asked for (T-405): uvicorn cancels what is left after this many seconds.
        timeout_graceful_shutdown=GRACEFUL_SECONDS,
    )


@contextmanager
def shutdown_signals(
    request: Callable[[str], None], server: Any, requested: Callable[[], bool]
) -> Iterator[None]:
    """SIGINT and SIGTERM ask the shutdown coordinator for the whole run, not just while uvicorn
    serves. A second signal after shutdown started forces uvicorn's exit (its usual second Ctrl-C);
    the coordinator's deadline still ends the process if that isn't enough."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: types.FrameType | None) -> None:
        if requested():
            server.force_exit = True
        request(f"received {signal.Signals(signum).name}")

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def remove_file(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def print_flushed(text: str) -> None:
    """stdout is a pipe under the E2E harness, where print() would wait in a buffer."""
    print(text, flush=True)  # noqa: T201 - the one line --no-browser promises


_ENV_LOCK = threading.Lock()


def open_in_browser(path: Path) -> bool:
    """§4: the browser gets the file's path only; the token is inside the 0600 file. A browser
    started here must not inherit the volume's temp directory (T-402: it would keep the volume busy,
    and its files would be cleared under it at the next start), so those variables are left out of
    its environment. Returns whether a browser was started."""
    with _ENV_LOCK:
        saved = {k: os.environ.pop(k) for k in TEMP_ENV if k in os.environ}
        try:
            return webbrowser.open(path.as_uri())
        finally:
            os.environ.update(saved)


def serve(  # noqa: PLR0913, PLR0915 - the parts are injectable for the tests
    prepared: Prepared,
    *,
    env: Mapping[str, str],
    platform: str = sys.platform,
    build: Callable[..., runtime.Runtime] = runtime.build,
    make_server: Callable[[Any], Any] = lambda app: Server(uvicorn_config(app)),
    make_watchdog: Callable[[datadir.DataDir, Callable[[str], None]], Watchdog] = Watchdog,
    open_browser: Callable[[Path], bool] = open_in_browser,
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

    # The watchdog runs before the node checks (§8.1), so a volume lost while they run is noticed.
    # Until the shutdown coordinator exists, what it reports is kept and refuses the start-up.
    lost_lock = threading.Lock()
    lost_early: list[str] = []
    forward: list[Callable[[str], None]] = []

    def on_lost(reason: str) -> None:
        with lost_lock:
            if not forward:
                lost_early.append(reason)
                return
            request = forward[0]
        request(reason)

    watchdog = make_watchdog(data, on_lost)
    watchdog.start()
    try:
        rt = build(
            port=port,
            bootstrap_token=token,
            rpc=prepared.config.rpc,
            volume=data.volume,
            allow_unencrypted=prepared.needs_test_chain,
            on_claimed=remove_launch_file,
        )
        # Only now, after the node checks and the storage policy (§8.1). From here a browser that is
        # quicker than uvicorn's start waits in the backlog instead of being refused.
        with lost_lock:
            forward.append(rt.shutdown.request)
            lost = lost_early[0] if lost_early else watchdog.problem()
        if lost is not None:
            raise LaunchError(f"{lost}; nothing was started")
        sock.listen(LISTEN_BACKLOG)
    except runtime.StorageRefused as e:
        watchdog.stop()
        sock.close()
        raise LaunchError(str(e)) from None
    except BaseException:
        watchdog.stop()
        sock.close()
        raise

    server = make_server(rt.app)
    server_stopped = threading.Event()

    def stop_server() -> None:
        server.should_exit = True
        # Wait for uvicorn to finish, so later steps (closing the DB, from M2) never run under a
        # request; within the coordinator's deadline, which ends the process if this doesn't.
        if not server_stopped.wait(SERVER_STOP_SECONDS):
            raise RuntimeError("the server didn't stop in time")

    rt.shutdown.add_step("stop the server", stop_server)
    rt.shutdown.add_step("stop the watchdog", watchdog.stop)
    rt.shutdown.add_step("remove the launch file", remove_launch_file)
    rt.shutdown.add_step("clear the temp directory", lambda: datadir.clear_tmp(data))  # §6
    rt.shutdown.add_step("flush the logs", flush_logs)

    def launch() -> None:
        # The browser opens once uvicorn is serving, never before (a console browser would block,
        # and nothing would answer it).
        for _ in range(int(BROWSER_WAIT_SECONDS / 0.05)):
            if getattr(server, "started", False) or rt.shutdown.requested:
                break
            threading.Event().wait(0.05)
        if rt.shutdown.requested:
            return
        try:
            opened = open_browser(launch_file[0])
        except Exception:
            log.exception("opening the browser failed")
            opened = False
        if not opened:
            announce(f"coinacct: open this file in your browser: {launch_file[0]}")

    with shutdown_signals(rt.shutdown.request, server, lambda: rt.shutdown.requested):
        try:
            launch_file.append(write_bootstrap_file(bootstrap_dir(env, data, platform), port, token))
            expiry = threading.Timer(max(0.0, rt.sessions.expires_at - time.monotonic()), remove_launch_file)
            expiry.daemon = True
            expiry.start()
            log.info("serving on 127.0.0.1:%d (%s)", port, "online" if rt.status.online else "offline mode")
            if prepared.open_browser:
                threading.Thread(target=launch, name="launch-browser", daemon=True).start()
            else:
                announce(f"coinacct: launch file {launch_file[0]}")
            server.run(sockets=[sock])
        finally:
            server_stopped.set()
            sock.close()
            remove_launch_file()
            rt.shutdown.request("the server stopped")  # a no-op if shutdown already started
        clean = rt.shutdown.wait()
    return 0 if clean else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        prepared = prepare(sys.argv[1:] if argv is None else argv, os.environ)
        return serve(prepared, env=os.environ)
    except LaunchError as e:
        sys.stderr.write(f"coinacct: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

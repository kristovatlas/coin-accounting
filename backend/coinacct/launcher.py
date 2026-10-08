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

6. **Node checks** through `api.runtime` → `services/` → `chain/`, before anything listens. The
   shutdown coordinator, its steps, the signal handlers and the dismount watchdog (T-405) already
   exist, so a signal or a lost volume during the checks runs the steps and ends the process. A
   node problem means offline mode; the deferred storage policy (T-401) can still stop start-up.
7. **Listen** on a socket bound to `127.0.0.1` with a port the OS picks (T-103), and run uvicorn
   in-process on it: no proxy headers, no `Server` header, no access log, no WebSockets.
8. **Shutdown wiring:** SIGINT, SIGTERM, the watchdog and Quit all go to one `Shutdown`
   coordinator, whose steps stop the server (waiting for it, within the deadline) and the
   watchdog, remove the bootstrap file, clear `<data>/tmp` and flush the logs. uvicorn's own signal
   handling is replaced, so a signal never skips them; a second signal forces uvicorn's exit.
   The browser inherits the process environment, `TMPDIR` included (#139).
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
from coinacct.storage.chain_state import peek_recorded_chain
from coinacct.storage.db import DB_NAME, DbError, open_db
from coinacct.storage.logfile import open_log_handler
from coinacct.storage.watchdog import Watchdog

log = logging.getLogger(__name__)

PR_SET_DUMPABLE: Final = 4  # <linux/prctl.h>
DATA_DIR_ENV: Final = "COINACCT_DATA_DIR"
BOOTSTRAP_PREFIX: Final = "coinacct-bootstrap-"
# The production frontend build (the frontend make target). Read once at start-up and served from
# memory, since `api/` has no filesystem access (architecture §2).
FRONTEND_DIST: Final = Path(__file__).resolve().parents[2] / "frontend" / "dist"
MAX_BUNDLE_FILES: Final = 200
MAX_BUNDLE_BYTES: Final = 20 * 1024 * 1024
LISTEN_BACKLOG: Final = 64
GRACEFUL_SECONDS: Final = 5  # uvicorn's wait for open requests at shutdown
SERVER_STOP_SECONDS: Final = 10.0  # under services.lifecycle.DEADLINE_SECONDS (15)
BROWSER_WAIT_SECONDS: Final = 30.0
LAUNCH_ERROR_EXIT: Final = 2


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
def shutdown_signals(request: Callable[[str], None], force_exit: Callable[[], None]) -> Iterator[None]:
    """SIGINT and SIGTERM ask the shutdown coordinator for the whole run, from before the node
    checks to the end. A second signal after shutdown started also calls `force_exit` (uvicorn's
    usual second Ctrl-C); the coordinator's deadline still ends the process if that isn't enough."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    seen: list[int] = []

    def handler(signum: int, frame: types.FrameType | None) -> None:
        if seen:
            force_exit()
        seen.append(signum)
        request(f"received {signal.Signals(signum).name}")

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def load_bundle(dist: Path) -> dict[str, bytes] | None:
    """The built frontend, as {relative POSIX path: bytes}, or None if there is no build (then the app
    serves its placeholder page). Read once, at start-up, and never written (architecture §2, ADR
    0034). Only regular files are read, never through a link, and the bundle is bounded; anything
    else, an unreadable directory included, stops start-up rather than serving a partial or foreign
    tree."""
    try:
        st = dist.lstat()
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISDIR(st.st_mode):
            raise LaunchError(f"the frontend build at {dist} is not a directory")
        try:
            index = (dist / "index.html").lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(index.st_mode):
            raise LaunchError(f"the frontend build's index.html is not a regular file: {dist}")
        files: dict[str, bytes] = {}
        total = 0
        for root, dirs, names in os.walk(dist, onerror=_refuse_unreadable, followlinks=False):
            for d in dirs:
                if (Path(root) / d).is_symlink():
                    raise LaunchError(f"the frontend build contains a link: {Path(root) / d}")
            for name in names:
                path = Path(root) / name
                if len(files) >= MAX_BUNDLE_FILES:
                    raise LaunchError(f"the frontend build at {dist} is larger than expected")
                data = _read_bundle_file(path, MAX_BUNDLE_BYTES - total)
                total += len(data)
                files[path.relative_to(dist).as_posix()] = data
        return files
    except OSError as e:
        raise LaunchError(f"can't read the frontend build at {dist} ({e.strerror or e})") from None


def _refuse_unreadable(error: OSError) -> None:
    raise error  # os.walk would otherwise skip the directory and serve a partial build


def _read_bundle_file(path: Path, budget: int) -> bytes:
    """One regular file, opened without following a link (O_NONBLOCK so a FIFO can't block), and
    read up to `budget` bytes: a file that is, or grows, larger stops start-up."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LaunchError(f"the frontend build contains something other than a file: {path}")
        data = f.read(budget + 1)
    if len(data) > budget:
        raise LaunchError(f"the frontend build at {path.parent} is larger than expected")
    return data


def remove_file(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def print_flushed(text: str) -> None:
    """stdout is a pipe under the E2E harness, where print() would wait in a buffer."""
    print(text, flush=True)  # noqa: T201 - the one line --no-browser promises


def open_in_browser(path: Path) -> bool:
    """§4: the browser gets the file's path only; the token is inside the 0600 file. Returns whether
    a browser was started. The browser inherits the process environment, `TMPDIR` included (#139)."""
    return webbrowser.open(path.as_uri())


def stderr_line(text: str) -> None:
    with suppress(OSError):
        os.write(2, f"coinacct: {text}\n".encode(errors="replace"))


def serve(  # noqa: PLR0912, PLR0913, PLR0915 - the parts are injectable for the tests
    prepared: Prepared,
    *,
    env: Mapping[str, str],
    platform: str = sys.platform,
    build: Callable[..., runtime.Runtime] = runtime.build,
    make_shutdown: Callable[[], Any] = runtime.new_shutdown,
    make_server: Callable[[Any], Any] = lambda app: Server(uvicorn_config(app)),
    make_watchdog: Callable[[datadir.DataDir, Callable[[str], None]], Watchdog] = Watchdog,
    open_browser: Callable[[Path], bool] = open_in_browser,
    announce: Callable[[str], None] = print_flushed,
    exit_process: Callable[[int], object] = os._exit,
    bundle_dir: Path | None = None,
) -> int:
    """Run the app until it shuts down. Returns the process exit code.

    The shutdown coordinator, its steps, the signal handlers and the watchdog all exist before the
    node checks (§8.1), so a signal or a lost volume at any point after this starts the same ordered
    shutdown, under the coordinator's deadline (T-405). One that comes while the node checks are
    still running ends the process once the steps have run: nothing has been served or written, and
    a check can wait on the node for a long time.
    """
    data = prepared.data_dir
    bundle = load_bundle(FRONTEND_DIST if bundle_dir is None else bundle_dir)
    sock = loopback_socket()
    port = sock.getsockname()[1]
    token = new_bootstrap_token()
    launch_file: list[Path] = []
    servers: list[Any] = []
    dbs: list[Any] = []  # the user DB, once open
    chain_jobs: list[Any] = []  # the job worker and tip poller, once started (§3)
    db_path = data.root / DB_NAME
    server_running = threading.Event()  # set just before uvicorn runs
    server_stopped = threading.Event()  # set once it has returned
    phase_lock = threading.Lock()
    # "starting" until uvicorn is about to run ("serving"), or until start-up decides to refuse
    # ("refusing", reported by this thread). "ended" once the last shutdown step has ended a start-up.
    phase = ["starting"]
    shutdown = make_shutdown()

    def remove_launch_file() -> None:
        for path in launch_file:
            remove_file(path)

    def stop_server() -> None:
        for server in servers:
            server.should_exit = True
        # A server about to start sees the request and doesn't (see below). One that runs is waited
        # for, so later steps (closing the DB) never run under a request; within the
        # coordinator's deadline, which ends the process if this doesn't.
        if server_running.is_set() and not server_stopped.wait(SERVER_STOP_SECONDS):
            raise RuntimeError("the server didn't stop in time")

    def close_db() -> None:
        # Only once nothing else can be using it (§3): not while start-up may still be in the node
        # checks (the last step ends that process), and not if the server failed to stop in time.
        # Nor while a chain job that ignored its cancel may still be writing to it.
        # An unclosed DB is safe: WAL and synchronous=FULL roll an open transaction back on reopen.
        with phase_lock:
            starting = phase[0] in ("starting", "ended")
        if starting or (server_running.is_set() and not server_stopped.is_set()):
            return
        if not all(jobs.stopped for jobs in chain_jobs):
            log.warning("a chain job didn't stop in time; the user DB is left for the OS to close")
            return
        for conn in dbs:
            conn.close()

    def stop_chain_jobs() -> None:
        # §3 step 2: cancel the current job; abort the node's scan if ours may be running.
        for jobs in chain_jobs:
            jobs.stop()

    def end_a_stalled_start_up() -> None:
        # Shutdown was asked for before uvicorn ran, and start-up hasn't taken over the report: the
        # main thread may be blocked (a node check, a write to a hung volume), so end the process.
        with phase_lock:
            if phase[0] != "starting":
                return
            phase[0] = "ended"
        stderr_line("stopped during start-up, nothing was started")
        exit_process(LAUNCH_ERROR_EXIT)

    def take_over(new: str) -> bool:
        """Move out of "starting"; False if the last step has already ended the start-up."""
        with phase_lock:
            if phase[0] == "ended":
                return False
            if phase[0] == "starting":
                phase[0] = new
            return True

    def clear_tmp_if_trusted() -> None:
        # On a lost or replaced data directory, whatever is at that path isn't the verified one:
        # don't touch it (the next start clears tmp).
        if watchdog.problem() is None:
            datadir.clear_tmp(data)

    def force_exit() -> None:
        for server in servers:
            server.force_exit = True

    watchdog = make_watchdog(data, shutdown.request)
    shutdown.add_step("report the reason", lambda: stderr_line(f"shutting down: {shutdown.reason}"))
    shutdown.add_step("stop the server", stop_server)
    shutdown.add_step("stop the chain jobs", stop_chain_jobs)
    shutdown.add_step("close the user DB", close_db)
    shutdown.add_step("stop the watchdog", watchdog.stop)
    shutdown.add_step("remove the launch file", remove_launch_file)
    shutdown.add_step("clear the temp directory", clear_tmp_if_trusted)  # §6
    shutdown.add_step("flush the logs", flush_logs)
    shutdown.add_step("end a stalled start-up", end_a_stalled_start_up)

    def refuse(why: str) -> LaunchError:
        # Start-up stops here: run the steps (nothing is served yet), then report. The coordinator's
        # deadline bounds the wait.
        take_over("refusing")
        shutdown.request(why)
        shutdown.wait()
        sock.close()
        return LaunchError(f"{shutdown.reason}; nothing was started")

    def launch() -> None:
        # The browser opens once uvicorn is serving, never before (a console browser would block,
        # and nothing would answer it). If it never starts, the path is printed instead.
        server = servers[0]
        for _ in range(int(BROWSER_WAIT_SECONDS / 0.05)):
            if getattr(server, "started", False) or shutdown.requested:
                break
            threading.Event().wait(0.05)
        if shutdown.requested:
            return
        opened = False
        if getattr(server, "started", False):
            try:
                opened = open_browser(launch_file[0])
            except Exception:
                log.exception("opening the browser failed")
        if not opened:
            announce(f"coinacct: open this file in your browser: {launch_file[0]}")

    with shutdown_signals(shutdown.request, force_exit):
        try:
            # §8.1: the user DB (its checks, integrity check and migrations), then the watchdog, then
            # the node checks, which compare the node's chain with the DB's (T-206).
            if prepared.needs_test_chain and db_path.exists():
                # Unencrypted storage is allowed only on a test chain (T-401): an existing DB that
                # records another chain is refused before anything writes to it.
                recorded = peek_recorded_chain(db_path)
                if recorded is not None and recorded not in datadir.TEST_CHAINS:
                    raise refuse(
                        f"this data directory is for {recorded}, which never runs on unencrypted storage"
                        " (T-401)"
                    )
            try:
                dbs.append(open_db(data))
            except DbError as e:
                raise refuse(str(e)) from None
            watchdog.start()  # before the node checks (§8.1)
            try:
                rt = build(
                    port=port,
                    bootstrap_token=token,
                    rpc=prepared.config.rpc,
                    volume=data.volume,
                    db=dbs[0],
                    allow_unencrypted=prepared.needs_test_chain,
                    on_claimed=remove_launch_file,
                    shutdown=shutdown,
                    bundle=bundle,
                )
                if rt.chain_jobs is not None:
                    chain_jobs.append(rt.chain_jobs)
            finally:
                with phase_lock:
                    ended = phase[0] == "ended"
            if ended:  # the last step has ended the process (or, in a test, recorded that it would)
                sock.close()
                raise LaunchError(f"{shutdown.reason}; nothing was started")
            lost = watchdog.problem()  # lost between two watchdog ticks
            if lost is not None:
                raise refuse(lost)
            if shutdown.requested:
                raise refuse(shutdown.reason or "shutdown was requested")
            # Only now, after the node checks and the storage policy (§8.1). From here a browser that
            # is quicker than uvicorn's start waits in the backlog instead of being refused.
            sock.listen(LISTEN_BACKLOG)
            servers.append(make_server(rt.app))
            if shutdown.requested:  # asked for while the server was being made
                raise refuse(shutdown.reason or "shutdown was requested")
        except runtime.StorageRefused as e:
            # A DB this start created is left as it is (schema only, no chain recorded): telling it
            # apart from one another launch is using at the same time isn't possible here, and
            # deleting a DB in use would lose data.
            raise refuse(str(e)) from None
        except DbError as e:  # the DB failed during the node checks (recording the chain)
            raise refuse(str(e)) from None
        except LaunchError:
            raise
        except BaseException:
            log.exception("start-up failed")
            refuse("start-up failed")
            raise
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
            if not take_over("serving"):
                raise LaunchError(f"{shutdown.reason}; nothing was started")
            server_running.set()
            if not shutdown.requested:  # set after server_running, so stop_server waits or we skip
                servers[0].run(sockets=[sock])
        except BaseException:
            take_over("refusing")  # this thread reports it; the last step mustn't end the process
            raise
        finally:
            server_stopped.set()
            sock.close()
            remove_launch_file()
            shutdown.request("the server stopped")  # a no-op if shutdown already started
        clean = shutdown.wait()
    return 0 if clean else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        prepared = prepare(sys.argv[1:] if argv is None else argv, os.environ)
        return serve(prepared, env=os.environ)
    except LaunchError as e:
        sys.stderr.write(f"coinacct: {e}\n")
        return LAUNCH_ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())

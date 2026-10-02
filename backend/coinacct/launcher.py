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

Starting the server, the bootstrap token exchange (§4) and the node checks (§8.1, run by the app
through `services/` → `chain/`) come with the web app. `write_bootstrap_file` is here already
because the launcher owns that file.
"""

from __future__ import annotations

import argparse
import ctypes
import html
import logging
import os
import resource
import secrets
import stat
import sys
import tempfile
import threading
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from coinacct import config
from coinacct.storage import config_file, datadir, volume
from coinacct.storage.logfile import open_log_handler

log = logging.getLogger(__name__)

PR_SET_DUMPABLE: Final = 4  # <linux/prctl.h>
DATA_DIR_ENV: Final = "COINACCT_DATA_DIR"
BOOTSTRAP_PREFIX: Final = "coinacct-bootstrap-"


class LaunchError(Exception):
    """Start-up can't continue. The message says what to fix and holds no secrets."""


@dataclass(frozen=True)
class Options:
    data_dir: str | None
    allow_unencrypted_storage: bool
    confirm_encrypted_volume: bool


@dataclass(frozen=True)
class Prepared:
    data_dir: datadir.DataDir
    config: config.Config
    # True when the volume isn't encrypted and --allow-unencrypted-storage was given: the app must
    # still refuse to run unless the node turns out to be on regtest, signet or testnet (T-401).
    needs_test_chain: bool


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
    args = parser.parse_args(list(argv))
    return Options(
        data_dir=args.data_dir or env.get(DATA_DIR_ENV) or None,
        allow_unencrypted_storage=args.allow_unencrypted_storage,
        confirm_encrypted_volume=args.confirm_encrypted_volume,
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
    return Prepared(data_dir=data, config=parsed, needs_test_chain=needs_test_chain)


def bootstrap_dir(env: Mapping[str, str], data: datadir.DataDir) -> Path:
    """Architecture §4: `$XDG_RUNTIME_DIR` (per-user, RAM-backed) when it's safe, else `<data>`."""
    runtime = env.get("XDG_RUNTIME_DIR")
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

"""Start-up before the server (architecture §1, §4, §6; THREAT_MODEL T-110, T-401, T-402, T-403, T-404)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct import launcher
from coinacct.launcher import LaunchError, Options, parse_options, prepare
from coinacct.storage import datadir
from coinacct.storage.volume import Encryption, VolumeStatus

BACKEND = Path(__file__).resolve().parents[3]
TXID = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
PASSWORD = "launcher-test-password"
GOOD_CONFIG = f'[rpc]\nhost = "127.0.0.1"\nport = 18443\nuser = "ro-client"\npassword = "{PASSWORD}"\n'


@pytest.fixture
def process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """prepare() changes process-wide state; put it all back afterwards."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(logging, "lastResort", logging.lastResort)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    logging.captureWarnings(False)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    (d / "config.toml").write_text(GOOD_CONFIG)
    (d / "config.toml").chmod(0o600)
    return d


def no_hardening() -> None:
    pass


def encrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        datadir, "detect", lambda root: VolumeStatus(Encryption.VERACRYPT, "test volume", "ext4")
    )


@pytest.fixture
def unencrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not the real detection: the tests must not depend on whether the host's disk is encrypted.
    monkeypatch.setattr(
        datadir, "detect", lambda root: VolumeStatus(Encryption.NONE, "plain test disk", "ext4")
    )


def test_hardening_disables_core_dumps_and_dumpability_and_sets_a_private_umask_t404() -> None:
    # In a child process, so this test process keeps its own limits.
    probe = (
        "import ctypes, os, resource, sys\n"
        "from coinacct.launcher import harden\n"
        "harden()\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
        "print(oct(os.umask(0)))\n"
        "print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) if sys.platform.startswith('linux') else 0)\n"
    )
    out = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(BACKEND), "PATH": os.environ.get("PATH", "")},
    ).stdout.split()
    assert out[0:2] == ["(0,", "0)"]
    assert out[2] == "0o77"
    assert out[3] == "0"  # PR_GET_DUMPABLE


def test_the_data_directory_comes_from_the_flag_or_the_environment_t401() -> None:
    assert parse_options([], {}) == Options(None, False, False)
    assert parse_options([], {"COINACCT_DATA_DIR": "/v/d"}).data_dir == "/v/d"
    assert parse_options(["--data-dir", "/v/x"], {"COINACCT_DATA_DIR": "/v/d"}).data_dir == "/v/x"
    assert parse_options(["--allow-unencrypted-storage"], {}).allow_unencrypted_storage


def test_option_abbreviations_are_refused() -> None:
    with pytest.raises(SystemExit):
        parse_options(["--allow-unencrypted"], {})


@pytest.mark.usefixtures("process_state", "unencrypted")
def test_an_unencrypted_data_directory_is_refused_without_the_flag_t401(data: Path) -> None:
    with pytest.raises(LaunchError, match="--allow-unencrypted-storage exists only for regtest"):
        prepare(["--data-dir", str(data)], {}, hardening=no_hardening)


@pytest.mark.usefixtures("process_state", "unencrypted")
def test_with_the_flag_unencrypted_storage_waits_for_the_chain_t401(data: Path) -> None:
    prepared = prepare(["--data-dir", str(data), "--allow-unencrypted-storage"], {}, hardening=no_hardening)
    assert prepared.needs_test_chain is True
    assert prepared.config.rpc.port == 18443


@pytest.mark.usefixtures("process_state")
def test_an_encrypted_volume_starts_without_the_flag(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    encrypted(monkeypatch)
    prepared = prepare(["--data-dir", str(data)], {}, hardening=no_hardening)
    assert prepared.needs_test_chain is False
    assert prepared.data_dir.root == data.resolve()


@pytest.mark.usefixtures("process_state")
def test_temp_files_go_to_the_volume_and_tmp_is_cleared_t402(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted(monkeypatch)
    (data / "tmp").mkdir(mode=0o700)
    (data / "tmp" / "leftover").write_text("x")
    prepare(["--data-dir", str(data)], {}, hardening=no_hardening)
    tmp = str(data.resolve() / "tmp")
    assert (os.environ["TMPDIR"], os.environ["SQLITE_TMPDIR"], tempfile.gettempdir()) == (tmp, tmp, tmp)
    assert list((data / "tmp").iterdir()) == []


@pytest.mark.usefixtures("process_state")
def test_logs_and_crashes_are_written_redacted_to_the_volume_t403(
    data: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    encrypted(monkeypatch)
    prepare(["--data-dir", str(data)], {}, hardening=no_hardening)
    logging.getLogger("coinacct.chain").info("fetched %s", TXID)
    try:
        raise RuntimeError(f"failed on {TXID}")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    def fail() -> None:
        raise ValueError(f"thread saw {TXID}")

    worker = threading.Thread(target=fail)
    worker.start()
    worker.join()
    # A logger that doesn't propagate and has no handler, as libraries set up: logging's
    # lastResort would print it to stderr unredacted.
    isolated = logging.getLogger("test.isolated")
    monkeypatch.setattr(isolated, "propagate", False)
    isolated.warning("isolated %s", TXID)
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_file = data / "logs" / "coinacct.log"
    text = log_file.read_text()
    assert TXID not in text
    assert text.count("<hex>") >= 4
    assert "isolated <hex>" in text
    assert "unhandled exception in thread" in text
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert TXID not in capsys.readouterr().err


@pytest.mark.usefixtures("process_state")
def test_a_missing_or_bad_config_stops_start_up_without_echoing_the_password_t201(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encrypted(monkeypatch)
    (data / "config.toml").write_text(GOOD_CONFIG.replace("127.0.0.1", "192.0.2.5"))
    with pytest.raises(LaunchError, match="not a loopback address") as e:
        prepare(["--data-dir", str(data)], {}, hardening=no_hardening)
    assert PASSWORD not in str(e.value)
    (data / "config.toml").unlink()
    with pytest.raises(LaunchError, match="doesn't exist"):
        prepare(["--data-dir", str(data)], {}, hardening=no_hardening)


@pytest.mark.usefixtures("process_state")
def test_confirming_the_volume_is_macos_only_t401(data: Path) -> None:
    with pytest.raises(LaunchError, match="for macOS"):
        prepare(
            ["--data-dir", str(data), "--confirm-encrypted-volume"],
            {},
            hardening=no_hardening,
            platform="linux",
        )


@pytest.mark.usefixtures("process_state")
def test_a_missing_data_directory_is_a_launch_error(tmp_path: Path) -> None:
    with pytest.raises(LaunchError, match="no data directory"):
        prepare([], {}, hardening=no_hardening)


def test_the_bootstrap_file_prefers_a_private_runtime_dir(data: Path, tmp_path: Path) -> None:
    dd = datadir.open_data_dir(str(data))
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    assert launcher.bootstrap_dir({"XDG_RUNTIME_DIR": str(runtime)}, dd, platform="linux") == runtime
    assert launcher.bootstrap_dir({}, dd, platform="linux") == dd.root
    runtime.chmod(0o755)
    assert launcher.bootstrap_dir({"XDG_RUNTIME_DIR": str(runtime)}, dd, platform="linux") == dd.root
    (tmp_path / "link").symlink_to(runtime)
    runtime.chmod(0o700)
    assert (
        launcher.bootstrap_dir({"XDG_RUNTIME_DIR": str(tmp_path / "link")}, dd, platform="linux") == dd.root
    )
    assert (
        launcher.bootstrap_dir({"XDG_RUNTIME_DIR": str(tmp_path / "missing")}, dd, platform="linux")
        == dd.root
    )


def test_the_bootstrap_file_never_uses_a_runtime_dir_on_macos_t110(data: Path, tmp_path: Path) -> None:
    dd = datadir.open_data_dir(str(data))
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    assert launcher.bootstrap_dir({"XDG_RUNTIME_DIR": str(runtime)}, dd, platform="darwin") == dd.root
    assert launcher.bootstrap_dir({"XDG_RUNTIME_DIR": str(runtime)}, dd, platform="linux") == runtime


def test_the_bootstrap_file_is_private_and_puts_the_token_only_in_the_fragment_t110(tmp_path: Path) -> None:
    token = launcher.new_bootstrap_token()
    path = launcher.write_bootstrap_file(tmp_path, 51234, token)
    assert path.stat().st_mode & 0o777 == 0o600
    page = path.read_text()
    assert f"url=http://127.0.0.1:51234/#bootstrap={token}" in page
    assert page.count(token) == 1
    assert '<meta name="referrer" content="no-referrer">' in page
    assert "<script" not in page.lower()
    second = launcher.write_bootstrap_file(tmp_path, 51234, token)
    assert second != path


def test_bootstrap_tokens_are_long_and_unpredictable() -> None:
    tokens = {launcher.new_bootstrap_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) >= 43 for t in tokens)  # 32 random bytes, base64url


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_a_bad_port_is_refused_for_the_bootstrap_file(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        launcher.write_bootstrap_file(tmp_path, port, "t")

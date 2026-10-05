"""The dismount watchdog (architecture §3; THREAT_MODEL T-405).

The running-thread tests use a short interval and wait on an Event for the callback, with a
generous upper bound; nothing sleeps for a fixed time.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.watchdog import Watchdog


@pytest.fixture
def verified(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


@pytest.fixture
def data(verified: DataDir) -> Path:
    return verified.root


def ignore(reason: str) -> None:
    pass


def test_an_untouched_directory_has_no_problem(verified: DataDir) -> None:
    assert Watchdog(verified, ignore).problem() is None


def test_a_removed_directory_is_reported_t405(data: Path, verified: DataDir) -> None:
    dog = Watchdog(verified, ignore)
    shutil.rmtree(data)
    assert dog.problem() == "the data directory is gone (volume dismounted?)"


def test_a_directory_replaced_at_the_same_path_is_reported_t405(
    data: Path, verified: DataDir, tmp_path: Path
) -> None:
    dog = Watchdog(verified, ignore)
    data.rename(tmp_path / "moved")
    data.mkdir(mode=0o700)  # same path, different directory (e.g. an unencrypted mount point)
    assert dog.problem() == "the data directory was replaced or remounted"


def test_the_running_watchdog_calls_back_once_and_stops_t405(data: Path, verified: DataDir) -> None:
    reasons: list[str] = []
    fired = threading.Event()

    def on_lost(reason: str) -> None:
        reasons.append(reason)
        fired.set()

    dog = Watchdog(verified, on_lost, interval=0.01)
    dog.start()
    shutil.rmtree(data)
    assert fired.wait(timeout=10)
    dog.stop()
    assert reasons == ["the data directory is gone (volume dismounted?)"]


def test_a_stopped_watchdog_doesnt_fire(data: Path, verified: DataDir) -> None:
    fired = threading.Event()
    dog = Watchdog(verified, lambda reason: fired.set(), interval=0.01)
    dog.start()
    dog.stop()
    shutil.rmtree(data)
    assert not fired.is_set()
    assert dog.problem() is not None  # the problem is real; only the stopped thread ignores it


def test_it_cant_be_started_twice(data: Path, verified: DataDir) -> None:
    dog = Watchdog(verified, ignore, interval=60)
    dog.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            dog.start()
    finally:
        dog.stop()


def test_a_directory_swapped_in_after_verification_is_never_trusted_t405(
    verified: DataDir, tmp_path: Path
) -> None:
    # Replaced between open_data_dir() and the watchdog's start (start-up runs config, DB open and
    # migrations in between): the watchdog must use the verified identity, not adopt the new one.
    verified.root.rename(tmp_path / "moved")
    verified.root.mkdir(mode=0o700)
    dog = Watchdog(verified, ignore)
    assert dog.problem() == "the data directory was replaced or remounted"

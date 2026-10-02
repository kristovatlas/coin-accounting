"""The dismount watchdog (architecture §3; THREAT_MODEL T-405).

The running-thread tests use a short interval and wait on an Event for the callback, with a
generous upper bound; nothing sleeps for a fixed time.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from coinacct.storage.watchdog import Watchdog


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return d


def ignore(reason: str) -> None:
    pass


def test_an_untouched_directory_has_no_problem(data: Path) -> None:
    assert Watchdog(data, ignore).problem() is None


def test_a_removed_directory_is_reported_t405(data: Path) -> None:
    dog = Watchdog(data, ignore)
    data.rmdir()
    assert dog.problem() == "the data directory is gone (volume dismounted?)"


def test_a_directory_replaced_at_the_same_path_is_reported_t405(data: Path, tmp_path: Path) -> None:
    dog = Watchdog(data, ignore)
    data.rename(tmp_path / "moved")
    data.mkdir(mode=0o700)  # same path, different directory (e.g. an unencrypted mount point)
    assert dog.problem() == "the data directory was replaced or remounted"


def test_the_running_watchdog_calls_back_once_and_stops_t405(data: Path) -> None:
    reasons: list[str] = []
    fired = threading.Event()

    def on_lost(reason: str) -> None:
        reasons.append(reason)
        fired.set()

    dog = Watchdog(data, on_lost, interval=0.01)
    dog.start()
    data.rmdir()
    assert fired.wait(timeout=10)
    dog.stop()
    assert reasons == ["the data directory is gone (volume dismounted?)"]


def test_a_stopped_watchdog_doesnt_fire(data: Path) -> None:
    fired = threading.Event()
    dog = Watchdog(data, lambda reason: fired.set(), interval=0.01)
    dog.start()
    dog.stop()
    data.rmdir()
    assert not fired.is_set()
    assert dog.problem() is not None  # the problem is real; only the stopped thread ignores it


def test_it_cant_be_started_twice(data: Path) -> None:
    dog = Watchdog(data, ignore, interval=60)
    dog.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            dog.start()
    finally:
        dog.stop()

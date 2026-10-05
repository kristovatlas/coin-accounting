"""The shutdown coordinator (architecture §3; THREAT_MODEL T-405).

`hard_exit` is replaced by a recorder, so the tests see when the process would have been ended
without ending it. Waits are on events with generous upper bounds; nothing sleeps for a fixed time
except the hanging step, which is released at the end of its test.
"""

from __future__ import annotations

import functools
import logging
import threading

import pytest

from coinacct.services.lifecycle import HARD_EXIT_CODE, Shutdown

WAIT = 5.0


class Exits:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.called = threading.Event()

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        self.called.set()


def test_steps_run_once_in_the_order_added() -> None:
    exits, ran = Exits(), list[str]()
    shutdown = Shutdown(hard_exit=exits)
    names = ("stop server", "cancel job", "close db", "flush logs")
    for name in names:
        shutdown.add_step(name, functools.partial(ran.append, name))
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert ran == list(names)
    assert exits.codes == []  # a clean shutdown leaves the exit to the launcher


def test_it_records_the_first_reason_and_runs_only_once(caplog: pytest.LogCaptureFixture) -> None:
    ran: list[str] = []
    shutdown = Shutdown(hard_exit=Exits())
    shutdown.add_step("step", lambda: ran.append("step"))
    assert not shutdown.requested
    with caplog.at_level(logging.INFO, logger="coinacct.services.lifecycle"):
        shutdown.request("the data directory is gone (volume dismounted?)")
        shutdown.request("SIGINT")
        assert shutdown.wait(WAIT)
    assert shutdown.requested
    assert shutdown.reason == "the data directory is gone (volume dismounted?)"
    assert ran == ["step"]
    assert any(
        r.levelno == logging.CRITICAL and "volume dismounted" in r.getMessage() for r in caplog.records
    )
    assert any("also asked: SIGINT" in r.getMessage() for r in caplog.records)


def test_concurrent_requests_run_the_steps_once() -> None:
    ran: list[int] = []
    shutdown = Shutdown(hard_exit=Exits())
    shutdown.add_step("count", lambda: ran.append(1))
    start = threading.Barrier(8)

    def ask(i: int) -> None:
        start.wait()
        shutdown.request(f"caller {i}")

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT)
    assert shutdown.wait(WAIT)
    assert ran == [1]


def test_request_returns_without_waiting_for_the_steps() -> None:
    release = threading.Event()
    shutdown = Shutdown(hard_exit=Exits())
    shutdown.add_step("slow", lambda: release.wait(WAIT))
    shutdown.request("quit")  # would block for WAIT seconds if the steps ran on this thread
    assert not shutdown.wait(0)
    release.set()
    assert shutdown.wait(WAIT)


def test_a_failing_step_doesnt_stop_the_others_and_the_process_exits_hard_t405() -> None:
    exits, ran = Exits(), []
    shutdown = Shutdown(hard_exit=exits)

    def broken() -> None:
        raise OSError("WAL checkpoint failed")

    shutdown.add_step("checkpoint", broken)
    shutdown.add_step("flush logs", lambda: ran.append("flush logs"))
    shutdown.request("the data directory is gone (volume dismounted?)")
    assert exits.called.wait(WAIT)
    assert ran == ["flush logs"]
    assert exits.codes == [HARD_EXIT_CODE]


def test_a_hanging_step_ends_the_process_at_the_deadline_t405() -> None:
    exits, release = Exits(), threading.Event()
    shutdown = Shutdown(deadline=0.05, hard_exit=exits)
    shutdown.add_step("stuck", lambda: release.wait(WAIT))
    shutdown.request("SIGTERM")
    try:
        assert exits.called.wait(WAIT)
        assert exits.codes == [HARD_EXIT_CODE]
        assert not shutdown.wait(0)
    finally:
        release.set()


def test_a_shutdown_that_finishes_in_time_doesnt_exit_hard_later() -> None:
    exits = Exits()
    shutdown = Shutdown(deadline=0.05, hard_exit=exits)
    shutdown.add_step("quick", lambda: None)
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    # Past the deadline: the timer was cancelled, so nothing fires.
    assert not exits.called.wait(0.2)


def test_if_shutdown_cant_start_the_process_exits_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    exits = Exits()
    shutdown = Shutdown(hard_exit=exits)

    def no_threads(*args: object, **kwargs: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    shutdown.request("SIGTERM")  # must not raise to the caller
    assert exits.codes == [HARD_EXIT_CODE]


def test_steps_cant_be_added_once_shutdown_has_started() -> None:
    shutdown = Shutdown(hard_exit=Exits())
    shutdown.request("SIGTERM")
    with pytest.raises(RuntimeError, match="already started"):
        shutdown.add_step("late", lambda: None)
    assert shutdown.wait(WAIT)


@pytest.mark.parametrize("deadline", [0, -1.0])
def test_the_deadline_must_be_positive(deadline: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        Shutdown(deadline=deadline)

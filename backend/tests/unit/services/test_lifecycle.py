"""The shutdown coordinator (architecture §3; THREAT_MODEL T-405).

`hard_exit` is replaced by a recorder and the deadline timer by `FakeTimer`, which the tests fire
by hand, so nothing depends on thread scheduling or wall-clock time. Waits are on events with
generous upper bounds.
"""

from __future__ import annotations

import faulthandler
import functools
import logging
import signal
import threading
from collections.abc import Callable

import pytest

from coinacct.services.lifecycle import HARD_EXIT_CODE, Shutdown, daemon_timer

WAIT = 5.0


class Exits:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.called = threading.Event()

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        self.called.set()


class FakeTimer:
    def __init__(self, seconds: float, callback: Callable[[], None]) -> None:
        self.seconds, self.callback = seconds, callback
        self.started = self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        assert self.started
        self.callback()


class Timers:
    def __init__(self) -> None:
        self.made: list[FakeTimer] = []

    def __call__(self, seconds: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(seconds, callback)
        self.made.append(timer)
        return timer


def coordinator(deadline: float = 15.0) -> tuple[Shutdown, Exits, Timers]:
    exits, timers = Exits(), Timers()
    return Shutdown(deadline=deadline, hard_exit=exits, timer=timers), exits, timers


def test_steps_run_once_in_the_order_added() -> None:
    shutdown, exits, timers = coordinator()
    ran: list[str] = []
    names = ("stop server", "cancel job", "close db", "flush logs")
    for name in names:
        shutdown.add_step(name, functools.partial(ran.append, name))
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert ran == list(names)
    assert exits.codes == []  # a clean shutdown leaves the exit to the launcher
    assert timers.made[0].cancelled


def test_the_runner_is_not_a_daemon_thread() -> None:
    # A daemon thread is cut off when the main thread ends after the server returns, which would
    # skip closing the DB and flushing the logs.
    shutdown, _, _ = coordinator()
    seen: list[bool] = []
    shutdown.add_step("record", lambda: seen.append(threading.current_thread().daemon))
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert seen == [False]


def test_it_records_the_first_reason_and_runs_only_once(caplog: pytest.LogCaptureFixture) -> None:
    shutdown, _, timers = coordinator()
    ran: list[str] = []
    shutdown.add_step("step", lambda: ran.append("step"))
    assert not shutdown.requested
    with caplog.at_level(logging.INFO, logger="coinacct.services.lifecycle"):
        shutdown.request("the data directory is gone (volume dismounted?)")
        shutdown.request("SIGINT")
        assert shutdown.wait(WAIT)
    assert shutdown.reason == "the data directory is gone (volume dismounted?)"
    assert ran == ["step"]
    assert len(timers.made) == 1
    assert any(
        r.levelno == logging.CRITICAL and "volume dismounted" in r.getMessage() for r in caplog.records
    )
    assert any("also asked: SIGINT" in r.getMessage() for r in caplog.records)


def test_concurrent_requests_run_the_steps_once() -> None:
    shutdown, _, _ = coordinator()
    ran: list[int] = []
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
    assert not any(t.is_alive() for t in threads)
    assert shutdown.wait(WAIT)
    assert ran == [1]


def test_request_returns_without_waiting_for_the_steps() -> None:
    shutdown, _, _ = coordinator()
    release = threading.Event()
    shutdown.add_step("slow", lambda: release.wait(WAIT))
    shutdown.request("quit")  # would block for WAIT seconds if the steps ran on this thread
    assert not shutdown.wait(0)
    release.set()
    assert shutdown.wait(WAIT)


def test_the_deadline_is_armed_before_anything_is_logged_t405(monkeypatch: pytest.MonkeyPatch) -> None:
    # On a hung volume a log write can block; the deadline must already be running by then.
    shutdown, _, timers = coordinator()
    armed_when_logging: list[bool] = []

    def critical(*args: object, **kwargs: object) -> None:
        armed_when_logging.append(bool(timers.made) and timers.made[0].started)

    monkeypatch.setattr("coinacct.services.lifecycle.log.critical", critical)
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert armed_when_logging == [True]


def test_a_failing_step_doesnt_stop_the_others_and_the_process_exits_hard_t405() -> None:
    shutdown, exits, timers = coordinator()
    ran: list[str] = []

    def broken() -> None:
        raise OSError("WAL checkpoint failed")

    shutdown.add_step("checkpoint", broken)
    shutdown.add_step("flush logs", lambda: ran.append("flush logs"))
    shutdown.request("the data directory is gone (volume dismounted?)")
    assert exits.called.wait(WAIT)
    assert ran == ["flush logs"]
    assert exits.codes == [HARD_EXIT_CODE]
    assert not shutdown.wait(0)  # not reported as a clean shutdown
    assert not timers.made[0].cancelled  # still armed while the failure is logged


def test_a_failing_step_still_exits_if_logging_is_broken_t405(monkeypatch: pytest.MonkeyPatch) -> None:
    shutdown, exits, _ = coordinator()

    def broken_log(*args: object, **kwargs: object) -> None:
        raise OSError("log volume gone")

    def broken() -> None:
        raise OSError("close failed")

    monkeypatch.setattr("coinacct.services.lifecycle.log.exception", broken_log)
    shutdown.add_step("close db", broken)
    shutdown.request("SIGTERM")
    assert exits.called.wait(WAIT)
    assert exits.codes == [HARD_EXIT_CODE]


def test_a_hanging_step_ends_the_process_at_the_deadline_t405() -> None:
    shutdown, exits, timers = coordinator(deadline=15.0)
    release = threading.Event()
    shutdown.add_step("stuck", lambda: release.wait(WAIT))
    shutdown.request("SIGTERM")
    try:
        assert timers.made[0].seconds == 15.0
        timers.made[0].fire()  # the deadline passes while the step hangs
        assert exits.codes == [HARD_EXIT_CODE]
        assert not shutdown.wait(0)
    finally:
        release.set()


def test_a_shutdown_that_finished_in_time_ignores_a_late_timer() -> None:
    shutdown, exits, timers = coordinator()
    shutdown.add_step("quick", lambda: None)
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    timers.made[0].fire()  # even if cancel came too late
    assert exits.codes == []


def test_if_the_runner_cant_start_the_process_exits_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    shutdown, exits, timers = coordinator()

    def no_threads(*args: object, **kwargs: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    shutdown.request("SIGTERM")  # must not raise to the caller
    assert timers.made[0].started  # the deadline was armed first
    assert exits.codes == [HARD_EXIT_CODE]


def test_a_signal_while_the_main_thread_holds_the_lock_doesnt_deadlock() -> None:
    # Python runs signal handlers on the main thread between bytecodes, possibly inside add_step or
    # request. If this ever deadlocks, faulthandler ends the test run instead of hanging it.
    shutdown, _, _ = coordinator()
    previous = signal.signal(signal.SIGUSR1, lambda signum, frame: shutdown.request("SIGUSR1"))
    faulthandler.dump_traceback_later(30, exit=True)
    try:
        with shutdown._lock:
            signal.raise_signal(signal.SIGUSR1)
        assert shutdown.wait(WAIT)
        assert shutdown.reason == "SIGUSR1"
    finally:
        faulthandler.cancel_dump_traceback_later()
        signal.signal(signal.SIGUSR1, previous)


def test_steps_cant_be_added_once_shutdown_has_started() -> None:
    shutdown, _, _ = coordinator()
    shutdown.request("SIGTERM")
    with pytest.raises(RuntimeError, match="already started"):
        shutdown.add_step("late", lambda: None)
    assert shutdown.wait(WAIT)


@pytest.mark.parametrize("deadline", [0, -1.0])
def test_the_deadline_must_be_positive(deadline: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        Shutdown(deadline=deadline)


def test_the_real_timer_is_a_daemon() -> None:
    timer = daemon_timer(60, lambda: None)
    assert isinstance(timer, threading.Timer)
    assert timer.daemon

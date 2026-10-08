"""The shutdown coordinator (architecture §3; THREAT_MODEL T-405).

`hard_exit` is replaced by a recorder and the deadline timer by `FakeTimer`, which the tests fire
by hand, so nothing depends on wall-clock time. The coordinator's own threads are real; waits on
them are on events with generous upper bounds.
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
LOGGER = "coinacct.services.lifecycle"


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
        self.on_cancel: Callable[[], None] = lambda: None

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.on_cancel()
        self.cancelled = True

    def fire(self) -> None:
        assert self.started
        self.callback()


class Timers:
    def __init__(self) -> None:
        self.made: list[FakeTimer] = []
        self.armed = threading.Event()

    def __call__(self, seconds: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(seconds, callback)
        self.made.append(timer)
        self.armed.set()
        return timer

    def first(self) -> FakeTimer:
        assert self.armed.wait(WAIT)
        return self.made[0]


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
    assert timers.first().cancelled


def test_the_runner_is_not_a_daemon_thread() -> None:
    # A daemon thread is cut off when the main thread ends after the server returns, which would
    # skip closing the DB and flushing the logs.
    shutdown, _, _ = coordinator()
    seen: list[bool] = []
    shutdown.add_step("record", lambda: seen.append(threading.current_thread().daemon))
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert seen == [False]


def test_a_request_from_a_daemon_thread_still_runs_the_steps_on_a_non_daemon_thread() -> None:
    # Threads inherit the daemon flag of the thread that starts them, and the watchdog is a daemon.
    shutdown, _, _ = coordinator()
    seen: list[bool] = []
    shutdown.add_step("record", lambda: seen.append(threading.current_thread().daemon))
    watchdog = threading.Thread(target=shutdown.request, args=("volume gone",), daemon=True)
    watchdog.start()
    watchdog.join(WAIT)
    assert shutdown.wait(WAIT)
    assert seen == [False]


def test_it_records_the_first_reason_and_runs_only_once() -> None:
    shutdown, _, timers = coordinator()
    ran: list[str] = []
    shutdown.add_step("step", lambda: ran.append("step"))
    assert not shutdown.requested
    shutdown.request("the data directory is gone (volume dismounted?)")
    shutdown.request("SIGINT")
    assert shutdown.requested
    assert shutdown.wait(WAIT)
    assert shutdown.reason == "the data directory is gone (volume dismounted?)"
    assert ran == ["step"]
    assert len(timers.made) == 1


def test_the_reason_is_logged_as_critical() -> None:
    shutdown, _, _ = coordinator()
    records: list[logging.LogRecord] = []
    logged = threading.Event()

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)  # before signalling, so the test never reads too early
            logged.set()

    capture = Capture(level=logging.CRITICAL)
    logging.getLogger(LOGGER).addHandler(capture)
    try:
        shutdown.request("the data directory is gone (volume dismounted?)")
        assert logged.wait(WAIT)
    finally:
        logging.getLogger(LOGGER).removeHandler(capture)
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.CRITICAL, "shutting down: the data directory is gone (volume dismounted?)")
    ]


def test_concurrent_requests_run_the_steps_once() -> None:
    shutdown, _, timers = coordinator()
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
    assert len(timers.made) == 1


def test_a_nested_request_during_the_claim_starts_shutdown_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # A signal handler can re-enter request() on the main thread; only one shutdown may start.
    shutdown, _, timers = coordinator()
    ran: list[int] = []
    shutdown.add_step("count", lambda: ran.append(1))
    original_set = shutdown._wake.set

    def set_with_nested_request() -> None:
        shutdown.request("SIGINT (nested)")
        original_set()

    monkeypatch.setattr(shutdown._wake, "set", set_with_nested_request)
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert ran == [1]
    assert len(timers.made) == 1
    assert shutdown.reason == "SIGTERM"


def test_a_step_being_added_when_shutdown_is_claimed_still_runs() -> None:
    shutdown, _, _ = coordinator()
    ran: list[str] = []
    with shutdown._lock:  # add_step was interrupted between its check and its append
        shutdown.request("SIGTERM")
        shutdown._steps.append(("close db", lambda: ran.append("close db")))
    assert shutdown.wait(WAIT)
    assert ran == ["close db"]


def test_the_runner_reads_the_steps_under_the_lock() -> None:
    # So an add_step that a signal interrupted after its check finishes its append first.
    shutdown, _, _ = coordinator()
    held_when_read: list[bool] = []

    class Steps(list[tuple[str, Callable[[], object]]]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            held_when_read.append(shutdown._lock.locked())
            return super().__iter__()

    shutdown._steps = Steps()
    shutdown.request("SIGTERM")
    assert shutdown.wait(WAIT)
    assert held_when_read == [True]


def test_request_returns_without_waiting_for_the_steps() -> None:
    shutdown, _, _ = coordinator()
    release = threading.Event()
    shutdown.add_step("slow", lambda: release.wait(WAIT))
    shutdown.request("quit")  # would block for WAIT seconds if the steps ran on this thread
    assert not shutdown.wait(0)
    release.set()
    assert shutdown.wait(WAIT)


def test_the_deadline_and_the_steps_start_before_anything_is_logged_t405(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a hung volume a log write can block; the deadline and the steps must not wait for it.
    shutdown, _, timers = coordinator()
    release, logged = threading.Event(), threading.Event()
    ran: list[str] = []
    armed_when_logging: list[bool] = []

    reason = "SIGTERM (this test's)"

    def stuck_critical(*args: object, **kwargs: object) -> None:
        if reason not in args:
            return  # another test's coordinator, logging late from its own thread
        armed_when_logging.append(bool(timers.made) and timers.made[0].started)
        logged.set()
        release.wait(WAIT)  # a log write stuck on the volume

    monkeypatch.setattr(f"{LOGGER}.log.critical", stuck_critical)
    shutdown.add_step("stop server", lambda: ran.append("stop server"))
    try:
        shutdown.request(reason)
        assert logged.wait(WAIT)
        assert shutdown.wait(WAIT)  # the steps ran while the log write was stuck
        assert armed_when_logging == [True]
        assert ran == ["stop server"]
    finally:
        release.set()


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
    assert not timers.first().cancelled  # still armed while the failure is logged


def test_the_hard_exit_bypasses_logging_t405(capfd: pytest.CaptureFixture[str]) -> None:
    shutdown, exits, timers = coordinator()
    release = threading.Event()

    class Broken(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise OSError("log volume gone")

    broken = Broken()
    logging.getLogger(LOGGER).addHandler(broken)
    try:
        shutdown.add_step("stuck", lambda: release.wait(WAIT))
        shutdown.request("SIGTERM")
        timers.first().fire()  # the deadline passes while the step hangs and logging is broken
        assert exits.codes == [HARD_EXIT_CODE]
        assert "coinacct: shutdown didn't finish within 15 s; exiting now" in capfd.readouterr().err
    finally:
        logging.getLogger(LOGGER).removeHandler(broken)
        release.set()


def test_a_failing_step_still_exits_if_logging_is_broken_t405(monkeypatch: pytest.MonkeyPatch) -> None:
    shutdown, exits, _ = coordinator()

    def broken_log(*args: object, **kwargs: object) -> None:
        raise OSError("log volume gone")

    def broken() -> None:
        raise OSError("close failed")

    monkeypatch.setattr(f"{LOGGER}.log.exception", broken_log)
    monkeypatch.setattr(f"{LOGGER}.log.critical", broken_log)
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
        timer = timers.first()
        assert timer.seconds == 15.0
        timer.fire()  # the deadline passes while the step hangs
        assert exits.codes == [HARD_EXIT_CODE]
        assert not shutdown.wait(0)
    finally:
        release.set()


def test_done_is_set_before_the_timer_is_cancelled() -> None:
    # A real timer may already be firing when cancel() runs; it must then see a clean finish.
    shutdown, exits, timers = coordinator()
    release = threading.Event()
    done_at_cancel: list[bool] = []
    shutdown.add_step("gated", lambda: release.wait(WAIT))
    shutdown.request("SIGTERM")
    timer = timers.first()
    timer.on_cancel = lambda: done_at_cancel.append(shutdown.wait(0))
    release.set()
    assert shutdown.wait(WAIT)
    assert done_at_cancel == [True]
    timer.fire()  # a late timer is ignored
    assert exits.codes == []


def test_if_the_runner_cant_start_the_process_exits_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    shutdown, exits, timers = coordinator()

    def no_threads(*args: object, **kwargs: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)  # the waiter is already running
    shutdown.request("SIGTERM")  # must not raise to the caller
    assert exits.called.wait(WAIT)
    assert timers.first().started  # the deadline was armed first
    assert exits.codes == [HARD_EXIT_CODE]


def test_a_signal_while_the_main_thread_holds_the_lock_doesnt_deadlock() -> None:
    # Python runs signal handlers on the main thread between bytecodes, possibly inside add_step.
    # If this ever deadlocks, faulthandler ends the test run instead of hanging it.
    shutdown, _, _ = coordinator()
    previous = signal.signal(signal.SIGUSR1, lambda signum, frame: shutdown.request("SIGUSR1"))
    faulthandler.dump_traceback_later(30, exit=True)
    try:
        with shutdown._lock:  # e.g. inside add_step
            signal.raise_signal(signal.SIGUSR1)
            assert shutdown.requested  # the handler ran here, while the lock was held
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

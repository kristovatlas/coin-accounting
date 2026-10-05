"""The shutdown coordinator (architecture §3; THREAT_MODEL T-405).

Shutdown can be asked for from anywhere: a signal handler, the dismount watchdog's thread, or quit
from the UI. `Shutdown.request` is safe to call from any of them, any number of times, and never
raises. It does almost nothing itself, because a signal handler runs on the main thread between
bytecodes and may interrupt that thread anywhere, even inside `threading` or `logging`:

- It **claims** shutdown with one non-blocking lock acquire, which can't be interrupted halfway, so
  exactly one caller wins even when a signal handler re-enters.
- It then only **sets an event**. A waiter thread, started with the coordinator, wakes, arms the
  deadline timer, starts the runner, and only then logs. So no thread is started and nothing is
  logged from inside a signal handler, and a log write that blocks on a hung volume can't delay the
  deadline or the steps.

The runner is an explicitly **non-daemon** thread (a thread inherits the daemon flag of the thread
that starts it, and the waiter and the watchdog are daemons), so the interpreter waits for the steps
when the server returns and the main thread ends. It runs the steps in the order they were added
(stop the server, cancel the current job, close the DB, flush the logs), reading the list under the
lock, so a step being added when the claim happened isn't lost.

The app must not keep running after shutdown was asked for, for example on a dismounted volume
(T-405). If a step raises, the other steps still run and the process then exits hard; if the steps
don't finish within the deadline, it exits hard at once. The hard exit writes one line to stderr
and never goes through `logging`. A clean shutdown exits with code 0 through the launcher; a failed
or overdue one exits with 1.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Final, Protocol

log = logging.getLogger(__name__)

DEADLINE_SECONDS: Final = 15.0
HARD_EXIT_CODE: Final = 1


class Timer(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


def daemon_timer(seconds: float, callback: Callable[[], None]) -> Timer:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    return timer


def _stderr(text: str) -> None:
    try:
        os.write(2, f"coinacct: {text}\n".encode(errors="replace"))
    except BaseException:  # noqa: S110 - best effort only; the exit must still happen
        pass


class Shutdown:
    def __init__(
        self,
        *,
        deadline: float = DEADLINE_SECONDS,
        hard_exit: Callable[[int], object] = os._exit,
        timer: Callable[[float, Callable[[], None]], Timer] = daemon_timer,
    ) -> None:
        if deadline <= 0:
            raise ValueError("the shutdown deadline must be positive")
        self._deadline = deadline
        self._hard_exit = hard_exit
        self._timer = timer
        self._steps: list[tuple[str, Callable[[], object]]] = []
        self._lock = threading.Lock()  # guards _steps; never taken by request()
        self._claim = threading.Lock()  # held for good by the first request
        self._wake = threading.Event()
        self._reason: str | None = None
        self._done = threading.Event()
        self._waiter = threading.Thread(target=self._wait_for_request, name="shutdown-waiter", daemon=True)
        self._waiter.start()

    @property
    def requested(self) -> bool:
        return self._claim.locked()

    @property
    def reason(self) -> str | None:
        return self._reason

    def add_step(self, name: str, step: Callable[[], object]) -> None:
        """Add a step; steps run in the order they were added. Only before shutdown starts."""
        with self._lock:
            if self.requested:
                raise RuntimeError("shutdown has already started")
            self._steps.append((name, step))

    def request(self, reason: str) -> None:
        """Start shutting down. Safe from any thread or a signal handler; never raises."""
        try:
            if not self._claim.acquire(blocking=False):
                return  # already under way; the first reason stands
            self._reason = reason
            self._wake.set()
        except BaseException:  # a failure here must still end the process
            self._exit_now("shutdown could not start")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every step has run cleanly. False on timeout, or if a step failed (the
        process is then being ended)."""
        return self._done.wait(timeout)

    def _wait_for_request(self) -> None:
        self._wake.wait()
        try:
            timer = self._timer(self._deadline, self._overdue)
            timer.start()  # armed before anything that could block, logging included
            runner = threading.Thread(target=self._run, args=(timer,), name="shutdown", daemon=False)
            runner.start()
        except BaseException:
            self._exit_now("shutdown could not start")
            return
        try:
            log.critical("shutting down: %s", self._reason)
        except BaseException:  # noqa: S110 - logging may be what's broken; the steps already run
            pass

    def _run(self, timer: Timer) -> None:
        with self._lock:  # waits for an add_step that was interrupted by the claim
            steps = list(self._steps)
        failed = []
        for name, step in steps:
            try:
                step()
            except BaseException:  # one failing step must not stop the others
                failed.append(name)
                try:
                    log.exception("shutdown step %r failed", name)  # the timer is still armed
                except BaseException:  # noqa: S110 - logging may be what's broken
                    pass
        if failed:
            self._exit_now(f"shutdown steps failed ({', '.join(failed)})")
            return
        self._done.set()  # before cancelling, so a timer already firing sees a clean finish
        timer.cancel()

    def _overdue(self) -> None:
        if not self._done.is_set():
            self._exit_now(f"shutdown didn't finish within {self._deadline:.0f} s")

    def _exit_now(self, why: str) -> None:
        _stderr(f"{why}; exiting now")
        self._hard_exit(HARD_EXIT_CODE)

"""The shutdown coordinator (architecture §3; THREAT_MODEL T-405).

Shutdown can be asked for from anywhere: a signal handler, the dismount watchdog's thread, or quit
from the UI. `Shutdown.request` is safe to call from any of them, any number of times, and never
raises: the first call starts the shutdown and later calls only log. The steps run on their own
thread, in the order they were added (stop the server, cancel the current job, close the
DB, flush the logs), so a slow step never blocks the caller.

The app must not keep running after shutdown was asked for, for example on a dismounted volume
(T-405). So:
- The deadline timer is armed **before anything else**, logging included: on a hung volume even a
  log write can block, and the timer must still end the process.
- If a step raises, the other steps still run, and then the process exits hard. If the steps don't
  finish within the deadline, it exits hard at once. The hard exit writes one line to stderr and
  never goes through `logging`.
- The steps run on a non-daemon thread, so the interpreter waits for them when the server returns
  and the main thread ends; a clean shutdown then exits normally, writing logs and coverage.
- The lock is re-entrant: a signal handler runs on the main thread between bytecodes, possibly
  while that thread holds the lock, and must not deadlock.

A clean shutdown exits with code 0 through the launcher; a failed or overdue one exits with 1.
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
        self._lock = threading.RLock()
        self._reason: str | None = None
        self._done = threading.Event()

    @property
    def requested(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    def add_step(self, name: str, step: Callable[[], object]) -> None:
        """Add a step; steps run in the order they were added. Only before shutdown starts."""
        with self._lock:
            if self._reason is not None:
                raise RuntimeError("shutdown has already started")
            self._steps.append((name, step))

    def request(self, reason: str) -> None:
        """Start shutting down. Safe from any thread or a signal handler; never raises."""
        try:
            with self._lock:
                first = self._reason
                if first is None:
                    self._reason = reason
                    steps = list(self._steps)
            if first is not None:
                log.info("shutdown already under way (%s); also asked: %s", first, reason)
                return
            timer = self._timer(self._deadline, self._overdue)
            timer.start()  # armed before anything that could block, logging included
            log.critical("shutting down: %s", reason)
            runner = threading.Thread(target=self._run, args=(steps, timer), name="shutdown")
            runner.start()
        except BaseException:  # a failure here must still end the process
            self._exit_now("shutdown could not start")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every step has run cleanly. False on timeout, or if a step failed (the
        process is then being ended)."""
        return self._done.wait(timeout)

    def _run(self, steps: list[tuple[str, Callable[[], object]]], timer: Timer) -> None:
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
        timer.cancel()
        self._done.set()

    def _overdue(self) -> None:
        if not self._done.is_set():
            self._exit_now(f"shutdown didn't finish within {self._deadline:.0f} s")

    def _exit_now(self, why: str) -> None:
        _stderr(f"{why}; exiting now")
        self._hard_exit(HARD_EXIT_CODE)

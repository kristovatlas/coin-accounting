"""The shutdown coordinator (architecture §3; THREAT_MODEL T-405).

Shutdown can be asked for from anywhere: a signal handler, the dismount watchdog's thread, or quit
from the UI. `Shutdown.request` is safe to call from any of them, any number of times, and never
raises: the first call starts the shutdown and later calls only log. The steps run on their own
thread, in the order they were added (stop accepting requests, cancel the current job, close the
DB, flush the logs), so a slow step never blocks the caller.

The app must not keep running after shutdown was asked for, for example on a dismounted volume
(T-405). So if a step raises, the other steps still run and the process then exits hard; if the
steps don't finish within the deadline, it exits hard at once. A clean shutdown leaves the exit to
the launcher (the server returns and the main thread ends), so coverage and logs are written.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Final

log = logging.getLogger(__name__)

DEADLINE_SECONDS: Final = 15.0
HARD_EXIT_CODE: Final = 1


class Shutdown:
    def __init__(
        self,
        *,
        deadline: float = DEADLINE_SECONDS,
        hard_exit: Callable[[int], object] = os._exit,
    ) -> None:
        if deadline <= 0:
            raise ValueError("the shutdown deadline must be positive")
        self._deadline = deadline
        self._hard_exit = hard_exit
        self._steps: list[tuple[str, Callable[[], object]]] = []
        self._lock = threading.Lock()
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
                if self._reason is not None:
                    log.info("shutdown already under way (%s); also asked: %s", self._reason, reason)
                    return
                self._reason = reason
                steps = list(self._steps)
            log.critical("shutting down: %s", reason)
            timer = threading.Timer(self._deadline, self._overdue)
            timer.daemon = True
            timer.start()
            runner = threading.Thread(target=self._run, args=(steps, timer), name="shutdown", daemon=True)
            runner.start()
        except BaseException:  # a failure here must still end the process
            log.exception("shutdown could not start; exiting now")
            self._hard_exit(HARD_EXIT_CODE)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every step has run. True if they did, False on timeout."""
        return self._done.wait(timeout)

    def _run(self, steps: list[tuple[str, Callable[[], object]]], timer: threading.Timer) -> None:
        failed = []
        for name, step in steps:
            try:
                step()
            except BaseException:  # one failing step must not stop the others
                log.exception("shutdown step %r failed", name)
                failed.append(name)
        timer.cancel()
        self._done.set()
        if failed:
            log.critical("shutdown steps failed (%s); exiting now", ", ".join(failed))
            self._hard_exit(HARD_EXIT_CODE)

    def _overdue(self) -> None:
        if not self._done.is_set():
            log.critical("shutdown didn't finish within %.0f s; exiting now", self._deadline)
            self._hard_exit(HARD_EXIT_CODE)

"""The dismount watchdog (architecture §3; THREAT_MODEL T-405).

Every 2 s it checks that the verified data directory is still there: the same path, on the same
device, with the same inode, as recorded when `open_data_dir` verified it (not when the watchdog
starts, so a directory swapped in between is never trusted). If the volume has been dismounted,
or the directory was removed or replaced, it calls `on_lost` once (which starts shutdown) and
stops. It never tries to recover: whatever is at that path now is not the directory that was
verified.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Final

from coinacct.storage.datadir import DataDir

log = logging.getLogger(__name__)

INTERVAL_SECONDS: Final = 2.0


class Watchdog:
    def __init__(
        self, data_dir: DataDir, on_lost: Callable[[str], None], *, interval: float = INTERVAL_SECONDS
    ) -> None:
        self._root = data_dir.root
        self._identity = (data_dir.device, data_dir.inode)  # as verified, never re-read here
        self._on_lost = on_lost
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def problem(self) -> str | None:
        """Why the data directory can no longer be trusted, or None if it still can."""
        try:
            st = os.stat(self._root)
        except FileNotFoundError:
            return "the data directory is gone (volume dismounted?)"
        except OSError as e:
            return f"the data directory can't be read ({e.strerror})"
        if (st.st_dev, st.st_ino) != self._identity:
            return "the data directory was replaced or remounted"
        return None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the watchdog is already running")
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            reason = self.problem()
            if reason is not None:
                log.critical("shutting down: %s (T-405)", reason)
                self._on_lost(reason)
                return

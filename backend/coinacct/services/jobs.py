"""The job worker and the tip poller (architecture §3, §8.4; THREAT_MODEL T-207, T-212).

- **The job worker** is one thread that runs queued jobs one at a time: Core runs one `scanblocks`
  at a time for all RPC users, so chain jobs never overlap. A job has an id and a state (queued,
  running, done, failed, cancelled); `cancel` stops a queued job and asks a running one to stop at
  its next check.
- **The tip poller** calls `getbestblockhash` every `POLL_SECONDS` and queues one tip-change job
  (`chain_sync.sync`) when the tip moves, unless one is already queued. It uses no ZMQ, which would
  be a new flow (§5).
- **At shutdown** (§3 step 2): the poller stops, the queue is cancelled, and if the app's in-flight
  scan marker is set, `scanblocks abort` is sent, so the node isn't left scanning for a client
  that's gone. Both threads stop within a bounded time: the steps never wait on the node.

A failed job is logged by its exception's class name only: node replies and errors can name the
user's scripts (T-201, T-403).
"""

from __future__ import annotations

import enum
import itertools
import logging
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from coinacct.chain import node_checks
from coinacct.chain.scans import Scan
from coinacct.chain.txs import ChainRpc
from coinacct.domain.secret import Secret
from coinacct.services import chain_sync
from coinacct.storage.chain_cache import scan_marker
from coinacct.storage.db import Connection

log = logging.getLogger(__name__)

POLL_SECONDS: Final = 30.0
STOP_SECONDS: Final = 2.0


class State(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: int
    name: str
    run: Callable[[threading.Event], object]
    state: State = State.QUEUED
    result: object = None
    error: str | None = None  # the exception's class name, never its message
    cancel: threading.Event = field(default_factory=threading.Event)


class JobWorker:
    """One thread, one job at a time (§3)."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, name: str, run: Callable[[threading.Event], object]) -> int:
        """Queue `run(cancelled)`; it should return soon after `cancelled` is set."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("the job worker is stopping")
            job = Job(next(self._ids), name, run)
            self._jobs[job.id] = job
        self._queue.put(job)
        return job.id

    def job(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def pending(self, name: str) -> bool:
        """Whether a job of this name is queued or running."""
        with self._lock:
            return any(
                j.name == name and j.state in (State.QUEUED, State.RUNNING) for j in self._jobs.values()
            )

    def cancel(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.cancel.set()
                if job.state is State.QUEUED:
                    job.state = State.CANCELLED

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    @property
    def stopped(self) -> bool:
        """The thread has ended (or never started): no job can touch the DB any more."""
        return not self._thread.is_alive()

    def stop(self, timeout: float = STOP_SECONDS) -> None:
        """Cancel everything and stop, waiting at most `timeout` for a running job (a daemon
        thread: one stuck on the node doesn't hold up the process)."""
        with self._lock:
            self._stopping.set()
            for job in self._jobs.values():
                job.cancel.set()
                if job.state is State.QUEUED:
                    job.state = State.CANCELLED
        self._queue.put(None)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                if job.state is not State.QUEUED:
                    continue
                job.state = State.RUNNING
                self._current = job
            try:
                result = job.run(job.cancel)
            except Exception as e:  # a failed job never stops the worker
                with self._lock:
                    job.state, job.error = State.FAILED, type(e).__name__
                log.warning("job %d (%s) failed: %s", job.id, job.name, type(e).__name__)
            else:
                with self._lock:
                    job.result = result
                    job.state = State.CANCELLED if job.cancel.is_set() else State.DONE
            finally:
                with self._lock:
                    self._current = None


class TipPoller:
    """Polls the node's tip and queues a tip-change job when it moves (§3, §8.4)."""

    def __init__(
        self,
        rpc: ChainRpc,
        conn: Connection,
        worker: JobWorker,
        subjects: Callable[[], Sequence[Scan]],
        *,
        interval: float = POLL_SECONDS,
    ) -> None:
        self._rpc, self._conn, self._worker, self._subjects = rpc, conn, worker, subjects
        self._interval = interval
        self._last: object = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="tip-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def poll(self) -> int | None:
        """One poll: the queued job's id, or None if the tip hasn't moved or a sync is pending."""
        best = self._rpc.call("getbestblockhash")
        if best == self._last or self._worker.pending("sync"):
            return None
        self._last = best
        return self._worker.submit("sync", self._sync)

    @property
    def stopped(self) -> bool:
        return not self._thread.is_alive()

    def stop(self, timeout: float = STOP_SECONDS) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _sync(self, cancelled: threading.Event) -> chain_sync.SyncResult | None:
        if cancelled.is_set():
            return None
        result = chain_sync.sync(self._rpc, self._conn, self._subjects())
        if not result.complete:
            self._last = None  # unfinished: the next poll queues the sync again
        return result

    def _loop(self) -> None:
        while True:
            try:
                self.poll()
            except Exception as e:  # the node can be away for a while: keep polling
                log.warning("tip poll failed: %s", type(e).__name__)
            if self._stop.wait(self._interval):
                return


def stop_chain_jobs(rpc: ChainRpc, conn: Connection, poller: TipPoller, worker: JobWorker) -> None:
    """The shutdown step (§3 step 2): stop polling, cancel the jobs, and abort the node's scan if
    the app's in-flight marker says one of ours may be running (T-212)."""
    poller.stop()
    worker.stop()
    if scan_marker(conn) is not None:
        try:
            rpc.call("scanblocks", ["abort"])
        except Exception as e:  # the node may be gone; the marker makes the next start recover
            log.warning("couldn't abort the node's scan at shutdown: %s", type(e).__name__)


@dataclass
class ChainJobs:
    """The running worker and poller, for the shutdown step."""

    rpc: ChainRpc
    conn: Connection
    worker: JobWorker
    poller: TipPoller

    def stop(self) -> None:
        stop_chain_jobs(self.rpc, self.conn, self.poller, self.worker)

    @property
    def stopped(self) -> bool:
        """Both threads have ended, so the user DB can be closed (§3)."""
        return self.worker.stopped and self.poller.stopped


def no_subjects() -> Sequence[Scan]:
    """Until descriptors and addresses are imported (M2), there is nothing to scan."""
    return ()


def start_chain_jobs(  # noqa: PLR0913 - the endpoint, its credentials, the DB and the subjects
    host: str,
    port: int,
    user: str,
    password: Secret,
    *,
    db: Connection,
    subjects: Callable[[], Sequence[Scan]] = no_subjects,
) -> ChainJobs:
    """Start the job worker and the tip poller against the configured node (online mode only)."""
    rpc = node_checks.connect(host, port, user, password)
    worker = JobWorker()
    poller = TipPoller(rpc, db, worker, subjects)
    worker.start()
    poller.start()
    return ChainJobs(rpc, db, worker, poller)

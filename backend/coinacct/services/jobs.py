"""The job worker and the tip poller (architecture §3, §8.4; THREAT_MODEL T-207, T-212).

- **The job worker** is one thread that runs queued jobs one at a time: Core runs one `scanblocks`
  at a time for all RPC users, so chain jobs never overlap. A job has an id and a state (queued,
  running, done, failed, cancelled); `cancel` stops a queued job and asks a running one to stop at
  its next check.
- **The tip poller** calls `getbestblockhash` every `POLL_SECONDS` and queues one tip-change job
  (`chain_sync.sync`) when the tip moves, unless one is already queued. It uses no ZMQ, which would
  be a new flow (§5).
- **At shutdown** (§3 step 2): the poller stops, every job is cancelled (a running sync starts no
  further range), and if the app's in-flight scan marker is set, `scanblocks abort` is sent on a
  client with a short timeout, so the node isn't left scanning for a client that's gone. Then the
  worker gets a short join, and the marker is read once more for a range that started just as
  the cancel was set. Each wait is bounded (each abort runs on its own thread, joined for
  `ABORT_SECONDS`), so the step ends within `SHUTDOWN_STEP_SECONDS`, well inside the shutdown
  deadline (T-405).

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
KEEP_FINISHED: Final = 50  # finished jobs kept for `job()`; older ones (and their results) go
STOP_SECONDS: Final = 2.0
# The shutdown step's waits: the poller's join, the abort call, the worker's join (T-405).
POLLER_JOIN_SECONDS: Final = 0.5
ABORT_SECONDS: Final = 1.0
WORKER_JOIN_SECONDS: Final = 1.0
# Two abort calls at most: one before the worker's join, one after it if a scan started meanwhile.
SHUTDOWN_STEP_SECONDS: Final = POLLER_JOIN_SECONDS + 2 * ABORT_SECONDS + WORKER_JOIN_SECONDS


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
        self.request_stop()
        self.join(timeout)

    def request_stop(self) -> None:
        """Cancel every job and refuse new ones, without waiting."""
        with self._lock:
            self._stopping.set()
            for job in self._jobs.values():
                job.cancel.set()
                if job.state is State.QUEUED:
                    job.state = State.CANCELLED
        self._queue.put(None)

    def join(self, timeout: float) -> None:
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                if job.state is not State.QUEUED:
                    self._forget_old()  # a cancelled queued job counts towards the bound too
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
                    self._forget_old()

    def _forget_old(self) -> None:
        done = [i for i, j in self._jobs.items() if j.state not in (State.QUEUED, State.RUNNING)]
        for i in done[: max(0, len(done) - KEEP_FINISHED)]:
            del self._jobs[i]


# After a complete sync: (rpc, db, the scripts with mempool activity) -> the descriptors whose windows
# grew (services.discovery.extend_windows).
Discover = Callable[[ChainRpc, Connection, Sequence[str]], Sequence[int]]


class TipPoller:
    """Polls the node's tip and queues a tip-change job when it moves (§3, §8.4)."""

    def __init__(  # noqa: PLR0913 - the node, the DB, the worker, what to scan, and discovery
        self,
        rpc: ChainRpc,
        conn: Connection,
        worker: JobWorker,
        subjects: Callable[[], Sequence[Scan]],
        *,
        interval: float = POLL_SECONDS,
        discover: Discover | None = None,
    ) -> None:
        self._rpc, self._conn, self._worker, self._subjects = rpc, conn, worker, subjects
        self._discover = discover
        self._interval = interval
        self._last: object = None
        self._requested = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="tip-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request_sync(self) -> None:
        """Queue a sync at the next poll even if the tip hasn't moved (new imports to scan). Kept until
        a sync is queued after it: a sync already running may have read the subjects before the
        import, so its finishing can't clear the request."""
        self._requested.set()

    def poll(self) -> int | None:
        """One poll: the queued job's id, or None if the tip hasn't moved or a sync is pending."""
        best = self._rpc.call("getbestblockhash")
        if self._worker.pending("sync"):
            return None  # a sync running now keeps any request for the one after it
        if best == self._last and not self._requested.is_set():
            return None
        self._requested.clear()  # the job queued here reads the subjects when it starts
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
            self._last = None
            return None
        try:
            result = chain_sync.sync(self._rpc, self._conn, self._subjects(), cancelled.is_set)
        except BaseException:
            self._last = None  # failed: the next poll queues the sync again, tip moved or not
            raise
        if not result.complete and (result.waiting or not result.over_budget):
            self._last = None  # unfinished: the next poll queues the sync again
        elif result.target is not None:
            # The tip this sync actually processed, which may be newer than the polled one. A subject
            # over its budget alone waits for the tip after it: rescanning its refused range every
            # poll would hold Core's one scan slot (T-205, T-212) until the user decides (M2).
            self._last = result.target.blockhash
        if result.complete and self._discover is not None:
            self._grow_windows(self._discover, result)
        return result

    def _grow_windows(self, discover: Discover, result: chain_sync.SyncResult) -> None:
        """After a complete sync: grow the descriptor windows whose used indexes near their end, and
        scan the wider windows at the next poll (services.discovery)."""
        pending = [p.script_hex for p in result.pending or ()]
        try:
            grown = discover(self._rpc, self._conn, pending)
        except Exception as e:  # the sync itself is done: the next one tries again
            log.warning("growing descriptor windows failed: %s", type(e).__name__)
            return
        if grown:
            self.request_sync()

    def _loop(self) -> None:
        while True:
            try:
                self.poll()
            except Exception as e:  # the node can be away for a while: keep polling
                log.warning("tip poll failed: %s", type(e).__name__)
            if self._stop.wait(self._interval):
                return


def stop_chain_jobs(abort_rpc: ChainRpc, conn: Connection, poller: TipPoller, worker: JobWorker) -> None:
    """The shutdown step (§3 step 2): stop polling, cancel the jobs, abort the node's scan if the
    app's in-flight marker says one of ours may be running (T-212), then a short join. `abort_rpc`
    is a client with a short timeout (`ABORT_SECONDS`): a hung node can't hold up shutdown.

    The marker is read on the shared connection while a cancelled job may still be running: one
    SELECT, serialised by SQLite, which also sees a marker that job has just set. Without a marker
    nothing is aborted, since the running scan may be another client's (§8.1)."""
    poller.stop(POLLER_JOIN_SECONDS)
    worker.request_stop()
    _abort_if_ours(abort_rpc, conn)
    worker.join(WORKER_JOIN_SECONDS)
    if not worker.stopped:
        # A job that checked its cancel just before it was set may have started one more range
        # since the first read; it set the marker before calling the node, so this read sees it.
        _abort_if_ours(abort_rpc, conn)


def _abort_if_ours(abort_rpc: ChainRpc, conn: Connection) -> None:
    if scan_marker(conn) is None:
        return

    def abort() -> None:
        try:
            abort_rpc.call("scanblocks", ["abort"])
        except Exception as e:  # the node may be gone; the marker makes the next start recover
            log.warning("couldn't abort the node's scan at shutdown: %s", type(e).__name__)

    # The client's timeout is per socket operation; a node trickling its reply could exceed it, so
    # the call gets a hard bound here (a daemon thread: one still waiting doesn't hold the exit).
    t = threading.Thread(target=abort, name="scan-abort", daemon=True)
    t.start()
    t.join(ABORT_SECONDS)


@dataclass
class ChainJobs:
    """The running worker and poller, for the shutdown step."""

    abort_rpc: ChainRpc
    conn: Connection
    worker: JobWorker
    poller: TipPoller

    def request_sync(self) -> None:
        """Something to scan was imported: sync at the next poll, tip moved or not (PLAN §3)."""
        self.poller.request_sync()

    def stop(self) -> None:
        stop_chain_jobs(self.abort_rpc, self.conn, self.poller, self.worker)

    @property
    def stopped(self) -> bool:
        """The worker has ended, so the user DB can be closed (§3). The poller never touches the
        DB, so a poll still waiting on a slow node doesn't keep it open."""
        return self.worker.stopped


def no_subjects() -> Sequence[Scan]:
    """Until descriptors and addresses are imported (M2), there is nothing to scan."""
    return ()


def start_chain_jobs(  # noqa: PLR0913 - the endpoint, its credentials, the DB, the subjects and discovery
    host: str,
    port: int,
    user: str,
    password: Secret,
    *,
    db: Connection,
    subjects: Callable[[], Sequence[Scan]] = no_subjects,
    discover: Discover | None = None,
) -> ChainJobs:
    """Start the job worker and the tip poller against the configured node (online mode only)."""
    rpc = node_checks.connect(host, port, user, password)
    abort_rpc = node_checks.connect(host, port, user, password, timeout=ABORT_SECONDS)
    worker = JobWorker()
    poller = TipPoller(rpc, db, worker, subjects, discover=discover)
    worker.start()
    poller.start()
    return ChainJobs(abort_rpc, db, worker, poller)

"""The job worker, the tip poller and their shutdown step (architecture §3, §8.4; THREAT_MODEL T-207,
T-212)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import node_checks
from coinacct.chain.scans import Scan
from coinacct.domain.secret import Secret
from coinacct.rpc import RpcTransportError
from coinacct.services import chain_sync, jobs
from coinacct.services.jobs import JobWorker, State, TipPoller
from coinacct.storage import chain_cache as cc
from coinacct.storage.chain_state import Tip, last_tip, record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from tests.unit.services.test_chain_sync import Node, bh

SUBJECT = Scan("s", ("addr(bcrt1qexample)",))


def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "timed out"
        threading.Event().wait(0.005)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


@pytest.fixture
def worker() -> Iterator[JobWorker]:
    w = JobWorker()
    w.start()
    yield w
    w.stop()


# --- The job worker ------------------------------------------------------------------------------


def test_jobs_run_one_at_a_time_in_order(worker: JobWorker) -> None:
    running = 0
    overlap: list[int] = []
    order: list[int] = []

    def job(n: int) -> Callable[[threading.Event], object]:
        def run(_cancelled: threading.Event) -> object:
            nonlocal running
            running += 1
            overlap.append(running)
            threading.Event().wait(0.01)
            order.append(n)
            running -= 1
            return n * 10

        return run

    ids = [worker.submit("j", job(n)) for n in range(5)]
    wait_for(lambda: all(worker.job(i).state is State.DONE for i in ids))  # type: ignore[union-attr]
    assert order == [0, 1, 2, 3, 4] and max(overlap) == 1  # Core runs one scan at a time (§3)
    assert [worker.job(i).result for i in ids] == [0, 10, 20, 30, 40]  # type: ignore[union-attr]


def test_a_failed_job_is_recorded_by_class_only_and_the_worker_goes_on(
    worker: JobWorker, caplog: pytest.LogCaptureFixture
) -> None:
    def fails(_cancelled: threading.Event) -> object:
        raise ValueError("addr(bcrt1qsecretscript) is broken")

    failed = worker.submit("bad", fails)
    after = worker.submit("good", lambda _c: "ok")
    wait_for(lambda: worker.job(after).state is State.DONE)  # type: ignore[union-attr]
    job = worker.job(failed)
    assert job is not None and job.state is State.FAILED and job.error == "ValueError"
    assert "bcrt1qsecretscript" not in caplog.text and "ValueError" in caplog.text  # T-201, T-403


def test_a_cancelled_queued_job_never_runs() -> None:
    w = JobWorker()  # not started: the job stays queued
    ran: list[int] = []
    job_id = w.submit("j", lambda _c: ran.append(1))
    assert w.pending("j")
    w.cancel(job_id)
    assert w.job(job_id).state is State.CANCELLED and not w.pending("j")  # type: ignore[union-attr]
    w.start()
    later = w.submit("k", lambda _c: "done")
    wait_for(lambda: w.job(later).state is State.DONE)  # type: ignore[union-attr]
    assert ran == []
    w.stop()


def test_a_running_job_is_asked_to_stop_and_ends_cancelled(worker: JobWorker) -> None:
    started = threading.Event()

    def long(cancelled: threading.Event) -> object:
        started.set()
        assert cancelled.wait(5)
        return "stopped early"

    job_id = worker.submit("long", long)
    assert started.wait(5) and worker.busy and worker.pending("long")
    worker.cancel(job_id)
    wait_for(lambda: worker.job(job_id).state is State.CANCELLED)  # type: ignore[union-attr]
    wait_for(lambda: not worker.busy)


def test_stop_cancels_everything_refuses_new_jobs_and_never_waits_long_on_a_stuck_job() -> None:
    w = JobWorker()
    w.start()
    release = threading.Event()
    started = threading.Event()

    def stuck(_cancelled: threading.Event) -> object:  # ignores the cancel request, like a slow node
        started.set()
        release.wait(5)
        return None

    w.submit("stuck", stuck)
    queued = w.submit("queued", lambda _c: "never")
    assert started.wait(5)
    t0 = time.monotonic()
    w.stop(timeout=0.2)
    assert time.monotonic() - t0 < 2  # bounded (§3): the shutdown deadline isn't spent here
    assert w.job(queued).state is State.CANCELLED  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="stopping"):
        w.submit("late", lambda _c: None)
    release.set()


# --- The tip poller ------------------------------------------------------------------------------


def test_a_tip_change_queues_one_sync_and_an_unchanged_tip_none_t207(conn: sqlite3.Connection) -> None:
    node = Node(500, {10: 1})
    w = JobWorker()  # not started yet: the sync stays queued
    poller = TipPoller(node, conn, w, lambda: [SUBJECT], interval=3600)
    first = poller.poll()
    assert first is not None
    assert poller.poll() is None  # one sync is pending already
    node.tip = 501
    assert poller.poll() is None  # the tip moved, but the queued sync will catch up to it
    node.tip = 500
    w.start()
    wait_for(lambda: w.job(first).state is State.DONE)  # type: ignore[union-attr]
    assert last_tip(conn) == Tip(bh(500), 500) and cc.coverage(conn, "s") is not None
    assert poller.poll() is None  # the tip hasn't moved
    node.tip = 501
    again = poller.poll()
    assert again is not None
    wait_for(lambda: w.job(again).state is State.DONE)  # type: ignore[union-attr]
    assert last_tip(conn) == Tip(bh(501), 501)
    w.stop()


def test_an_unfinished_sync_is_queued_again_on_the_next_poll_t210(conn: sqlite3.Connection) -> None:
    node = Node(500, {10: 1})
    node.busy = True  # another RPC user holds the scan slot
    w = JobWorker()
    w.start()
    poller = TipPoller(node, conn, w, lambda: [SUBJECT], interval=3600)
    first = poller.poll()
    assert first is not None
    wait_for(lambda: w.job(first).state is State.DONE)  # type: ignore[union-attr]
    result = w.job(first).result  # type: ignore[union-attr]
    assert isinstance(result, chain_sync.SyncResult) and not result.complete
    node.busy = False
    again = poller.poll()  # same tip, but the catch-up isn't finished
    assert again is not None
    wait_for(lambda: w.job(again).state is State.DONE)  # type: ignore[union-attr]
    assert last_tip(conn) == Tip(bh(500), 500)
    w.stop()


def test_the_poller_keeps_polling_through_node_errors(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    class Away(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getbestblockhash" and len([c for c in self.calls if c[0] == method]) < 2:
                self.calls.append((method, params))
                raise RpcTransportError("getbestblockhash: connection refused")
            return super().call(method, params)

    node = Away(500)
    w = JobWorker()
    w.start()
    poller = TipPoller(node, conn, w, lambda: [], interval=0.01)
    caplog.set_level(logging.WARNING)
    poller.start()
    wait_for(lambda: last_tip(conn) == Tip(bh(500), 500))  # the node came back: the sync ran
    poller.stop()
    w.stop()
    assert "RpcTransportError" in caplog.text and "connection refused" not in caplog.text


def test_a_cancelled_sync_does_nothing(conn: sqlite3.Connection) -> None:
    node = Node(500)
    w = JobWorker()
    poller = TipPoller(node, conn, w, lambda: [SUBJECT], interval=3600)
    job_id = poller.poll()
    assert job_id is not None
    job = w.job(job_id)
    assert job is not None
    job.cancel.set()
    assert job.run(job.cancel) is None and last_tip(conn) is None


# --- Shutdown and start ------------------------------------------------------------------------


def test_shutdown_aborts_the_nodes_scan_only_when_ours_may_be_running_t212(conn: sqlite3.Connection) -> None:
    node = Node(500)
    w = JobWorker()
    w.start()
    poller = TipPoller(node, conn, w, lambda: [], interval=3600)
    jobs.stop_chain_jobs(node, conn, poller, w)
    assert ("scanblocks", ["abort"]) not in node.calls

    cc.set_scan_marker(conn, "s")
    node.running = True
    w2 = JobWorker()
    w2.start()
    jobs.stop_chain_jobs(node, conn, TipPoller(node, conn, w2, lambda: [], interval=3600), w2)
    assert ("scanblocks", ["abort"]) in node.calls
    with pytest.raises(RuntimeError):
        w2.submit("late", lambda _c: None)


def test_a_node_gone_at_shutdown_is_logged_and_the_marker_kept_for_the_next_start_t212(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    class Gone(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            raise RpcTransportError(f"{method}: connection refused")

    cc.set_scan_marker(conn, "s")
    w = JobWorker()
    w.start()
    jobs.stop_chain_jobs(Gone(500), conn, TipPoller(Gone(500), conn, w, lambda: [], interval=3600), w)
    assert cc.scan_marker(conn) is not None  # the next start recovers it (§8.1)
    assert "RpcTransportError" in caplog.text


def test_start_chain_jobs_polls_at_once_and_stops_cleanly(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = Node(500)
    seen: list[tuple[str, int, str]] = []

    def connect(host: str, port: int, user: str, password: Secret) -> Node:
        seen.append((host, port, user))
        return node

    monkeypatch.setattr(node_checks, "connect", connect)
    running = jobs.start_chain_jobs("127.0.0.1", 18443, "ro-client", Secret("pw"), db=conn)
    assert seen == [("127.0.0.1", 18443, "ro-client")]
    wait_for(lambda: last_tip(conn) == Tip(bh(500), 500))  # the first poll caught up
    assert not running.stopped  # the launcher keeps the DB open until this is true
    running.stop()
    assert running.stopped
    assert not running.poller._thread.is_alive() and not running.worker._thread.is_alive()

"""The account and import routes (PLAN §3; THREAT_MODEL T-203, T-403, T-701, T-703; architecture §3).

They run against a real user DB (the writer and its readers) and `services.imports.Imports`, with
the fake node of the import tests. Synthetic regtest data only.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.api import runtime
from coinacct.api.app import create_app
from coinacct.api.session import Sessions
from coinacct.domain.secret import Secret
from coinacct.rpc import RpcTransportError
from coinacct.services.imports import Imports, service
from coinacct.services.startup import NodeStatus
from coinacct.storage import accounts as ac
from coinacct.storage import db as db_module
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import DbError, open_db, open_reader, transaction
from tests.unit.services.test_imports import TPUB_DESC, Node, p2wpkh

from .asgi import PORT, Reply, call

TOKEN = "launch-token-for-tests"
ONLINE = NodeStatus(online=True, chain="regtest", reasons=())


class Recorded:
    """A reader that records whether it was closed (it belongs to the request's thread, so the test
    thread can't simply try it)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class World:
    def __init__(self, tmp_path: Path, *, online: bool = True, timeout: float = 5.0) -> None:
        d = tmp_path / "data"
        d.mkdir(mode=0o700)
        data = open_data_dir(str(d))
        self.db = open_db(data, timeout=timeout)
        record_chain(self.db, "regtest")
        self.readers: list[Recorded] = []

        def reader() -> Any:
            conn = Recorded(open_reader(data))
            self.readers.append(conn)
            return conn

        self.node = Node()
        self.imports = Imports(self.db, reader, self.node if online else None)
        self.syncs = 0
        self.imports.jobs_started(self.on_sync)
        self.sessions = Sessions(TOKEN)
        self.app = create_app(
            port=PORT,
            sessions=self.sessions,
            status=lambda: ONLINE,
            on_quit=lambda: None,
            imports=self.imports,
        )
        reply = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN})
        self.auth = {"Authorization": f"Bearer {reply.json()['session']}"}

    def on_sync(self) -> None:
        self.syncs += 1

    def get(self, path: str) -> Reply:
        return call(self.app, "GET", path, headers=self.auth)

    def post(self, path: str, body: dict[str, Any]) -> Reply:
        return call(self.app, "POST", path, headers=self.auth, json_body=body)

    def wallet(self) -> int:
        reply = self.post("/api/tax-accounts", {"name": "Cold storage", "kind": "self_custody"})
        assert reply.status == 201
        return int(reply.json()["id"])


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


ROUTES = [
    ("GET", "/api/accounts"),
    ("POST", "/api/entities"),
    ("POST", "/api/tax-accounts"),
    ("POST", "/api/clients"),
    ("POST", "/api/imports/addresses/preview"),
    ("POST", "/api/imports/addresses"),
    ("POST", "/api/imports/descriptor/preview"),
    ("POST", "/api/imports/descriptor"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_account_and_import_route_needs_the_session(world: World, method: str, path: str) -> None:
    reply = call(world.app, method, path, json_body={} if method == "POST" else None)
    assert reply.status == 401


def test_without_the_service_there_are_no_import_routes(tmp_path: Path) -> None:
    sessions = Sessions(TOKEN)
    app = create_app(port=PORT, sessions=sessions, status=lambda: ONLINE, on_quit=lambda: None)
    session = call(app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
    assert call(app, "GET", "/api/accounts", headers={"Authorization": f"Bearer {session}"}).status == 404


def test_accounts_are_listed_and_added(world: World) -> None:
    wallet = world.wallet()
    exchange = world.post("/api/entities", {"name": "An exchange", "kind": "exchange"}).json()["id"]
    custodial = world.post(
        "/api/tax-accounts", {"name": "Exchange account", "kind": "custodial", "entity_id": exchange}
    ).json()["id"]
    phone = world.post("/api/clients", {"name": "A phone wallet", "kind": "mobile"}).json()["id"]
    body = world.get("/api/accounts").json()
    assert body["online"] is True
    assert {"id": exchange, "name": "An exchange", "kind": "exchange", "knows_identity": True} in body[
        "entities"
    ]
    assert [a["id"] for a in body["tax_accounts"]] == [wallet, custodial]
    assert body["clients"] == [{"id": phone, "name": "A phone wallet", "kind": "mobile"}]
    assert world.readers and all(_closed(r) for r in world.readers)  # read through readers, each closed


def _closed(conn: Recorded) -> bool:
    return conn.closed


def test_a_refused_account_gets_the_fixed_message(world: World) -> None:
    world.post("/api/clients", {"name": "A phone wallet", "kind": "mobile"})
    reply = world.post("/api/clients", {"name": "A phone wallet", "kind": "mobile"})
    assert reply.status == 422 and "duplicate" in reply.json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "kind": "self"},  # the user entity is seeded, never added
        {"name": "", "kind": "exchange"},
        {"name": "x", "kind": "exchange", "extra": 1},
    ],
)
def test_malformed_bodies_are_refused_without_echo(world: World, body: dict[str, Any]) -> None:
    reply = world.post("/api/entities", body)
    assert reply.status == 422 and reply.json() == {"error": "invalid request"}


def test_an_address_list_is_previewed_then_imported_and_scanned_t701(world: World) -> None:
    wallet = world.wallet()
    (a1, s1), (a2, s2) = p2wpkh(1), p2wpkh(2)
    upload = f"{a1}\nnot an address\n{a2}\n"
    preview = world.post("/api/imports/addresses/preview", {"text": upload}).json()
    assert preview == {
        "new": [{"script": s1, "address": a1}, {"script": s2, "address": a2}],
        "known": [],
        "repeated": 0,
        "invalid_lines": [2],
    }
    assert world.syncs == 0 and ac.addresses(world.db) == []  # a preview writes nothing
    owner = {"entity_id": ac.ME, "tax_account_id": wallet, "label": "cold"}
    reply = world.post("/api/imports/addresses", {"text": upload, **owner, "start_height": 100})
    assert reply.json() == {"added": [s1, s2], "conflicts": []} and world.syncs == 1
    earlier = world.post("/api/imports/addresses", {"text": upload, **owner, "start_height": 0})
    # Nothing added, but the known scripts now start earlier: that history needs a scan.
    assert earlier.json() == {"added": [], "conflicts": []} and world.syncs == 2
    assert {a.start_height for a in ac.addresses(world.db)} == {0}


def test_an_import_that_only_conflicts_asks_for_no_sync(world: World) -> None:
    wallet = world.wallet()
    exchange = world.post("/api/entities", {"name": "An exchange", "kind": "exchange"}).json()["id"]
    address, script = p2wpkh(1)
    ac.add_addresses(world.db, [(script, address)], entity_id=exchange, tax_account_id=None, source="manual")
    body = {"text": address, "entity_id": ac.ME, "tax_account_id": wallet}
    assert world.post("/api/imports/addresses", body).json() == {"added": [], "conflicts": [script]}
    assert world.syncs == 0  # nothing of this owner's to scan


def test_a_private_key_is_refused_and_never_repeated_t703(world: World) -> None:
    wif_shaped = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")[:52]
    for path in ("/api/imports/addresses/preview", "/api/imports/descriptor/preview"):
        reply = world.post(path, {"text": f"{p2wpkh(1)[0]}\n{wif_shaped}"})
        assert reply.status == 422 and wif_shaped not in reply.body.decode()
    assert world.node.calls == []


def test_a_descriptor_is_previewed_through_the_node_then_imported_and_scanned(world: World) -> None:
    wallet = world.wallet()
    preview = world.post("/api/imports/descriptor/preview", {"text": TPUB_DESC, "gap_limit": 3}).json()
    assert preview["descriptor"] == TPUB_DESC + "#abcdefgh" and preview["ranged"] is True
    assert [d["index"] for d in preview["derived"]] == [0, 1, 2] and preview["already_imported"] is False
    reply = world.post(
        "/api/imports/descriptor",
        {"text": TPUB_DESC, "gap_limit": 3, "entity_id": ac.ME, "tax_account_id": wallet},
    )
    assert reply.status == 201 and world.syncs == 1
    [desc] = ac.descriptors(world.db)
    assert desc.id == reply.json()["id"] and desc.range_end == 2


def test_offline_a_descriptor_is_refused_before_anything_is_sent_t203(tmp_path: Path) -> None:
    w = World(tmp_path, online=False)
    try:
        for path in ("/api/imports/descriptor/preview", "/api/imports/descriptor"):
            body = {"text": TPUB_DESC, "entity_id": ac.ME, "tax_account_id": None}
            reply = w.post(path, body if path.endswith("descriptor") else {"text": TPUB_DESC})
            assert reply.status == 409 and "offline" in reply.json()["error"]
        assert w.get("/api/accounts").json()["online"] is False and w.node.calls == []
    finally:
        w.db.close()


def test_a_busy_db_is_a_503_not_a_hang(tmp_path: Path) -> None:
    w = World(tmp_path, timeout=0.1)
    inside, release = threading.Event(), threading.Event()

    def holder() -> None:
        with transaction(w.db):
            inside.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert inside.wait(5)
        reply = w.post("/api/clients", {"name": "A phone wallet", "kind": "mobile"})
        assert reply.status == 503 and "busy" in reply.json()["error"]
    finally:
        release.set()
        t.join(5)
        w.db.close()


def test_an_upload_over_the_body_limit_is_refused_before_the_service(world: World) -> None:
    reply = world.post("/api/imports/addresses/preview", {"text": "x" * (64 * 1024)})
    assert reply.status == 413 and world.readers == []


def test_import_owner_fields_are_validated(world: World) -> None:
    bad: list[dict[str, Any]] = [
        {"text": "x", "entity_id": ac.ME, "tax_account_id": None, "start_height": -1},
        {"text": "x", "entity_id": ac.ME, "tax_account_id": None, "client_ids": list(range(51))},
        {"text": "x", "tax_account_id": None},
    ]
    for body in bad:
        reply = world.post("/api/imports/addresses", body)
        assert reply.status == 422 and reply.json() == {"error": "invalid request"}
    extra = {"text": "x", "entity_id": ac.ME, "tax_account_id": None, "gap_limit": 5}
    assert world.post("/api/imports/addresses", extra).status == 422  # no gap limit for address lists


@pytest.mark.parametrize("online", [True, False])
def test_the_service_has_a_node_client_only_online_t203(world: World, online: bool) -> None:
    s = service(
        world.db, lambda: world.db, "127.0.0.1", 1, "ro-client", Secret("not-a-real-password"), online=online
    )
    assert s.online is online


def test_the_runtime_serves_imports_and_connects_them_to_the_chain_jobs(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    db = open_db(data)
    record_chain(db, "regtest")
    requested: list[str] = []

    class Jobs:
        def request_sync(self) -> None:
            requested.append("sync")

    rpc = type("Rpc", (), {"host": "127.0.0.1", "port": 1, "user": "ro-client", "password": Secret("x")})()
    rt = runtime.build(
        port=PORT,
        bootstrap_token=TOKEN,
        rpc=rpc,
        volume=None,
        db=db,
        allow_unencrypted=True,
        on_claimed=lambda: None,
        check=lambda *a, **k: ONLINE,
        start_jobs=lambda *a, **k: Jobs(),
        open_reader=functools.partial(open_reader, data),
    )
    session = call(rt.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
    auth = {"Authorization": f"Bearer {session}"}
    wallet = call(
        rt.app, "POST", "/api/tax-accounts", headers=auth, json_body={"name": "W", "kind": "self_custody"}
    )
    body = {"text": p2wpkh(1)[0], "entity_id": ac.ME, "tax_account_id": wallet.json()["id"]}
    assert rt.start_chain_jobs is not None
    rt.start_chain_jobs()  # the launcher starts them after its last checks
    assert call(rt.app, "POST", "/api/imports/addresses", headers=auth, json_body=body).status == 200
    assert requested == ["sync"]
    db.close()


def test_an_address_import_parses_and_writes_under_one_writer_transaction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = world.wallet()
    lock = db_module.writer_lock(world.db)
    assert lock is not None
    held: list[bool] = []
    real = ac.add_addresses

    def watched(*args: Any, **kwargs: Any) -> Any:
        held.append(world.db.in_transaction and lock._is_owned())  # type: ignore[attr-defined]
        return real(*args, **kwargs)

    monkeypatch.setattr(ac, "add_addresses", watched)
    body = {"text": p2wpkh(1)[0], "entity_id": ac.ME, "tax_account_id": wallet}
    assert world.post("/api/imports/addresses", body).status == 200
    assert held == [True]  # the parse and the write share one transaction, under the writer's lock


def test_a_reader_that_fails_its_file_checks_never_shows_the_path_t401(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    secret_path = "/media/veracrypt1/coinacct-data/db.sqlite"

    def failing_reader() -> Any:
        raise DbError(f"{secret_path} was replaced while it was opened (T-401)")

    world.imports._open_reader = failing_reader
    reply = world.get("/api/accounts")
    assert reply.status == 422 and secret_path not in reply.body.decode()
    assert "can't be used right now" in reply.json()["error"]
    assert "replaced" in caplog.text  # the reason is in the log


class AwayNode(Node):
    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append((method, params))
        raise RpcTransportError(method, "connection refused")


def test_a_node_gone_after_start_up_is_a_409_not_a_500(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.imports._rpc = AwayNode()
        reply = w.post("/api/imports/descriptor/preview", {"text": TPUB_DESC})
        assert reply.status == 409 and "didn't answer" in reply.json()["error"]
    finally:
        w.db.close()


def test_another_process_holding_the_write_lock_is_a_503(tmp_path: Path) -> None:
    w = World(tmp_path, timeout=0.05)
    path = tmp_path / "data" / "db.sqlite"
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    try:
        reply = w.post("/api/clients", {"name": "A phone wallet", "kind": "mobile"})
        assert reply.status == 503 and "busy" in reply.json()["error"]
    finally:
        other.execute("ROLLBACK")
        other.close()
        w.db.close()


def test_known_scripts_and_conflicts_reach_the_ui(world: World) -> None:
    wallet = world.wallet()
    exchange = world.post("/api/entities", {"name": "An exchange", "kind": "exchange"}).json()["id"]
    (a1, s1), (a2, s2) = p2wpkh(1), p2wpkh(2)
    ac.add_addresses(world.db, [(s1, a1)], entity_id=exchange, tax_account_id=None, source="manual")
    preview = world.post("/api/imports/addresses/preview", {"text": f"{a1}\n{a2}"}).json()
    assert preview["known"] == [{"script": s1, "address": a1, "entity_id": exchange, "tax_account_id": None}]
    assert preview["new"] == [{"script": s2, "address": a2}]
    body = {"text": f"{a1}\n{a2}", "entity_id": ac.ME, "tax_account_id": wallet}
    assert world.post("/api/imports/addresses", body).json() == {"added": [s2], "conflicts": [s1]}


def test_the_account_overview_is_one_snapshot(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    real = ac.entities

    def watched(conn: Any) -> Any:
        seen.append(conn.in_transaction)
        return real(conn)

    monkeypatch.setattr(ac, "entities", watched)
    assert world.get("/api/accounts").status == 200
    assert seen == [True]  # read inside the reader's snapshot, with the accounts and clients

"""The history routes (PLAN §3): addresses with balances, one address's events, and UTXOs, read from
the chain cache through `services.history.History`. Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.api.app import create_app
from coinacct.api.session import Sessions
from coinacct.services.history import History
from coinacct.services.startup import NodeStatus
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import DbBusy, open_db, open_reader
from tests.unit.services.test_history import TIP, A, B, two_wallets, txid

from .asgi import PORT, Reply, call

__all__ = ["two_wallets"]  # the fixture, shared with the service tests

TOKEN = "launch-token-for-tests"


class World:
    def __init__(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir(mode=0o700)
        data = open_data_dir(str(d))
        self.db = open_db(data)
        record_chain(self.db, "regtest")
        sessions = Sessions(TOKEN)
        self.app = create_app(
            port=PORT,
            sessions=sessions,
            status=lambda: NodeStatus(online=False, chain="regtest", reasons=()),
            on_quit=lambda: None,
            history=History(lambda: open_reader(data)),
        )
        session = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
        self.auth = {"Authorization": f"Bearer {session}"}

    def get(self, path: str) -> Reply:
        return call(self.app, "GET", path, headers=self.auth)

    def post(self, path: str, body: dict[str, Any]) -> Reply:
        return call(self.app, "POST", path, headers=self.auth, json_body=body)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


@pytest.fixture
def conn(world: World) -> sqlite3.Connection:
    return world.db  # for the shared two_wallets fixture


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/api/addresses"), ("POST", "/api/addresses/events"), ("GET", "/api/utxos")]
)
def test_every_history_route_needs_the_session(world: World, method: str, path: str) -> None:
    body = {"script": A[0]} if method == "POST" else None
    assert call(world.app, method, path, json_body=body).status == 401


def test_addresses_carry_balances_and_the_tip_they_are_as_of(
    world: World, two_wallets: tuple[int, int]
) -> None:
    body: dict[str, Any] = world.get("/api/addresses").json()
    assert body["as_of"] == {"blockhash": TIP.blockhash, "height": TIP.height}
    a = next(x for x in body["addresses"] if x["script"] == A[0])
    assert a == {
        "script": A[0],
        "address": A[1],
        "entity_id": 1,
        "tax_account_id": two_wallets[0],
        "label": "savings",
        "balance": 20_000,
        "utxos": 1,
        "received": 70_000,
        "transactions": 3,
        "last_height": 300,
        "scanned_to": None,
    }
    assert body["catching_up"] is False
    hot = world.get(f"/api/addresses?tax_account_id={two_wallets[1]}").json()["addresses"]
    assert [x["script"] for x in hot] == [B[0]]


def test_one_addresses_events(world: World, two_wallets: tuple[int, int]) -> None:
    events = world.post("/api/addresses/events", {"script": A[0]}).json()["events"]
    assert [e["kind"] for e in events] == ["receive", "receive", "spend"]
    assert events[2]["prevout"] == {"txid": txid(1), "vout": 0}


def test_an_unknown_script_is_a_404(world: World, two_wallets: tuple[int, int]) -> None:
    script = "0014" + "cc" * 20
    reply = world.post("/api/addresses/events", {"script": script})
    assert reply.status == 404 and script not in reply.body.decode()


def test_the_script_never_goes_in_a_url_t105(world: World, two_wallets: tuple[int, int]) -> None:
    assert world.get(f"/api/addresses/{A[0]}/events").status in (404, 405)  # no such GET route


@pytest.mark.parametrize("script", ["ZZ", "0014AA", "abc"])
def test_a_malformed_script_is_a_422_without_echo(
    world: World, two_wallets: tuple[int, int], script: str
) -> None:
    reply = world.post("/api/addresses/events", {"script": script})
    assert reply.status == 422 and reply.json() == {"error": "invalid request"}


def test_a_busy_db_is_a_503(tmp_path: Path) -> None:
    def busy() -> sqlite3.Connection:
        raise DbBusy("the user DB is busy; try again in a moment")

    sessions = Sessions(TOKEN)
    app = create_app(
        port=PORT,
        sessions=sessions,
        status=lambda: NodeStatus(online=False, chain=None, reasons=()),
        on_quit=lambda: None,
        history=History(busy),
    )
    session = call(app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
    reply = call(app, "GET", "/api/addresses", headers={"Authorization": f"Bearer {session}"})
    assert reply.status == 503 and "busy" in reply.json()["error"]


def test_utxos_oldest_first(world: World, two_wallets: tuple[int, int]) -> None:
    utxos = world.get("/api/utxos").json()["utxos"]
    assert [(u["txid"], u["vout"], u["sats"]) for u in utxos] == [(txid(2), 1, 20_000), (txid(3), 0, 45_000)]
    assert world.get("/api/utxos?tax_account_id=notanumber").status == 422


def test_without_the_service_there_are_no_history_routes(tmp_path: Path) -> None:
    sessions = Sessions(TOKEN)
    app = create_app(
        port=PORT,
        sessions=sessions,
        status=lambda: NodeStatus(online=False, chain=None, reasons=()),
        on_quit=lambda: None,
    )
    session = call(app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
    assert call(app, "GET", "/api/utxos", headers={"Authorization": f"Bearer {session}"}).status == 404

"""The graph routes (PLAN §4, M3): one transaction and what spent an output, through
`services.graph.Graph`. The node side is faked as in `unit/services/test_graph.py`. Synthetic data."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.api.app import create_app
from coinacct.api.session import Sessions
from coinacct.chain import txs
from coinacct.chain.spenders import Spend, SpendState
from coinacct.domain.chain import Outpoint
from coinacct.services.graph import Graph
from coinacct.services.startup import NodeStatus
from coinacct.storage import chain_cache as cc
from coinacct.storage import tags
from coinacct.storage.datadir import DataDir, open_data_dir
from coinacct.storage.db import open_reader
from tests.unit.services.test_graph import MINE, TIP, FakeChain, h, tx
from tests.unit.services.test_graph import chain as chain_fixture
from tests.unit.services.test_graph import conn as conn_fixture

from .asgi import PORT, Reply, call

__all__ = ["chain_fixture", "conn_fixture"]

TOKEN = "launch-token-for-tests"
chain = chain_fixture
conn = conn_fixture


@pytest.fixture
def data(tmp_path: Path) -> DataDir:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    return open_data_dir(str(d))


class World:
    def __init__(self, data: DataDir, db: sqlite3.Connection, *, online: bool) -> None:
        self.app = create_app(
            port=PORT,
            sessions=Sessions(TOKEN),
            status=lambda: NodeStatus(online=online, chain="regtest", reasons=()),
            on_quit=lambda: None,
            graph=Graph(db, lambda: open_reader(data), object() if online else None),  # type: ignore[arg-type]
        )
        session = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
        self.auth = {"Authorization": f"Bearer {session}"}

    def post(self, path: str, body: dict[str, Any]) -> Reply:
        return call(self.app, "POST", path, headers=self.auth, json_body=body)


@pytest.fixture
def world(data: DataDir, conn: sqlite3.Connection, chain: FakeChain) -> Iterator[World]:
    yield World(data, conn, online=True)


@pytest.mark.parametrize("path", ["/api/graph/tx", "/api/graph/spender"])
def test_every_graph_route_needs_the_session(world: World, path: str) -> None:
    assert call(world.app, "POST", path, json_body={"txid": h(1), "n": 0}).status == 401


def test_a_transaction_comes_with_its_owners(world: World, chain: FakeChain) -> None:
    chain.txs[(h(1), h(490))] = tx(1)
    reply = world.post("/api/graph/tx", {"txid": h(1), "blockhash": h(490)})
    assert reply.status == 200
    body = reply.json()
    assert body["txid"] == h(1) and body["confirmations"] == 11
    assert body["outputs"][0]["script"] == MINE and body["outputs"][0]["owner"]["label"] == "savings"
    assert body["outputs"][0]["owner"]["client_ids"] == []
    assert body["outputs"][1]["owner"] is None
    assert body["inputs"][0]["prevout"] == {"txid": h(1001), "vout": 0}
    assert body["mixing"] is None


def test_a_transaction_carries_the_users_mixing_flag(
    world: World, chain: FakeChain, conn: sqlite3.Connection
) -> None:
    chain.txs[(h(1), h(490))] = tx(1)
    tags.set_mixing(conn, h(1), True, at="2026-10-08T12:00:00+00:00")
    assert world.post("/api/graph/tx", {"txid": h(1), "blockhash": h(490)}).json()["mixing"] is True


def test_a_spend_and_an_unspent_snapshot(world: World, chain: FakeChain, conn: sqlite3.Connection) -> None:
    cc.put_tx(conn, tx(1), 490, TIP)
    chain.spends[Outpoint(h(1), 0)] = Spend(Outpoint(h(1), 0), SpendState.SPENT, h(2), h(495))
    chain.spends[Outpoint(h(1), 1)] = Spend(Outpoint(h(1), 1), SpendState.UNSPENT)
    chain.txs[(h(2), h(495))] = tx(2, block=495, confirmations=6, spends=Outpoint(h(1), 0))
    spent = world.post("/api/graph/spender", {"txid": h(1), "blockhash": h(490), "n": 0}).json()
    assert spent == {"state": "spent", "spending_txid": h(2), "blockhash": h(495), "as_of": None}
    unspent = world.post("/api/graph/spender", {"txid": h(1), "blockhash": h(490), "n": 1}).json()
    assert unspent["state"] == "unspent" and unspent["as_of"] == {
        "blockhash": TIP.blockhash,
        "height": TIP.height,
    }


def test_an_unknown_transaction_is_a_404_that_doesnt_repeat_it(world: World, chain: FakeChain) -> None:
    chain.raise_on_fetch = txs.TxNotFoundError("no such tx " + h(7))
    reply = world.post("/api/graph/tx", {"txid": h(7), "blockhash": None})
    assert reply.status == 404 and h(7) not in reply.body.decode()


def test_offline_an_uncached_transaction_is_a_409_t203(data: DataDir, conn: sqlite3.Connection) -> None:
    world = World(data, conn, online=False)
    reply = world.post("/api/graph/tx", {"txid": h(1), "blockhash": h(490)})
    assert reply.status == 409


@pytest.mark.parametrize(
    "body",
    [
        {"txid": "zz" * 32},
        {"txid": "AB" * 32},
        {"txid": h(1), "blockhash": "00"},
        {"txid": h(1), "extra": 1},
        {"txid": h(1), "n": -1},
    ],
)
def test_malformed_queries_are_refused_without_echo(world: World, body: dict[str, Any]) -> None:
    path = "/api/graph/spender" if "n" in body else "/api/graph/tx"
    reply = world.post(path, body)
    assert reply.status == 422 and reply.json() == {"error": "invalid request"}

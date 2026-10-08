"""The tag routes (PLAN §4, M3): an address's owner, account, label and clients; a transaction's
mixing flag; and the change log, through `services.tags.Tagging`. Synthetic data only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.api.app import create_app
from coinacct.api.session import Sessions
from coinacct.services.imports import Imports
from coinacct.services.startup import NodeStatus
from coinacct.services.tags import Tagging
from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db, open_reader

from .asgi import PORT, Reply, call

TOKEN = "launch-token-for-tests"
SCRIPT = "0014" + "cc" * 20
TXID = "ab" * 32


class World:
    def __init__(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir(mode=0o700)
        data = open_data_dir(str(d))
        self.db = open_db(data)
        record_chain(self.db, "regtest")
        imports = Imports(self.db, lambda: open_reader(data), None)
        self.app = create_app(
            port=PORT,
            sessions=Sessions(TOKEN),
            status=lambda: NodeStatus(online=False, chain="regtest", reasons=()),
            on_quit=lambda: None,
            tagging=Tagging(
                self.db, lambda: open_reader(data), imports, now=lambda: "2026-10-08T12:00:00+00:00"
            ),
        )
        session = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
        self.auth = {"Authorization": f"Bearer {session}"}

    def post(self, path: str, body: dict[str, Any]) -> Reply:
        return call(self.app, "POST", path, headers=self.auth, json_body=body)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


@pytest.fixture
def conn(world: World) -> sqlite3.Connection:
    return world.db


@pytest.mark.parametrize("path", ["/api/tags/address", "/api/tags/mixing", "/api/tags/history"])
def test_every_tag_route_needs_the_session(world: World, path: str) -> None:
    assert call(world.app, "POST", path, json_body={}).status == 401


def test_an_address_is_tagged_retagged_and_its_history_read(world: World, conn: sqlite3.Connection) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    wallet = ac.add_tax_account(conn, "Cold storage", "self_custody")
    first = world.post("/api/tags/address", {"script": SCRIPT, "entity_id": exchange})
    assert first.status == 200 and first.json() == {"new": True}
    second = world.post(
        "/api/tags/address", {"script": SCRIPT, "entity_id": ME, "tax_account_id": wallet, "label": "mine"}
    )
    assert second.json() == {"new": False}
    changes = world.post("/api/tags/history", {"kind": "address_tag", "subject": SCRIPT}).json()["changes"]
    assert [c["after"]["entity_id"] for c in changes] == [exchange, ME]
    assert changes[1]["before"]["entity_id"] == exchange


def test_the_mixing_flag_is_set_and_logged(world: World) -> None:
    assert world.post("/api/tags/mixing", {"txid": TXID, "mixing": True}).status == 200
    [change] = world.post("/api/tags/history", {"kind": "tx_flag", "subject": TXID}).json()["changes"]
    assert change == {
        "at": "2026-10-08T12:00:00+00:00",
        "kind": "tx_flag",
        "before": None,
        "after": {"mixing": True, "source": "user"},
    }


def test_a_refused_tag_is_a_422_without_echo(world: World) -> None:
    reply = world.post("/api/tags/address", {"script": SCRIPT, "entity_id": ME})  # no account
    assert reply.status == 422 and SCRIPT not in reply.body.decode()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/tags/address", {"script": "zz", "entity_id": 1}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 0}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "label": "x" * 201}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "extra": 1}),
        ("/api/tags/mixing", {"txid": "AB" * 32, "mixing": True}),
        ("/api/tags/mixing", {"txid": TXID}),
        ("/api/tags/history", {"kind": "address_tag", "subject": "not hex"}),
        ("/api/tags/history", {"subject": TXID}),
        ("/api/tags/history", {"kind": "event", "subject": TXID}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 2**63}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "tax_account_id": 2**63}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "client_ids": [2**63]}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "client_ids": [0]}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "address": ""}),
        ("/api/tags/address", {"script": SCRIPT, "entity_id": 1, "address": "a" * 91}),
    ],
)
def test_malformed_bodies_are_refused_without_echo(world: World, path: str, body: dict[str, Any]) -> None:
    reply = world.post(path, body)
    assert reply.status == 422 and reply.json() == {"error": "invalid request"}


@pytest.mark.parametrize("field", ["label", "address"])
def test_a_private_key_in_a_tag_is_refused_without_echo_t703(
    world: World, conn: sqlite3.Connection, field: str
) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    wif_shaped = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")[:52]
    reply = world.post("/api/tags/address", {"script": SCRIPT, "entity_id": exchange, field: wif_shaped})
    assert reply.status == 422 and wif_shaped not in reply.body.decode()
    assert ac.addresses(conn) == []


def test_an_address_text_that_isnt_the_scripts_is_a_422_without_echo_t701(
    world: World, conn: sqlite3.Connection
) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    other = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"  # pays to another script than SCRIPT
    reply = world.post("/api/tags/address", {"script": SCRIPT, "entity_id": exchange, "address": other})
    assert reply.status == 422 and reply.json() == {"error": "that address doesn't pay to that script"}
    assert ac.addresses(conn) == []


def test_one_addresss_history_route_returns_only_its_own_changes(
    world: World, conn: sqlite3.Connection
) -> None:
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    other = "0014" + "dd" * 20
    for script in (SCRIPT, other):
        world.post("/api/tags/address", {"script": script, "entity_id": exchange, "label": script[-2:]})
    changes = world.post("/api/tags/history", {"kind": "address_tag", "subject": SCRIPT}).json()["changes"]
    assert [c["after"]["label"] for c in changes] == [SCRIPT[-2:]]

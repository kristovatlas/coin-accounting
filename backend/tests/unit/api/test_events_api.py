"""The event routes (PLAN §2, §7; M6): the user's events and lot choices, through `services.events.Events`.
Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.api.app import create_app
from coinacct.api.session import Sessions
from coinacct.services.events import Events
from coinacct.services.startup import NodeStatus
from coinacct.storage import accounts as ac
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db, open_reader

from .asgi import PORT, Reply, call

TOKEN = "launch-token-for-tests"
WIF = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"  # the well-known example key, not anyone's


class World:
    def __init__(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir(mode=0o700)
        data = open_data_dir(str(d))
        self.db = open_db(data)
        record_chain(self.db, "regtest")
        self.wallet = ac.add_tax_account(self.db, "Wallet", "self_custody")
        exchange = ac.add_entity(self.db, "Some exchange", "exchange")
        self.exchange = ac.add_tax_account(self.db, "Exchange", "custodial", entity_id=exchange)
        self.app = create_app(
            port=PORT,
            sessions=Sessions(TOKEN),
            status=lambda: NodeStatus(online=False, chain="regtest", reasons=()),
            on_quit=lambda: None,
            events=Events(self.db, lambda: open_reader(data), now=lambda: "2026-10-14T12:00:00+00:00"),
        )
        session = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
        self.auth = {"Authorization": f"Bearer {session}"}

    def post(self, path: str, body: dict[str, Any]) -> Reply:
        return call(self.app, "POST", path, headers=self.auth, json_body=body)

    def get(self, path: str) -> Reply:
        return call(self.app, "GET", path, headers=self.auth)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


def buy(world: World, **kw: Any) -> dict[str, Any]:
    return {
        "kind": "buy",
        "account": world.wallet,
        "at": "2025-03-01T12:00:00Z",
        "sats": 1000,
        "usd": "10.00",
        **kw,
    }


def test_an_event_is_added_listed_edited_and_removed(world: World) -> None:
    added = world.post("/api/events", buy(world, note="first"))
    assert added.status == 201
    event_id = added.json()["id"]
    (listed,) = world.get("/api/events").json()["events"]
    assert (listed["id"], listed["kind"], listed["usd"], listed["at"], listed["note"]) == (
        event_id,
        "buy",
        "10.00",
        "2025-03-01T12:00:00+00:00",
        "first",
    )
    assert world.post("/api/events/update", {"id": event_id, "event": buy(world, usd="12.50")}).status == 200
    assert world.get("/api/events").json()["events"][0]["usd"] == "12.50"
    assert world.post("/api/events/remove", {"id": event_id}).status == 200
    assert world.get("/api/events").json()["events"] == []


def test_a_choice_of_lots_is_set_and_listed(world: World) -> None:
    sale = world.post(
        "/api/events",
        {
            "kind": "sell",
            "account": world.exchange,
            "at": "2025-03-01T12:00:00Z",
            "sats": 1000,
            "usd": "50.00",
        },
    ).json()["id"]
    body = {"id": sale, "choices": [{"lot": "e1", "sats": 1000}], "identified_at": "2025-03-01T11:00:00Z"}
    assert world.post("/api/events/identify", body).status == 200
    (listed,) = world.get("/api/events").json()["events"]
    assert (listed["choices"], listed["identified_at"]) == (
        [{"lot": "e1", "sats": 1000}],
        "2025-03-01T11:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"usd": "10.001"}, 422),  # not exact cents
        ({"at": "2025-03-01T12:00:00"}, 422),  # no time zone
        ({"sats": 0}, 422),
        ({"kind": "swap"}, 422),
        ({"extra": 1}, 422),
        ({"to": 2}, 422),  # a buy has no destination: the storage layer refuses it
        ({"account": 999}, 422),  # an account that doesn't exist
        ({"note": f"backup {WIF}"}, 422),  # a private key in free text (T-703)
    ],
)
def test_invalid_events_are_refused_without_echoing_them(
    world: World, body: dict[str, Any], status: int
) -> None:
    reply = world.post("/api/events", buy(world, **body))
    assert reply.status == status
    assert WIF.encode() not in reply.body and b"10.001" not in reply.body
    assert world.get("/api/events").json()["events"] == []


def test_the_routes_need_a_session(world: World) -> None:
    assert call(world.app, "GET", "/api/events").status == 401
    assert call(world.app, "POST", "/api/events", json_body=buy(world)).status == 401


def test_an_unknown_event_is_refused(world: World) -> None:
    assert world.post("/api/events/remove", {"id": 12345}).status == 422

"""The event routes (PLAN §2, §7; M6): the user's events and lot choices, through `services.events.Events`.
Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
        self.wallet2 = ac.add_tax_account(self.db, "Cold wallet", "self_custody")
        other = ac.add_entity(self.db, "Another exchange", "exchange")
        self.exchange2 = ac.add_tax_account(self.db, "Exchange 2", "custodial", entity_id=other)
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


def sell(world: World, **kw: Any) -> dict[str, Any]:
    return {
        "kind": "sell",
        "account": world.exchange,
        "at": "2025-03-01T12:00:00Z",
        "sats": 1000,
        "usd": "50.00",
        **kw,
    }


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


def test_a_choice_of_lots_is_set_now_and_listed_adr0008(world: World) -> None:
    sale = world.post("/api/events", sell(world)).json()["id"]
    assert (
        world.post("/api/events/identify", {"id": sale, "choices": [{"lot": "7", "sats": 1000}]}).status
        == 200
    )
    (listed,) = world.get("/api/events").json()["events"]
    # the app records when the choice was made: its clock, never the client's say (T-508)
    assert (listed["choices"], listed["identified_at"]) == (
        [{"lot": "7", "sats": 1000}],
        "2026-10-14T12:00:00+00:00",
    )
    assert world.post("/api/events/identify", {"id": sale, "choices": []}).status == 200
    assert world.get("/api/events").json()["events"][0]["choices"] == []
    reply = world.post(
        "/api/events/identify",
        {"id": sale, "choices": [{"lot": "7", "sats": 1000}], "identified_at": "2025-01-01T00:00:00Z"},
    )
    assert reply.status == 422  # the body can't say when


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"usd": "10.001"}, 422),  # not exact cents
        ({"at": "2025-03-01T12:00:00"}, 422),  # no time zone
        ({"sats": 0}, 422),
        ({"sats": "1000"}, 422),  # strict: no number as text
        ({"at": 1740830400}, 422),  # strict: no Unix time for a moment
        ({"valuation_source": f"from {WIF}"}, 422),  # a private key in free text (T-703)
        ({"at": "0001-01-01T00:00:00+05:00"}, 422),  # a moment at the calendar's edge
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


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/events"),
        ("POST", "/api/events"),
        ("POST", "/api/events/update"),
        ("POST", "/api/events/remove"),
        ("POST", "/api/events/identify"),
    ],
)
def test_every_route_needs_a_session_t104(world: World, method: str, path: str) -> None:
    assert call(world.app, method, path, json_body={}).status == 401


def test_an_unknown_event_is_not_found(world: World) -> None:
    assert world.post("/api/events/remove", {"id": 12345}).status == 404
    assert world.post("/api/events/update", {"id": 12345, "event": buy(world)}).status == 404
    assert world.post("/api/events/identify", {"id": 12345, "choices": []}).status == 404


@pytest.mark.parametrize(
    "choices",
    [
        [{"lot": "7", "sats": 600}],  # short of the sale
        [{"lot": WIF, "sats": 1000}],  # a key, though in the lot-id alphabet (T-703)
        [{"lot": "7 ; x", "sats": 1000}],
    ],
)
def test_invalid_choices_are_refused_and_nothing_is_logged(world: World, choices: list[Any]) -> None:
    sale = world.post("/api/events", sell(world)).json()["id"]
    before = world.db.execute("SELECT count(*) FROM change_log").fetchone()
    reply = world.post("/api/events/identify", {"id": sale, "choices": choices})
    assert reply.status == 422
    assert WIF.encode() not in reply.body
    assert world.db.execute("SELECT count(*) FROM change_log").fetchone() == before


def test_an_edit_with_a_key_or_a_misfit_is_refused(world: World) -> None:
    sale = world.post("/api/events", sell(world)).json()["id"]
    world.post("/api/events/identify", {"id": sale, "choices": [{"lot": "7", "sats": 1000}]})
    assert world.post("/api/events/update", {"id": sale, "event": sell(world, note=WIF)}).status == 422
    assert world.post("/api/events/update", {"id": sale, "event": buy(world)}).status == 422  # its choice


def test_every_field_round_trips_and_events_list_by_moment(world: World) -> None:
    gift = {
        "kind": "gift_in",
        "account": world.wallet,
        "at": "2025-02-01T00:00:00Z",
        "sats": 10,
        "fmv": "1.00",
        "donor_acquired": "2020-01-01",
        "donor_always_long": True,
    }
    withdrawal = {
        "kind": "withdrawal",
        "account": world.exchange,
        "to": world.wallet,
        "at": "2025-01-01T00:00:00Z",
        "sats": 10,
        "fee_sats": 1,
        "missing_basis": "2.00",
        "missing_acquired": "2024-01-01",
    }
    world.post("/api/events", gift)
    world.post("/api/events", withdrawal)
    listed = world.get("/api/events").json()["events"]
    assert [e["kind"] for e in listed] == ["withdrawal", "gift_in"]
    assert (
        listed[0]["to"],
        listed[0]["fee_sats"],
        listed[0]["missing_basis"],
        listed[0]["missing_acquired"],
    ) == (
        world.wallet,
        1,
        "2.00",
        "2024-01-01",
    )
    assert (
        listed[1]["usd"],
        listed[1]["fmv"],
        listed[1]["donor_acquired"],
        listed[1]["donor_always_long"],
    ) == (
        None,
        "1.00",
        "2020-01-01",
        True,
    )


def test_the_change_log_records_the_apps_clock(world: World) -> None:
    world.post("/api/events", buy(world))
    assert world.db.execute("SELECT at, kind, origin FROM change_log").fetchall() == [
        ("2026-10-14T12:00:00+00:00", "event", "user"),
    ]


@pytest.mark.parametrize(
    ("kind", "source", "target", "status"),
    [
        ("deposit", "wallet", "exchange", 201),
        ("deposit", "exchange", "wallet", 422),
        ("withdrawal", "exchange", "wallet", 201),
        ("withdrawal", "wallet", "exchange", 422),
        ("self_transfer", "wallet", "exchange", 422),
        ("self_transfer", "wallet", "wallet2", 201),
        ("withdrawal", "exchange", "exchange2", 201),  # straight from one exchange into another
        ("deposit", "exchange", "exchange2", 422),
        ("self_transfer", "exchange", "exchange2", 422),
    ],
)
def test_a_movement_goes_the_way_its_kind_says(
    world: World, kind: str, source: str, target: str, status: int
) -> None:
    body = {
        "kind": kind,
        "account": getattr(world, source),
        "to": getattr(world, target),
        "at": "2025-03-01T12:00:00Z",
        "sats": 10,
    }
    assert world.post("/api/events", body).status == status


def test_ids_are_strict_integers(world: World) -> None:
    world.post("/api/events", buy(world))
    assert world.post("/api/events/remove", {"id": True}).status == 422
    assert world.post("/api/events", buy(world, account=str(world.wallet))).status == 422
    assert len(world.get("/api/events").json()["events"]) == 1


def test_an_edit_the_wrong_way_is_refused_and_nothing_changes(world: World) -> None:
    body = {
        "kind": "deposit",
        "account": world.wallet,
        "to": world.exchange,
        "at": "2025-03-01T12:00:00Z",
        "sats": 10,
    }
    deposit = world.post("/api/events", body).json()["id"]
    before = (world.get("/api/events").json(), world.db.execute("SELECT count(*) FROM change_log").fetchone())
    assert (
        world.post("/api/events/update", {"id": deposit, "event": {**body, "to": world.wallet2}}).status
        == 422
    )
    assert world.post("/api/events/update", {"id": deposit, "event": {**body, "note": WIF}}).status == 422
    after = (world.get("/api/events").json(), world.db.execute("SELECT count(*) FROM change_log").fetchone())
    assert after == before


def test_a_gifts_donor_date_is_iso_text_only(world: World) -> None:
    gift = {
        "kind": "gift_in",
        "account": world.wallet,
        "at": "2025-02-01T00:00:00Z",
        "sats": 10,
        "fmv": "1.00",
        "donor_acquired": 1577836800,
    }
    assert world.post("/api/events", gift).status == 422
    assert world.get("/api/events").json()["events"] == []


def test_the_apps_clock_rounds_up_to_the_second_t508() -> None:
    from coinacct.services.events import _second_up  # noqa: PLC0415 - the one private helper tested

    sale = datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)
    assert _second_up(sale) == sale
    assert _second_up(sale.replace(microsecond=1)) == sale + timedelta(seconds=1)  # never before the sale

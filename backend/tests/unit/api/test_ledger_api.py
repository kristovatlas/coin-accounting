"""The ledger route (PLAN §7, ADR 0021 §1; M6): the lot engine's run of the stored events, through
`services.ledger.Ledger`. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.api.app import _usd, create_app
from coinacct.api.session import Sessions
from coinacct.services.events import Events
from coinacct.services.ledger import Ledger
from coinacct.services.startup import NodeStatus
from coinacct.storage import accounts as ac
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db, open_reader

from .asgi import PORT, Reply, call

TOKEN = "launch-token-for-tests"
NOW = "2026-10-14T12:00:00+00:00"


class World:
    def __init__(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir(mode=0o700)
        data = open_data_dir(str(d))
        self.db = open_db(data)
        record_chain(self.db, "regtest")
        self.wallet = ac.add_tax_account(self.db, "Wallet", "self_custody")  # automatic, the default
        self.manual = ac.add_tax_account(self.db, "Manual wallet", "self_custody", automatic=False)
        exchange = ac.add_entity(self.db, "Some exchange", "exchange")
        self.exchange = ac.add_tax_account(self.db, "Exchange", "custodial", entity_id=exchange)
        reader = lambda: open_reader(data)  # noqa: E731 - one opener for both services
        self.app = create_app(
            port=PORT,
            sessions=Sessions(TOKEN),
            status=lambda: NodeStatus(online=False, chain="regtest", reasons=()),
            on_quit=lambda: None,
            events=Events(self.db, reader, now=lambda: NOW),
            ledger=Ledger(reader),
        )
        session = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
        self.auth = {"Authorization": f"Bearer {session}"}

    def add(self, kind: str, account: int, at: str, sats: int, **kw: Any) -> int:
        reply = call(
            self.app,
            "POST",
            "/api/events",
            headers=self.auth,
            json_body={"kind": kind, "account": account, "at": at, "sats": sats, **kw},
        )
        assert reply.status == 201, reply.body
        event_id: int = reply.json()["id"]
        return event_id

    def identify(self, event_id: int, *choices: tuple[int | str, int]) -> None:
        body = {"id": event_id, "choices": [{"lot": str(lot), "sats": sats} for lot, sats in choices]}
        assert call(self.app, "POST", "/api/events/identify", headers=self.auth, json_body=body).status == 200

    def ledger(self) -> Reply:
        return call(self.app, "GET", "/api/ledger", headers=self.auth)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


def test_the_ledger_needs_a_session_t104(world: World) -> None:
    assert call(world.app, "GET", "/api/ledger").status == 401


def test_automatic_mode_shows_the_lots_it_used_adr0021(world: World) -> None:
    a = world.add("buy", world.wallet, "2024-01-02T12:00:00Z", 1_000, usd="10.00")
    b = world.add("buy", world.wallet, "2024-02-02T12:00:00Z", 1_000, usd="20.00")
    spend = world.add("spend", world.wallet, "2025-03-01T12:00:00Z", 1_000, usd="60.00")
    reply = world.ledger()
    assert reply.status == 200
    out = reply.json()
    assert out["allocations"] == [
        {
            "event": spend,
            "lot": str(a),
            "sats": 1_000,
            "basis": "10.00",
            "proceeds": "60.00",
            "acquired": "2024-01-02",
            "disposed": "2025-03-01",
            "long_term": True,
            "rule": "cost",
        }
    ]
    assert out["holdings"] == [
        {
            "lot": str(b),
            "account": world.wallet,
            "acquired": "2024-02-02",
            "sats": 1_000,
            "basis": "20.00",
            "always_long": False,
            "unknown_basis": False,
        }
    ]
    assert (out["automatic"], out["stale"], out["late"], out["blocking"], out["replay_stopped"]) == (
        [],
        [],
        [],
        [],
        None,
    )


def test_a_late_choice_comes_with_the_standing_methods_figures_adr0008(world: World) -> None:
    a = world.add("buy", world.manual, "2024-01-02T12:00:00Z", 1_000, usd="10.00")
    b = world.add("buy", world.manual, "2024-02-02T12:00:00Z", 1_000, usd="20.00")
    spend = world.add("spend", world.manual, "2025-03-01T12:00:00Z", 1_000, usd="60.00")
    world.identify(spend, (b, 1_000))  # now, after the spend: late, and not what FIFO would use
    out = world.ledger().json()
    assert [(r["event"], r["lot"]) for r in out["allocations"]] == [(spend, str(b))]  # the choice is used
    (late,) = out["late"]
    assert (late["event"], late["identified_at"], late["replayed"]) == (spend, NOW, True)
    assert [(p["event"], p["lot"], p["basis"]) for p in late["standing"]] == [(spend, str(a), "10.00")]


def test_moves_gifts_and_what_blocks_a_report(world: World) -> None:
    a = world.add("buy", world.wallet, "2024-01-02T12:00:00Z", 1_000, usd="10.00")
    deposit = world.add("deposit", world.wallet, "2024-03-02T12:00:00Z", 600, to=world.exchange, fee_sats=100)
    gift = world.add("gift_out", world.wallet, "2024-04-02T12:00:00Z", 400, usd="0.00")
    short = world.add("sell", world.exchange, "2024-05-02T12:00:00Z", 900, usd="45.00")
    out = world.ledger().json()
    (move,) = out["moves"]
    assert (move["event"], move["lot"], move["into"], move["to"], move["sats"]) == (
        deposit,
        str(a),
        f"{a}@{deposit}",
        world.exchange,
        500,
    )
    assert [(g["event"], g["lot"], g["sats"], g["unknown_basis"]) for g in out["gifts"]] == [
        (gift, str(a), 400, False)
    ]
    assert out["blocking"] == [{"kind": "missing_lots", "event": short, "sats": 400}]


def test_an_automatic_accounts_stored_choice_is_listed_unused(world: World) -> None:
    a = world.add("buy", world.wallet, "2024-01-02T12:00:00Z", 1_000, usd="10.00")
    b = world.add("buy", world.wallet, "2024-02-02T12:00:00Z", 1_000, usd="20.00")
    spend = world.add("spend", world.wallet, "2025-03-01T12:00:00Z", 1_000, usd="60.00")
    world.identify(spend, (b, 1_000))
    out = world.ledger().json()
    assert (out["automatic"], out["stale"], out["late"]) == ([spend], [], [])
    assert [r["lot"] for r in out["allocations"]] == [str(a)]  # FIFO's oldest, not the stored choice


def test_a_damaged_stored_event_is_a_422_with_the_remedy_t408(world: World) -> None:
    world.add("buy", world.wallet, "2024-01-02T12:00:00Z", 1_000, usd="10.00")
    world.db.execute("UPDATE event SET usd = '1.001'")  # damage that bypasses the module
    reply = world.ledger()
    assert reply.status == 422
    assert reply.json() == {"error": "a stored event is damaged (T-408): restore the user DB from a backup"}


@pytest.mark.parametrize(("amount", "text"), [("1E+2", "100"), ("0.10", "0.10"), ("-5.00", "-5.00")])
def test_money_is_written_without_an_exponent(amount: str, text: str) -> None:
    assert _usd(Decimal(amount)) == text

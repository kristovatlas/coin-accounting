"""The user's events and lot identifications in the user DB (migration m0011; PLAN §2; THREAT_MODEL T-408,
T-501, T-508). Synthetic data only."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.domain.keys import PrivateKeyError
from coinacct.storage import accounts as ac
from coinacct.storage import change_log, tags
from coinacct.storage import events as ev
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from coinacct.storage.migrations import STEPS

AT = "2026-10-14T12:00:00+00:00"
D = Decimal
WHEN = datetime(2025, 3, 1, 12, tzinfo=UTC)
TXID = "ab" * 32


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


@pytest.fixture
def accounts(conn: sqlite3.Connection) -> tuple[int, int]:
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    return wallet, ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange)


def buy(account: int, **kw: Any) -> ev.Event:
    return ev.Event(
        "buy", account, kw.pop("at", WHEN), kw.pop("sats", 1_000), kw.pop("usd", D("10.00")), **kw
    )


def logged(conn: sqlite3.Connection, kind: str, subject: str) -> list[tuple[Any, Any, str]]:
    rows = conn.execute(
        "SELECT before, after, origin FROM change_log WHERE kind = ? AND subject = ? ORDER BY id",
        (kind, subject),
    ).fetchall()
    return [(None if b is None else json.loads(b), json.loads(a), o) for b, a, o in rows]


def test_an_event_is_stored_read_back_and_logged_t408(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    event = buy(wallet, valuation_source="exchange fill", txid=TXID, note="first buy")
    event_id = ev.add_event(conn, event, at=AT)
    assert ev.events(conn) == [ev.Stored(event_id, event)]
    ((before, after, origin),) = logged(conn, "event", str(event_id))
    assert (before, after["kind"], after["usd"], after["sats"], origin) == (
        None,
        "buy",
        "10.00",
        1_000,
        "user",
    )


def test_an_edit_is_logged_with_before_and_after_and_a_repeat_is_not(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    event_id = ev.add_event(conn, buy(wallet), at=AT)
    ev.update_event(conn, event_id, buy(wallet, usd=D("12.50")), at=AT)
    ev.update_event(conn, event_id, buy(wallet, usd=D("12.50")), at=AT)  # the same values: no row
    rows = logged(conn, "event", str(event_id))
    assert [(b and b["usd"], a["usd"]) for b, a, _ in rows] == [(None, "10.00"), ("10.00", "12.50")]
    assert ev.events(conn)[0].event.usd == D("12.50")


def test_a_moment_in_another_zone_is_stored_as_utc(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    local = datetime(2025, 3, 1, 7, tzinfo=timezone(timedelta(hours=-5)))
    ev.add_event(conn, buy(wallet, at=local), at=AT)
    (stored,) = ev.events(conn)
    assert stored.event.at == WHEN and stored.event.at.utcoffset() == timedelta(0)


def test_events_read_in_the_order_of_their_moments(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    late = ev.add_event(conn, buy(wallet, at=WHEN + timedelta(days=1)), at=AT)
    early = ev.add_event(conn, buy(wallet, at=WHEN), at=AT)
    assert [s.id for s in ev.events(conn)] == [early, late]


def test_every_kind_round_trips(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, exchange = accounts
    kinds = [
        ev.Event("gift_in", wallet, WHEN, 500, None, fmv=D("4.00"), donor_acquired=date(2020, 1, 1)),
        ev.Event(
            "gift_in",
            wallet,
            WHEN,
            500,
            D("6.00"),
            fmv=D("4.00"),
            donor_acquired=date(2020, 1, 1),
            donor_always_long=True,
        ),
        ev.Event("income", wallet, WHEN, 500, D("5.00"), valuation_source="payroll"),
        ev.Event("inherit", wallet, WHEN, 500, D("5.00")),
        ev.Event("p2p_buy", wallet, WHEN, 500, D("5.00")),
        ev.Event("buy", wallet, WHEN, 500, D("5.00"), fee_sats=1),  # a withheld BTC fee (ADR 0043)
        ev.Event("deposit", wallet, WHEN, 500, to=exchange, fee_sats=10),
        ev.Event(
            "withdrawal",
            exchange,
            WHEN,
            500,
            to=wallet,
            missing_basis=D("1.00"),
            missing_acquired=date(2024, 1, 1),
        ),
        ev.Event("self_transfer", wallet, WHEN, 500, to=exchange),
        ev.Event("sell", exchange, WHEN, 500, D("-0.50")),  # costs above proceeds: negative
        ev.Event("spend", wallet, WHEN, 500, D("5.00")),
        ev.Event("gift_out", wallet, WHEN, 500, D("0.00")),
    ]
    ids = [ev.add_event(conn, e, at=AT) for e in kinds]
    assert [s.event for s in ev.events(conn)] == kinds
    assert [s.id for s in ev.events(conn)] == ids


@pytest.mark.parametrize(
    ("event", "match"),
    [
        (ev.Event("buy", 1, WHEN, 0, D("1.00")), "whole sats"),
        (ev.Event("buy", 1, WHEN, 10, D("1.001")), "exact cents"),
        (ev.Event("buy", 1, WHEN, 10, D("-1.00")), "exact cents"),
        (ev.Event("buy", 1, WHEN, 10, None), "USD amount"),
        (ev.Event("buy", 1, datetime(2025, 3, 1), 10, D("1.00")), "timezone-aware"),
        (ev.Event("buy", 1, WHEN, 10, D("1.00"), to=2), "destination"),
        (ev.Event("deposit", 1, WHEN, 10), "destination"),
        (ev.Event("deposit", 1, WHEN, 10, D("1.00"), to=2), "no USD amount"),
        (ev.Event("deposit", 1, WHEN, 10, to=2, fee_sats=10), "fewer than"),
        (ev.Event("buy", 1, WHEN, 10, D("1.00"), fmv=D("1.00")), "only a gift"),
        (ev.Event("buy", 1, WHEN, 10, D("1.00"), donor_acquired=date(2020, 1, 1)), "only a gift"),
        (
            ev.Event("gift_in", 1, WHEN, 10, D("1.00"), fmv=D("1.00"), donor_acquired=date(2026, 1, 1)),
            "from the genesis block to the gift",
        ),
        (
            ev.Event(
                "buy", 1, WHEN, 10, D("1.00"), missing_basis=D("1.00"), missing_acquired=date(2024, 1, 1)
            ),
            "only a withdrawal",
        ),
        (ev.Event("buy", 1, WHEN, 10, D("1.00"), txid="AB" * 32), "txid"),
        (ev.Event("buy", 1, WHEN, 10, D("1.00"), note=""), "note"),
        (
            ev.Event("buy", 1, datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5))), 10, D("1.00")),
            "within the calendar",
        ),
        (ev.Event("income", 1, WHEN, 10, D("1.00"), fee_sats=1), "bears a BTC fee"),
        (ev.Event("sell", 1, WHEN, 10, D("1.00"), fee_sats=1), "isn't supported yet"),  # the engine's next
        (ev.Event("gift_out", 1, WHEN, 10, D("0.00"), fee_sats=1), "isn't supported yet"),
        (ev.Event("deposit", 1, WHEN, 10, to=1), "goes to another account"),
        (ev.Event("gift_out", 1, WHEN, 10, D("1.00")), "no proceeds"),
        (
            ev.Event("gift_in", 1, WHEN, 10, D("1.00"), fmv=D("1.00"), donor_acquired=date(2008, 1, 1)),
            "genesis",
        ),
        (
            ev.Event(
                "withdrawal", 1, WHEN, 10, to=2, missing_basis=D("1.00"), missing_acquired=date(2026, 1, 1)
            ),
            "genesis block to the withdrawal",
        ),
        (ev.Event("buy", True, WHEN, 10, D("1.00")), "account id"),
        (ev.Event("buy", None, WHEN, 10, D("1.00")), "account id"),  # type: ignore[arg-type]
        (ev.Event("swap", 1, WHEN, 10, D("1.00")), "unknown kind"),  # type: ignore[arg-type]
    ],
)
def test_invalid_events_are_refused_before_anything_is_written(
    conn: sqlite3.Connection, accounts: tuple[int, int], event: ev.Event, match: str
) -> None:
    with pytest.raises(ev.EventError, match=match):
        ev.add_event(conn, event, at=AT)
    assert ev.events(conn) == []
    assert conn.execute("SELECT count(*) FROM change_log WHERE kind = 'event'").fetchone() == (0,)


def test_the_schema_refuses_what_the_module_would(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    for sql in (
        "INSERT INTO event (kind, tax_account_id, at, sats)"
        " VALUES ('deposit', ?, '2025-03-01T12:00:00+00:00', 5)",
        "INSERT INTO event (kind, tax_account_id, at, sats)"
        " VALUES ('buy', ?, '2025-03-01T12:00:00+00:00', 5)",
        "INSERT INTO event (kind, tax_account_id, at, sats, usd)"
        " VALUES ('gift_in', ?, '2025-03-01T12:00:00+00:00', 5, NULL)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (wallet,))
    with pytest.raises(sqlite3.IntegrityError):  # an FMV at a gift on a buy
        conn.execute(
            "INSERT INTO event (kind, tax_account_id, at, sats, usd, fmv)"
            " VALUES ('buy', ?, '2025-03-01T12:00:00+00:00', 5, '1.00', '1.00')",
            (wallet,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event (kind, tax_account_id, to_account_id, at, sats)"
            " VALUES ('deposit', ?, ?, ?, 5)",
            (wallet, wallet, "2025-03-01T12:00:00+00:00"),
        )
    ev.add_event(conn, buy(exchange), at=AT)
    with pytest.raises(sqlite3.IntegrityError):  # an account in use can't be deleted
        conn.execute("DELETE FROM tax_account WHERE id = ?", (exchange,))


def test_a_choice_of_lots_is_stored_replaced_whole_and_logged_t508(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    first = (ev.Choice("e1", 600), ev.Choice("e2@t", 400))
    ev.identify(conn, sale, first, WHEN - timedelta(hours=1), at=AT)
    assert ev.identification(conn, sale) == (first, WHEN - timedelta(hours=1))
    ev.identify(conn, sale, (ev.Choice("e2@t", 1_000),), WHEN, at=AT)
    assert ev.identification(conn, sale) == ((ev.Choice("e2@t", 1_000),), WHEN)
    ev.identify(conn, sale, (), None, at=AT)  # back to the standing method
    assert ev.identification(conn, sale) == ((), None)
    rows = logged(conn, "identification", str(sale))
    assert [(b and len(b["choices"]), a and len(a["choices"])) for b, a, _ in rows] == [
        (None, 2),
        (2, 1),
        (1, None),
    ]


@pytest.mark.parametrize(
    ("choices", "when", "match"),
    [
        ((ev.Choice("e1", 2_000),), WHEN, "cover the event's sats"),
        ((ev.Choice("e1", 600),), WHEN, "cover the event's sats"),
        ((ev.Choice("e1", 500), ev.Choice("e1", 500)), WHEN, "each lot once"),
        ((ev.Choice("e1", 1_000),), datetime(2026, 10, 15, tzinfo=UTC), "later than the choice is recorded"),
        ((ev.Choice("", 10),), WHEN, "names a lot"),
        ((ev.Choice("e1 ; drop", 1_000),), WHEN, "engine's id"),
        ((ev.Choice("e1", 0),), WHEN, "whole sats"),
        ((ev.Choice("e1", 10),), None, "when it was made"),
        ((ev.Choice("e1", 10),), datetime(2025, 3, 1), "timezone-aware"),
        ([ev.Choice("e1", 10)], WHEN, "tuple of Choices"),
    ],
)
def test_invalid_choices_are_refused(
    conn: sqlite3.Connection, accounts: tuple[int, int], choices: Any, when: Any, match: str
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    with pytest.raises(ev.EventError, match=match):
        ev.identify(conn, sale, choices, when, at=AT)


def test_an_acquisition_chooses_no_lots(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, _ = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    with pytest.raises(ev.EventError, match="only a disposal or a movement"):
        ev.identify(conn, bought, (ev.Choice("e1", 10),), WHEN, at=AT)
    with pytest.raises(sqlite3.IntegrityError, match="only a disposal or a movement"):  # the schema too
        conn.execute(
            "INSERT INTO identification (event_id, position, lot, sats, identified_at)"
            " VALUES (?, 0, 'e1', 5, ?)",
            (bought, AT),
        )


def test_removing_an_event_removes_its_choice_first_both_logged_t408(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    ev.remove_event(conn, sale, at=AT)
    assert ev.events(conn) == []
    assert logged(conn, "identification", str(sale))[-1][1] is None
    before, after, _ = logged(conn, "event", str(sale))[-1]
    assert (before["kind"], after) == ("sell", None)
    with pytest.raises(ev.EventError, match="no such event"):
        ev.remove_event(conn, sale, at=AT)


def test_the_upgrade_keeps_every_change_log_row_and_its_rules_t408(tmp_path: Path) -> None:
    # a schema-10 DB with logged tags upgrades: every row is kept, with its id; the log stays
    # append-only and still names its origins; the new kinds are accepted
    d = tmp_path / "old"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:10]))
    try:
        record_chain(old, "regtest")
        tags.set_mixing(old, TXID, True, at=AT)
        tags.set_mixing(old, TXID, False, at=AT)
        rows = old.execute(
            "SELECT id, at, kind, subject, before, after, origin FROM change_log ORDER BY id"
        ).fetchall()
    finally:
        old.close()
    conn = open_db(data)
    try:
        assert (
            conn.execute(
                "SELECT id, at, kind, subject, before, after, origin FROM change_log ORDER BY id"
            ).fetchall()
            == rows
        )
        for sql in ("UPDATE change_log SET subject = 'x'", "DELETE FROM change_log"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO change_log (id, at, kind, subject, after, origin)"
                " VALUES (?, ?, 'tx_flag', ?, '{}', 'user')",
                (rows[0][0], AT, TXID),
            )
        with pytest.raises(sqlite3.IntegrityError, match="names its origin"):
            conn.execute(
                "INSERT INTO change_log (at, kind, subject, after) VALUES (?, 'event', '1', '{}')", (AT,)
            )
        with pytest.raises(sqlite3.IntegrityError):  # only a mixing flag may be the app's heuristic
            conn.execute(
                "INSERT INTO change_log (at, kind, subject, after, origin)"
                " VALUES (?, 'event', '1', '{}', 'heuristic')",
                (AT,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # the kinds are still a closed list
            conn.execute(
                "INSERT INTO change_log (at, kind, subject, after, origin)"
                " VALUES (?, 'lot', '1', '{}', 'user')",
                (AT,),
            )
        wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
        ev.add_event(conn, buy(wallet), at=AT)
        assert [c.kind for c in tags.changes(conn)][-1] == "event"
    finally:
        conn.close()


def test_a_private_key_in_free_text_is_refused_t703(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    wif = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"  # the well-known example key, not anyone's
    with pytest.raises(PrivateKeyError):
        ev.add_event(conn, buy(wallet, note=f"backup {wif}"), at=AT)
    assert ev.events(conn) == []


def test_an_unknown_account_is_refused_with_a_fixed_message(conn: sqlite3.Connection) -> None:
    with pytest.raises(ev.EventError, match="an account that doesn't exist"):
        ev.add_event(conn, buy(999), at=AT)
    assert ev.events(conn) == []


def test_a_movements_choice_may_name_the_arriving_sats_only_adr0043(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange, fee_sats=100), at=AT)
    ev.identify(conn, deposit, (ev.Choice("e1", 900),), WHEN, at=AT)  # the fee takes the standing method's
    ev.identify(conn, deposit, (ev.Choice("e1", 1_000),), WHEN, at=AT)  # or the whole
    with pytest.raises(ev.EventError, match="cover the event's sats"):
        ev.identify(conn, deposit, (ev.Choice("e1", 950),), WHEN, at=AT)


def test_a_withdrawal_with_a_missing_basis_chooses_no_lots(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    w = ev.add_event(
        conn,
        ev.Event(
            "withdrawal",
            exchange,
            WHEN,
            500,
            to=wallet,
            missing_basis=D("1.00"),
            missing_acquired=date(2024, 1, 1),
        ),
        at=AT,
    )
    with pytest.raises(ev.EventError, match="has no lots for"):
        ev.identify(conn, w, (ev.Choice("e1", 500),), WHEN, at=AT)


def test_an_edit_its_choice_no_longer_fits_is_refused(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    with pytest.raises(ev.EventError, match="only a disposal or a movement"):
        ev.update_event(conn, sale, buy(wallet), at=AT)
    with pytest.raises(ev.EventError, match="cover the event's sats"):
        ev.update_event(conn, sale, ev.Event("sell", wallet, WHEN, 900, D("50.00")), at=AT)
    ev.update_event(conn, sale, ev.Event("sell", wallet, WHEN, 1_000, D("55.00")), at=AT)  # a new price fits
    with pytest.raises(sqlite3.IntegrityError, match="only a disposal or a movement"):  # the schema too
        conn.execute("UPDATE event SET kind = 'buy', usd = '1.00' WHERE id = ?", (sale,))
    with pytest.raises(sqlite3.IntegrityError, match="no longer cover"):
        conn.execute("UPDATE event SET sats = 10 WHERE id = ?", (sale,))


def test_a_purchase_other_choices_name_keeps_its_lots(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{bought}@{sale}", 1_000),), WHEN, at=AT)  # a part moved on
    with pytest.raises(ev.EventError, match="other events' choices name"):
        ev.remove_event(conn, bought, at=AT)
    with pytest.raises(ev.EventError, match="other events' choices name"):
        ev.update_event(conn, bought, buy(exchange), at=AT)
    ev.update_event(conn, bought, buy(wallet, note="a note"), at=AT)  # the same lots: fine
    ev.identify(conn, sale, (), None, at=AT)
    ev.remove_event(conn, bought, at=AT)


def test_a_removed_events_id_is_never_given_again(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    first = ev.add_event(conn, buy(wallet), at=AT)
    ev.remove_event(conn, first, at=AT)
    assert ev.add_event(conn, buy(wallet), at=AT) != first


def test_the_upgrade_keeps_rows_without_an_origin_and_every_trigger_t408(tmp_path: Path) -> None:
    # a schema-9 DB (no origin column yet) upgrades through m0010 and m0011: its rows keep their NULL
    # origin; ids below 1 are still refused
    d = tmp_path / "older"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    old = open_db(data, steps=list(STEPS[:9]))
    try:
        record_chain(old, "regtest")
        old.execute(
            "INSERT INTO change_log (at, kind, subject, after) VALUES (?, 'tx_flag', ?, ?)",
            (AT, TXID, '{"mixing": true, "source": "user"}'),
        )
    finally:
        old.close()
    conn = open_db(data)
    try:
        assert conn.execute("SELECT origin FROM change_log").fetchall() == [(None,)]
        for bad in (0, -1):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(
                    "INSERT INTO change_log (id, at, kind, subject, after, origin)"
                    " VALUES (?, ?, 'event', '1', '{}', 'user')",
                    (bad, AT),
                )
    finally:
        conn.close()


def test_every_choice_reads_back_in_one_query(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, exchange = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 500, to=exchange), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 600), ev.Choice("e2", 400)), WHEN, at=AT)
    ev.identify(conn, deposit, (ev.Choice("e2", 500),), WHEN, at=AT)
    assert ev.identifications(conn) == {
        sale: ((ev.Choice("e1", 600), ev.Choice("e2", 400)), WHEN),
        deposit: ((ev.Choice("e2", 500),), WHEN),
    }


def test_an_unknown_event_is_not_found(conn: sqlite3.Connection) -> None:
    with pytest.raises(ev.EventNotFound):
        ev.remove_event(conn, 12345, at=AT)


def test_the_schema_holds_a_gift_given_to_no_proceeds(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event (kind, tax_account_id, at, sats, usd)"
            " VALUES ('gift_out', ?, '2025-03-01T12:00:00+00:00', 5, '1.00')",
            (wallet,),
        )


def test_a_stored_choice_is_the_users_specific_one(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    assert conn.execute("SELECT method, late FROM identification").fetchall() == [("specific", 0)]


@pytest.mark.parametrize(
    "lot", ["{m}", "{b}@{m}", "{b}@{m}@{w}", "{b}@{m}#1", "{b}@{w}@{m}", "{m}@@unrecorded"]
)
def test_a_movement_whose_moved_lots_are_named_keeps_them(
    conn: sqlite3.Connection, accounts: tuple[int, int], lot: str
) -> None:
    wallet, exchange = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange), at=AT)
    withdrawal = ev.add_event(conn, ev.Event("withdrawal", exchange, WHEN, 1_000, to=wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", exchange, WHEN, 1_000, D("50.00")), at=AT)
    chosen = lot.format(b=bought, m=deposit, w=withdrawal)
    ev.identify(conn, sale, (ev.Choice(chosen, 1_000),), WHEN, at=AT)
    with pytest.raises(ev.EventError, match="other events' choices name"):
        ev.remove_event(conn, deposit, at=AT)


def test_a_named_purchase_keeps_its_moment_fee_and_destination(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(str(bought), 1_000),), WHEN, at=AT)
    for edited in (buy(wallet, at=WHEN + timedelta(days=1)), buy(wallet, fee_sats=10)):
        with pytest.raises(ev.EventError, match="other events' choices name"):
            ev.update_event(conn, bought, edited, at=AT)


def test_an_id_that_only_contains_the_events_digits_doesnt_name_it(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    ids = [ev.add_event(conn, buy(wallet), at=AT) for _ in range(11)]  # ids 1 to 11
    first, ten, eleven = ids[0], ids[9], ids[10]
    assert (first, ten, eleven) == (1, 10, 11)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{ten}@{eleven}", 1_000),), WHEN, at=AT)  # other ids
    ev.remove_event(conn, first, at=AT)


@pytest.mark.parametrize("at", ["not a time", "2026-10-14T12:00:00"])
def test_the_logs_time_is_checked(conn: sqlite3.Connection, accounts: tuple[int, int], at: str) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    with pytest.raises(ev.EventError, match="the log's time"):
        ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=at)


def test_an_event_with_a_choice_keeps_its_account_and_moment(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    for edited in (
        ev.Event("sell", exchange, WHEN, 1_000, D("50.00")),
        ev.Event("sell", wallet, WHEN + timedelta(days=1), 1_000, D("50.00")),
    ):
        with pytest.raises(ev.EventError, match="keeps its account and moment"):
            ev.update_event(conn, sale, edited, at=AT)


def test_a_fee_change_its_arriving_choice_no_longer_fits_is_refused(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange, fee_sats=100), at=AT)
    ev.identify(conn, deposit, (ev.Choice("e1", 900),), WHEN, at=AT)
    with pytest.raises(ev.EventError, match="cover the event's sats"):
        ev.update_event(
            conn, deposit, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange, fee_sats=50), at=AT
        )


def test_an_unknown_event_is_not_found_for_every_write(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    with pytest.raises(ev.EventNotFound):
        ev.update_event(conn, 12345, buy(wallet), at=AT)
    with pytest.raises(ev.EventNotFound):
        ev.identify(conn, 12345, (), None, at=AT)


def test_the_schema_refuses_choices_over_the_events_sats(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    insert = (
        "INSERT INTO identification (event_id, position, lot, sats, identified_at) VALUES (?, ?, 'e1', ?, ?)"
    )
    conn.execute(insert, (sale, 0, 600, AT))
    with pytest.raises(sqlite3.IntegrityError, match="more sats than the event"):
        conn.execute(insert, (sale, 1, 500, AT))


def test_an_event_before_the_genesis_block_is_refused(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    with pytest.raises(ev.EventError, match="genesis"):
        ev.add_event(conn, buy(wallet, at=datetime(2008, 12, 31, tzinfo=UTC)), at=AT)


@pytest.mark.parametrize("kind", ["override", "lot_link", "tracking_start", "wallet_fifo"])
def test_the_log_accepts_the_kinds_later_steps_record(conn: sqlite3.Connection, kind: str) -> None:
    change_log.append(conn, AT, (kind, "s", None, "x"), "user")
    assert conn.execute("SELECT kind FROM change_log").fetchall() == [(kind,)]


def test_the_log_still_refuses_an_unknown_kind(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        change_log.append(conn, AT, ("setting", "s", None, "x"), "user")  # not in PLAN §2's list


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("sats = 1100", True),
        ("fee_sats = 50", True),
        ("sats = 1050, fee_sats = 150", False),
        ("note = 'x'", False),
    ],
)
def test_the_schema_keeps_an_edited_events_choice_exact(
    conn: sqlite3.Connection, accounts: tuple[int, int], change: str, refused: bool
) -> None:
    wallet, exchange = accounts
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange, fee_sats=100), at=AT)
    ev.identify(conn, deposit, (ev.Choice("e1", 900),), WHEN, at=AT)
    update = f"UPDATE event SET {change} WHERE id = ?"  # noqa: S608 - the test's own constant changes
    if refused:
        with pytest.raises(sqlite3.IntegrityError, match="no longer cover"):
            conn.execute(update, (deposit,))
    else:
        conn.execute(update, (deposit,))


def test_a_named_movement_keeps_its_choice(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, exchange = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    other = ev.add_event(conn, buy(wallet), at=AT)
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange), at=AT)
    ev.identify(conn, deposit, (ev.Choice(str(bought), 1_000),), WHEN, at=AT)
    sale = ev.add_event(conn, ev.Event("sell", exchange, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{bought}@{deposit}", 1_000),), WHEN, at=AT)
    for choice in ((ev.Choice(str(other), 1_000),), ()):
        with pytest.raises(ev.EventError, match="other events' choices name"):
            ev.identify(conn, deposit, choice, WHEN, at=AT)
    ev.identify(conn, deposit, (ev.Choice(str(bought), 1_000),), WHEN, at=AT)  # the same again


def test_a_moment_on_the_calendars_last_day_is_handled(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    last = datetime(9999, 12, 31, 12, tzinfo=UTC)
    gift = ev.Event("gift_in", wallet, last, 10, fmv=D("1.00"), donor_acquired=date(2020, 1, 1))
    assert ev.add_event(conn, gift, at=AT) >= 1


@pytest.mark.parametrize("lot", ["57", "57@1", "57@@unrecorded", "57#0"])
def test_a_choice_cant_name_an_event_not_yet_stored(
    conn: sqlite3.Connection, accounts: tuple[int, int], lot: str
) -> None:
    wallet, _ = accounts
    ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    with pytest.raises(ev.EventError, match="isn't stored"):
        ev.identify(conn, sale, (ev.Choice(lot, 1_000),), WHEN, at=AT)


def test_an_output_index_isnt_read_as_an_event(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, _ = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{bought}#57", 1_000),), WHEN, at=AT)


def test_a_choice_made_within_the_second_it_is_recorded_in_is_accepted(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    made = datetime.fromisoformat(AT) + timedelta(microseconds=500_000)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), made, at=AT)
    with pytest.raises(ev.EventError, match="later than the choice is recorded"):
        ev.identify(conn, sale, (ev.Choice("e1", 1_000),), made + timedelta(seconds=1), at=AT)
    with pytest.raises(ev.EventError, match="genesis"):
        ev.identify(conn, sale, (ev.Choice("e1", 1_000),), datetime(2008, 1, 1, tzinfo=UTC), at=AT)


@pytest.mark.parametrize("at", ["short", "2026-10-14T12:00:00", "not a time at all, but long"])
def test_every_writer_checks_the_logs_time(
    conn: sqlite3.Connection, accounts: tuple[int, int], at: str
) -> None:
    wallet, _ = accounts
    with pytest.raises(ev.EventError, match="the log's time"):
        ev.add_event(conn, buy(wallet), at=at)
    bought = ev.add_event(conn, buy(wallet), at=AT)
    with pytest.raises(ev.EventError, match="the log's time"):
        ev.update_event(conn, bought, buy(wallet, sats=2_000), at=at)
    with pytest.raises(ev.EventError, match="the log's time"):
        ev.remove_event(conn, bought, at=at)


def test_a_damaged_event_row_is_refused_when_read_t408(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    gift = ev.add_event(
        conn, ev.Event("gift_in", wallet, WHEN, 10, fmv=D("1.00"), donor_acquired=date(2020, 1, 1)), at=AT
    )
    conn.execute("UPDATE event SET fmv = '1.2.3' WHERE id = ?", (gift,))
    with pytest.raises(ev.EventError, match="damaged"):
        ev.events(conn)


@pytest.mark.parametrize("change", ["sats = 1", "event_id = event_id"])
def test_the_schema_refuses_editing_a_choices_rows(
    conn: sqlite3.Connection, accounts: tuple[int, int], change: str
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    with pytest.raises(sqlite3.IntegrityError, match="replaced, never edited"):
        conn.execute(f"UPDATE identification SET {change}")  # noqa: S608 - the test's own constant changes


@pytest.mark.parametrize(
    "change",
    [
        "missing_basis = '1.00', missing_acquired = '2024-01-01'",
        "at = '2025-06-01T00:00:00+00:00'",
        "tax_account_id = ?",
    ],
)
def test_the_schema_keeps_a_chosen_events_basis_account_and_moment(
    conn: sqlite3.Connection, accounts: tuple[int, int], change: str
) -> None:
    wallet, exchange = accounts
    withdrawal = ev.add_event(conn, ev.Event("withdrawal", exchange, WHEN, 1_000, to=wallet), at=AT)
    ev.identify(conn, withdrawal, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    third = ac.add_tax_account(
        conn, "Another exchange account", "custodial", entity_id=ac.entities(conn)[-1].id
    )
    values = (third, withdrawal) if "?" in change else (withdrawal,)
    with pytest.raises(sqlite3.IntegrityError, match="keeps its account, moment and basis"):
        conn.execute(f"UPDATE event SET {change} WHERE id = ?", values)  # noqa: S608 - constants


@pytest.mark.parametrize(
    "edit",
    [
        {"kind": "self_transfer"},
        {"sats": 2_000},
        {"fee_sats": 10},
        {"at": WHEN + timedelta(days=1)},
        {"to": "third"},
        {"missing_basis": D("1.00"), "missing_acquired": date(2024, 1, 1)},
    ],
)
def test_a_named_movement_keeps_everything_its_lots_depend_on(
    conn: sqlite3.Connection, accounts: tuple[int, int], edit: dict[str, Any]
) -> None:
    wallet, exchange = accounts
    third = ac.add_tax_account(conn, "Cold wallet", "self_custody")
    withdrawal = ev.add_event(conn, ev.Event("withdrawal", exchange, WHEN, 1_000, to=wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{withdrawal}@@unrecorded", 1_000),), WHEN, at=AT)
    fields = {"to": third} if edit.get("to") == "third" else edit
    if fields.get("kind") == "self_transfer":
        fields = {"kind": "self_transfer", "account": third}
    edited = dataclasses.replace(ev.Event("withdrawal", exchange, WHEN, 1_000, to=wallet), **fields)
    with pytest.raises(ev.EventError, match="other events' choices name"):
        ev.update_event(conn, withdrawal, edited, at=AT)
    ev.update_event(
        conn,
        withdrawal,
        dataclasses.replace(
            edited,
            **{k: getattr(ev.Event("withdrawal", exchange, WHEN, 1_000, to=wallet), k) for k in fields},
            note="only a note",
        ),
        at=AT,
    )  # what its lots don't depend on can change


def test_a_withdrawal_with_a_choice_cant_take_a_missing_basis(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    withdrawal = ev.add_event(conn, ev.Event("withdrawal", exchange, WHEN, 1_000, to=wallet), at=AT)
    ev.identify(conn, withdrawal, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    missing = ev.Event(
        "withdrawal",
        exchange,
        WHEN,
        1_000,
        to=wallet,
        missing_basis=D("1.00"),
        missing_acquired=date(2024, 1, 1),
    )
    with pytest.raises(ev.EventError, match=r"missing basis|chooses no lots"):
        ev.update_event(conn, withdrawal, missing, at=AT)


def test_a_named_movement_may_choose_the_same_lots_again(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, exchange = accounts
    a = ev.add_event(conn, buy(wallet, sats=600), at=AT)
    b = ev.add_event(conn, buy(wallet, sats=400), at=AT)
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange), at=AT)
    ev.identify(conn, deposit, (ev.Choice(str(a), 600), ev.Choice(str(b), 400)), WHEN, at=AT)
    sale = ev.add_event(conn, ev.Event("sell", exchange, WHEN, 600, D("30.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{a}@{deposit}", 600),), WHEN, at=AT)
    later = WHEN + timedelta(hours=1)
    ev.identify(conn, deposit, (ev.Choice(str(b), 400), ev.Choice(str(a), 600)), later, at=AT)  # same lots
    assert ev.identification(conn, deposit)[1] == later


def test_the_schema_refuses_a_btc_fee_on_a_disposal(
    conn: sqlite3.Connection, accounts: tuple[int, int]
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE event SET fee_sats = 5 WHERE id = ?", (sale,))


def test_a_choice_is_never_redated_earlier_t508(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    later = WHEN + timedelta(days=30)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), later, at=AT)
    with pytest.raises(ev.EventError, match="before the one it replaces"):
        ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    ev.identify(conn, sale, (), None, at=AT)  # cleared: a new choice starts afresh
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)


@pytest.mark.parametrize("lot", ["05", "05@1", "9" * 25, "9" * 25 + "@1"])
def test_an_event_id_in_a_lot_has_one_spelling(
    conn: sqlite3.Connection, accounts: tuple[int, int], lot: str
) -> None:
    wallet, _ = accounts
    ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    with pytest.raises(ev.EventError, match="no leading zero"):
        ev.identify(conn, sale, (ev.Choice(lot, 1_000),), WHEN, at=AT)


def test_a_choice_names_a_bounded_number_of_lots(conn: sqlite3.Connection, accounts: tuple[int, int]) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_001, D("50.00")), at=AT)
    many = tuple(ev.Choice(f"e{i}", 1) for i in range(ev.MAX_CHOICES + 1))
    with pytest.raises(ev.EventError, match="at most"):
        ev.identify(conn, sale, many, WHEN, at=AT)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE identification SET identified_at = '2025-13-45T00:00:00+00:00'",
        "UPDATE event SET usd = '1.001' WHERE kind = 'sell'",
        "UPDATE event SET at = '2025-03-01T17:00:00+05:00' WHERE kind = 'sell'",
    ],
)
def test_damaged_rows_are_refused_when_read_t408(
    conn: sqlite3.Connection, accounts: tuple[int, int], damage: str
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 1_000),), WHEN, at=AT)
    for trigger in (
        "identification_is_replaced_never_edited",
        "event_with_chosen_lots_keeps_its_account_moment_and_basis",
    ):
        conn.execute(f"DROP TRIGGER {trigger}")  # damage that bypasses the module and the schema's rules
    conn.execute(damage)
    with pytest.raises(ev.EventError, match="damaged"):
        ev.events(conn)
        ev.identifications(conn)
    with pytest.raises(ev.EventError, match="damaged"):
        ev.identifications(conn)
        ev.events(conn)


@pytest.mark.parametrize("kind", ["opening_allocation_2025", "fee"])
def test_plan_kinds_for_later_steps_are_listed_but_refused(
    conn: sqlite3.Connection, accounts: tuple[int, int], kind: str
) -> None:
    wallet, _ = accounts
    with pytest.raises(ev.EventError, match="isn't supported yet"):
        ev.add_event(conn, ev.Event(kind, wallet, WHEN, 10, D("1.00")), at=AT)  # type: ignore[arg-type]
    conn.execute(  # the schema lists them, so their steps needn't rebuild `event`
        "INSERT INTO event (kind, tax_account_id, at, sats, usd) VALUES (?, ?, ?, 10, '1.00')",
        (kind, wallet, AT),
    )


@pytest.mark.parametrize(
    ("column", "value"), [("fmv", "."), ("fmv", "1-2"), ("missing_basis", "-"), ("usd", "1-2")]
)
def test_the_schema_refuses_malformed_money(
    conn: sqlite3.Connection, accounts: tuple[int, int], column: str, value: str
) -> None:
    wallet, exchange = accounts
    gift = ev.add_event(
        conn, ev.Event("gift_in", wallet, WHEN, 10, fmv=D("1.00"), donor_acquired=date(2020, 1, 1)), at=AT
    )
    withdrawal = ev.add_event(
        conn,
        ev.Event(
            "withdrawal",
            exchange,
            WHEN,
            10,
            to=wallet,
            missing_basis=D("1.00"),
            missing_acquired=date(2024, 1, 1),
        ),
        at=AT,
    )
    target = {"fmv": gift, "usd": gift, "missing_basis": withdrawal}[column]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE event SET {column} = ? WHERE id = ?", (value, target))  # noqa: S608 - constants


@pytest.mark.parametrize("lot", ["{b}@0", "{b}@3", "ab12:0@@placeholder", "{b}@{b}#0"])
def test_lot_ids_the_engine_builds_are_accepted(
    conn: sqlite3.Connection, accounts: tuple[int, int], lot: str
) -> None:
    """An opening allocation's parts (`<id>@<n>`, PLAN §7) and a coin-traced wallet's lots, which start
    from a coin (ADR 0041), are lots a choice can name."""
    wallet, _ = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(lot.format(b=bought), 1_000),), WHEN, at=AT)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE identification SET lot = 'e 1' WHERE position = 0",  # outside the engine's id alphabet
        "UPDATE identification SET lot = 'e2' WHERE position = 0",  # the same lot twice
        "UPDATE identification SET sats = 300 WHERE position = 0",  # no longer covers the sale
        # two times for one choice
        "UPDATE identification SET identified_at = '2025-03-01T12:00:01+00:00' WHERE position = 1",
        "UPDATE identification SET identified_at = '2025-03-01T17:00:00+05:00'",  # not UTC
        "UPDATE identification SET identified_at = '2025-03-01T12:00:00Z'",  # not as the module writes it
        "UPDATE identification SET identified_at = '2008-01-01T00:00:00+00:00'",  # before genesis
    ],
)
def test_a_stored_choice_is_checked_as_identify_would_write_it_t408(
    conn: sqlite3.Connection, accounts: tuple[int, int], damage: str
) -> None:
    wallet, _ = accounts
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice("e1", 600), ev.Choice("e2", 400)), WHEN, at=AT)
    assert ev.identifications(conn) == {sale: ((ev.Choice("e1", 600), ev.Choice("e2", 400)), WHEN)}
    for trigger in (
        "identification_is_replaced_never_edited",
        "event_with_chosen_lots_keeps_its_account_moment_and_basis",
    ):
        conn.execute(f"DROP TRIGGER {trigger}")  # damage that bypasses the module and the schema's rules
    conn.execute(damage)
    with pytest.raises(ev.EventError, match="damaged"):
        ev.identifications(conn)

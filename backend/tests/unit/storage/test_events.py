"""The user's events and lot identifications in the user DB (migration m0011; PLAN §2; THREAT_MODEL T-408,
T-501, T-508). Synthetic data only."""

from __future__ import annotations

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
        ev.Event("sell", exchange, WHEN, 500, D("-0.50"), fee_sats=5),  # costs above proceeds: negative
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
        (ev.Event("income", 1, WHEN, 10, D("1.00"), fee_sats=1), "bears a fee"),
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
    with pytest.raises(sqlite3.IntegrityError, match="more sats than the event"):
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
    "lot", ["{m}", "{b}@{m}", "{b}@{m}@99", "{b}@{m}#1", "{b}@99@{m}", "{m}@@unrecorded"]
)
def test_a_movement_whose_moved_lots_are_named_keeps_them(
    conn: sqlite3.Connection, accounts: tuple[int, int], lot: str
) -> None:
    wallet, exchange = accounts
    bought = ev.add_event(conn, buy(wallet), at=AT)
    deposit = ev.add_event(conn, ev.Event("deposit", wallet, WHEN, 1_000, to=exchange), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", exchange, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(lot.format(b=bought, m=deposit), 1_000),), WHEN, at=AT)
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
    bought = ev.add_event(conn, buy(wallet), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", wallet, WHEN, 1_000, D("50.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{bought}0@{bought}1", 1_000),), WHEN, at=AT)  # other ids
    ev.remove_event(conn, bought, at=AT)


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


@pytest.mark.parametrize("kind", ["override", "lot_link", "tracking_start", "wallet_fifo", "setting"])
def test_the_log_accepts_the_kinds_later_steps_record(conn: sqlite3.Connection, kind: str) -> None:
    change_log.append(conn, AT, (kind, "s", None, "x"), "user")
    assert conn.execute("SELECT kind FROM change_log").fetchall() == [(kind,)]


def test_the_log_still_refuses_an_unknown_kind(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        change_log.append(conn, AT, ("price", "s", None, "x"), "user")

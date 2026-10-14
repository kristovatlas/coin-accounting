"""The user's stored events through the lot engine (PLAN §7, M6): the mapping to the engine's input and the
run. Synthetic data only."""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coinacct.services.imports import ImportRefused
from coinacct.services.ledger import (
    Ledger,
    automatic_accounts,
    engine_events,
    refused,
    standing_methods,
)
from coinacct.storage import accounts as ac
from coinacct.storage import events as ev
from coinacct.storage import settings
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db, open_reader
from coinacct.tax import engine
from coinacct.tax.engine import Acquisition, Disposal, Pick, Transfer

AT = "2026-10-14T12:00:00+00:00"
D = Decimal
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Ledger]]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    data = open_data_dir(str(d))
    conn = open_db(data)
    record_chain(conn, "regtest")
    yield conn, Ledger(functools.partial(open_reader, data))
    conn.close()


def test_a_moment_late_in_the_day_utc_is_the_previous_day_in_new_york() -> None:
    late = ev.Stored(1, ev.Event("buy", 7, datetime(2025, 1, 1, 3, tzinfo=UTC), 1_000, D("10.00")))
    (acq,) = engine_events([late], {}, NEW_YORK)
    assert isinstance(acq, Acquisition)
    assert (acq.id, acq.account, acq.on, acq.sats, acq.basis) == (
        "1",
        "7",
        date(2024, 12, 31),
        1_000,
        D("10.00"),
    )


def test_a_buys_withheld_fee_comes_off_its_lot_adr0043() -> None:
    bought = ev.Stored(
        1, ev.Event("buy", 7, datetime(2025, 1, 1, 12, tzinfo=UTC), 1_000, D("10.00"), fee_sats=1)
    )
    (acq,) = engine_events([bought], {}, NEW_YORK)
    assert isinstance(acq, Acquisition)
    assert (acq.sats, acq.basis) == (999, D("10.00"))  # the sats credited, the full cash paid


def test_a_choice_becomes_the_events_picks() -> None:
    when = datetime(2025, 3, 1, 12, tzinfo=UTC)
    sale = ev.Stored(2, ev.Event("sell", 7, when, 1_000, D("50.00")))
    move = ev.Stored(3, ev.Event("deposit", 7, when, 500, to=8, fee_sats=10))
    chosen = {2: ((ev.Choice("1", 1_000),), when)}
    out = engine_events([move, sale], chosen, NEW_YORK)
    assert [e.id for e in out] == ["2", "3"]  # by moment, then id
    sold, moved = out
    assert isinstance(sold, Disposal) and isinstance(moved, Transfer)
    assert (sold.picks, sold.identified_at, sold.proceeds) == ((Pick("1", 1_000),), when, D("50.00"))
    assert (moved.to, moved.fee_sats, moved.picks) == ("8", 10, None)


def test_only_a_fifo_standing_order_is_passed_on() -> None:
    accounts = [
        ac.TaxAccount(1, "a", "custodial", 2, "fifo", True, True),
        ac.TaxAccount(2, "b", "custodial", 2, "specific", True, True),
        ac.TaxAccount(3, "c", "self_custody", None, None, True, False),
    ]
    assert standing_methods(accounts) == {"1": "fifo"}


def test_the_ledger_runs_the_stored_events(db: tuple[sqlite3.Connection, Ledger]) -> None:
    conn, ledger = db
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange, automatic=False)  # lots chosen
    bought = ev.add_event(
        conn, ev.Event("buy", x, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    sale = ev.add_event(
        conn, ev.Event("sell", x, datetime(2025, 3, 1, 12, tzinfo=UTC), 400, D("30.00")), at=AT
    )
    ev.identify(conn, sale, (ev.Choice(str(bought), 400),), datetime(2025, 3, 1, 11, tzinfo=UTC), at=AT)
    settings.set_time_zone(conn, "America/New_York")
    run = ledger.run()
    inputs, result = run.inputs, run.result
    assert [e.id for e in inputs] == [str(bought), str(sale)]
    (row,) = result.allocations
    assert (row.lot, row.sats, row.basis, row.proceeds, row.long_term) == (
        str(bought),
        400,
        D("4.00"),
        D("30.00"),
        True,
    )
    assert result.late == ()
    assert run.ignored == ()


def test_the_ledger_takes_tax_dates_in_the_stored_time_zone(db: tuple[sqlite3.Connection, Ledger]) -> None:
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
    ev.add_event(conn, ev.Event("buy", wallet, datetime(2025, 1, 1, 3, tzinfo=UTC), 1_000, D("10.00")), at=AT)
    (utc,) = ledger.run().inputs
    assert utc.on == date(2025, 1, 1)  # UTC until the user sets a zone
    settings.set_time_zone(conn, "America/New_York")
    (local,) = ledger.run().inputs
    assert local.on == date(2024, 12, 31)


@pytest.mark.parametrize(("automatic", "lot"), [(True, "first"), (False, "second")])
def test_an_automatic_account_takes_the_standing_method_adr0021(
    db: tuple[sqlite3.Connection, Ledger], automatic: bool, lot: str
) -> None:
    conn, ledger = db
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange, automatic=automatic)
    first = ev.add_event(conn, ev.Event("buy", x, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT)
    second = ev.add_event(
        conn, ev.Event("buy", x, datetime(2024, 6, 2, tzinfo=UTC), 1_000, D("50.00")), at=AT
    )
    sale = ev.add_event(
        conn, ev.Event("sell", x, datetime(2025, 3, 1, 12, tzinfo=UTC), 1_000, D("60.00")), at=AT
    )
    ev.identify(conn, sale, (ev.Choice(str(second), 1_000),), datetime(2025, 3, 1, 11, tzinfo=UTC), at=AT)
    run = ledger.run()
    inputs, result = run.inputs, run.result
    (sold,) = [e for e in inputs if e.id == str(sale)]
    assert isinstance(sold, Disposal)
    assert sold.picks == (None if automatic else (Pick(str(second), 1_000),))
    (row,) = result.allocations
    assert row.lot == {"first": str(first), "second": str(second)}[lot]  # FIFO's oldest, or the choice
    assert result.late == ()
    assert (run.automatic, run.stale) == (((sale,) if automatic else ()), ())


def test_engine_events_drops_a_choice_on_an_automatic_account() -> None:
    when = datetime(2025, 3, 1, 12, tzinfo=UTC)
    sale = ev.Stored(2, ev.Event("sell", 7, when, 1_000, D("50.00")))
    chosen = {2: ((ev.Choice("1", 1_000),), when)}
    (manual,) = engine_events([sale], chosen, NEW_YORK)
    (auto,) = engine_events([sale], chosen, NEW_YORK, frozenset({2}))
    assert isinstance(manual, Disposal) and isinstance(auto, Disposal)
    assert (manual.picks, auto.picks, auto.identified_at) == ((Pick("1", 1_000),), None, None)


def test_automatic_mode_over_specific_identification_keeps_the_choices() -> None:
    accounts = [
        ac.TaxAccount(1, "a", "custodial", 2, "fifo", True, True),
        ac.TaxAccount(2, "b", "custodial", 2, "specific", True, True),  # nothing to select automatically
        ac.TaxAccount(3, "c", "self_custody", None, None, True, False),
        ac.TaxAccount(4, "d", "custodial", 2, "fifo", False, True),
    ]
    assert automatic_accounts(accounts) == {1, 3}


def test_a_damaged_time_zone_keeps_its_remedy(db: tuple[sqlite3.Connection, Ledger]) -> None:
    conn, ledger = db
    conn.execute("INSERT INTO setting (key, value) VALUES ('time_zone', 'Not/A_Zone')")
    with pytest.raises(ImportRefused, match="set the time zone again"):
        ledger.run()


def test_a_choice_naming_a_lot_an_automatic_movement_moved_is_ignored_too(
    db: tuple[sqlite3.Connection, Ledger],
) -> None:
    """An automatic wallet's deposit takes FIFO whatever its stored choice, so a manual exchange sale's
    choice of the lot it would have moved could name a lot the run never makes: it is ignored too."""
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")  # automatic, the default
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange, automatic=False)
    a = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    b = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 2, 2, tzinfo=UTC), 1_000, D("20.00")), at=AT
    )
    deposit = ev.add_event(
        conn, ev.Event("deposit", wallet, datetime(2024, 3, 2, tzinfo=UTC), 1_000, to=x), at=AT
    )
    ev.identify(conn, deposit, (ev.Choice(str(b), 1_000),), datetime(2024, 3, 2, tzinfo=UTC), at=AT)
    sale = ev.add_event(conn, ev.Event("sell", x, datetime(2025, 3, 1, tzinfo=UTC), 1_000, D("60.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{b}@{deposit}", 1_000),), datetime(2025, 3, 1, tzinfo=UTC), at=AT)
    run = ledger.run()  # doesn't stop on a lot the run never makes
    assert (run.automatic, run.stale) == (
        (deposit,),
        (sale,),
    )  # the deposit's (automatic), the now stale sale's
    (row,) = run.result.allocations
    assert row.lot == f"{a}@{deposit}"  # FIFO moved the oldest lot, and the sale took it


def test_a_downstream_choice_of_a_lot_the_run_makes_stands(db: tuple[sqlite3.Connection, Ledger]) -> None:
    """An automatic wallet's deposit takes FIFO and moves its oldest lot; a manual exchange sale that
    chose exactly that moved lot keeps its choice, over the exchange's own older lot."""
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")  # automatic, the default
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange, automatic=False)
    own = ev.add_event(conn, ev.Event("buy", x, datetime(2023, 1, 2, tzinfo=UTC), 1_000, D("5.00")), at=AT)
    a = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    deposit = ev.add_event(
        conn, ev.Event("deposit", wallet, datetime(2024, 3, 2, tzinfo=UTC), 1_000, to=x), at=AT
    )
    sale = ev.add_event(conn, ev.Event("sell", x, datetime(2025, 3, 1, tzinfo=UTC), 1_000, D("60.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{a}@{deposit}", 1_000),), datetime(2025, 3, 1, tzinfo=UTC), at=AT)
    run = ledger.run()
    assert run.ignored == ()
    (row,) = run.result.allocations
    assert row.lot == f"{a}@{deposit}" != str(own)


def test_a_choice_stale_after_a_movement_without_one_falls_back(
    db: tuple[sqlite3.Connection, Ledger],
) -> None:
    """A deposit with no choice of its own moves FIFO's oldest lot; a sale's choice of another lot it
    would have moved names a lot the run never makes, so it takes FIFO and is listed."""
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange, automatic=False)
    a = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    b = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 2, 2, tzinfo=UTC), 1_000, D("20.00")), at=AT
    )
    deposit = ev.add_event(
        conn, ev.Event("deposit", wallet, datetime(2024, 3, 2, tzinfo=UTC), 1_000, to=x), at=AT
    )
    sale = ev.add_event(conn, ev.Event("sell", x, datetime(2025, 3, 1, tzinfo=UTC), 1_000, D("60.00")), at=AT)
    ev.identify(conn, sale, (ev.Choice(f"{b}@{deposit}", 1_000),), datetime(2025, 3, 1, tzinfo=UTC), at=AT)
    run = ledger.run()
    assert (run.automatic, run.stale, run.ignored) == ((), (sale,), (sale,))
    (row,) = run.result.allocations
    assert row.lot == f"{a}@{deposit}"


def test_an_engine_refusal_that_isnt_a_stale_choice_still_stops_the_run(
    db: tuple[sqlite3.Connection, Ledger],
) -> None:
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
    cold = ac.add_tax_account(conn, "Cold", "self_custody")
    ev.add_event(conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT)
    ev.add_event(
        conn, ev.Event("self_transfer", wallet, datetime(2024, 3, 2, tzinfo=UTC), 1_000, to=cold), at=AT
    )
    with pytest.raises(ImportRefused) as refused:
        ledger.run()
    assert str(refused.value).startswith("the lot engine can't use event ")


def test_a_damaged_stored_event_keeps_its_remedy(db: tuple[sqlite3.Connection, Ledger]) -> None:
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
    ev.add_event(conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT)
    conn.execute("UPDATE event SET usd = '1.001'")
    with pytest.raises(ImportRefused, match="restore the user DB from a backup"):
        ledger.run()


def test_a_choice_of_more_sats_than_its_lot_still_holds_falls_back(
    db: tuple[sqlite3.Connection, Ledger],
) -> None:
    """A sale without a choice takes 600 of the only lot; a later sale's choice of 500 of it is stale."""
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody", automatic=False)
    a = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    ev.add_event(conn, ev.Event("sell", wallet, datetime(2024, 6, 1, tzinfo=UTC), 600, D("9.00")), at=AT)
    later = ev.add_event(
        conn, ev.Event("sell", wallet, datetime(2025, 6, 1, tzinfo=UTC), 500, D("9.00")), at=AT
    )
    ev.identify(conn, later, (ev.Choice(str(a), 500),), datetime(2025, 6, 1, tzinfo=UTC), at=AT)
    run = ledger.run()
    assert (run.automatic, run.stale) == ((), (later,))
    assert [(r.disposal, r.lot, r.sats) for r in run.result.allocations][-1] == (str(later), str(a), 400)
    assert [type(b).__name__ for b in run.result.blocking] == ["MissingLots"]  # the 100 sats it lacks


def test_a_transfers_choice_leaving_no_sats_for_its_fee_falls_back(
    db: tuple[sqlite3.Connection, Ledger],
) -> None:
    """A deposit chose the 1,980 sats that arrive; an earlier sale added later took the sats its fee
    needed, so the engine refuses the choice, and the deposit takes the standing method."""
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody", automatic=False)
    exchange = ac.add_entity(conn, "Some exchange", "exchange")
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange, automatic=False)
    a = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    b = ev.add_event(
        conn, ev.Event("buy", wallet, datetime(2024, 1, 3, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    c = ev.add_event(conn, ev.Event("buy", wallet, datetime(2024, 1, 4, tzinfo=UTC), 20, D("1.00")), at=AT)
    deposit = ev.add_event(
        conn, ev.Event("deposit", wallet, datetime(2024, 3, 2, tzinfo=UTC), 1_990, to=x, fee_sats=10), at=AT
    )
    picks = (ev.Choice(str(a), 1_000), ev.Choice(str(b), 980))
    ev.identify(conn, deposit, picks, datetime(2024, 3, 2, tzinfo=UTC), at=AT)
    assert ledger.run().stale == ()  # c's 20 sats pay the fee
    sale = ev.add_event(
        conn, ev.Event("sell", wallet, datetime(2024, 2, 1, tzinfo=UTC), 40, D("1.00")), at=AT
    )
    ev.identify(
        conn, sale, (ev.Choice(str(c), 20), ev.Choice(str(b), 20)), datetime(2024, 2, 1, tzinfo=UTC), at=AT
    )
    run = ledger.run()
    assert (run.automatic, run.stale) == ((), (deposit,))
    assert [type(x).__name__ for x in run.result.blocking] == ["MissingLots"]  # FIFO has 1,980 of 1,990


NAMED = "the lot engine can't use event {} as stored: check it, its accounts and its lots"
UNNAMED = "the lot engine can't use the stored events: check them, or restore from a backup"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("event '12': lot 'x' isn't held in account '3'", NAMED.format(12)),
        ("transfer '7': its fee is a whole number of sats below the sats it moves", NAMED.format(7)),
        ("an event names its id and its account", UNNAMED),
        ("event 'x@1' appears twice", UNNAMED),
        ("event '012' appears twice", UNNAMED),
    ],
)
def test_an_engine_refusal_names_only_the_event_t403(message: str, expected: str) -> None:
    assert str(refused(engine.EngineError(message))) == expected

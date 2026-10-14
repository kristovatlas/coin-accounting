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

from coinacct.services.ledger import Ledger, engine_events, standing_methods
from coinacct.storage import accounts as ac
from coinacct.storage import events as ev
from coinacct.storage import settings
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db, open_reader
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
    x = ac.add_tax_account(conn, "Exchange", "custodial", entity_id=exchange)
    bought = ev.add_event(
        conn, ev.Event("buy", x, datetime(2024, 1, 2, tzinfo=UTC), 1_000, D("10.00")), at=AT
    )
    sale = ev.add_event(
        conn, ev.Event("sell", x, datetime(2025, 3, 1, 12, tzinfo=UTC), 400, D("30.00")), at=AT
    )
    ev.identify(conn, sale, (ev.Choice(str(bought), 400),), datetime(2025, 3, 1, 11, tzinfo=UTC), at=AT)
    settings.set_time_zone(conn, "America/New_York")
    inputs, result = ledger.run()
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


def test_the_ledger_takes_tax_dates_in_the_stored_time_zone(db: tuple[sqlite3.Connection, Ledger]) -> None:
    conn, ledger = db
    wallet = ac.add_tax_account(conn, "Wallet", "self_custody")
    ev.add_event(conn, ev.Event("buy", wallet, datetime(2025, 1, 1, 3, tzinfo=UTC), 1_000, D("10.00")), at=AT)
    (utc,), _ = ledger.run()
    assert utc.on == date(2025, 1, 1)  # UTC until the user sets a zone
    settings.set_time_zone(conn, "America/New_York")
    (local,), _ = ledger.run()
    assert local.on == date(2024, 12, 31)

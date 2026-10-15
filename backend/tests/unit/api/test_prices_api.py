"""The price routes (PLAN §6, ADR 0039; M5): the cached prices and the last refresh, read only, through
`services.price_cache.PriceReader`. Synthetic prices only; nothing is downloaded."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coinacct.api.app import create_app
from coinacct.api.session import Sessions
from coinacct.services.price_cache import PriceReader
from coinacct.services.startup import NodeStatus
from coinacct.storage import prices as store
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import DbBusy, open_db, open_reader

from .asgi import PORT, Reply, call

TOKEN = "launch-token-for-tests"
H = "ab" * 32
REF = "coinmetrics:PriceUSD"


def flags(**kw: Any) -> dict[str, Any]:
    """A refresh's review flags as `flags_of` writes them, every kind present."""
    empty: dict[str, list[Any]] = {"USD": [], "EUR": [], "GBP": []}
    base: dict[str, Any] = {"mismatches": [], "reference_gaps": [], "reference_outliers": []}
    return base | {"gaps": empty, "outliers": empty} | kw


class World:
    def __init__(self, tmp_path: Path) -> None:
        d = tmp_path / "data"
        d.mkdir(mode=0o700)
        data = open_data_dir(str(d))
        self.db = open_db(data)
        record_chain(self.db, "regtest")
        self.app = create_app(
            port=PORT,
            sessions=Sessions(TOKEN),
            status=lambda: NodeStatus(online=False, chain="regtest", reasons=()),
            on_quit=lambda: None,
            prices=PriceReader(lambda: open_reader(data)),
        )
        session = call(self.app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
        self.auth = {"Authorization": f"Bearer {session}"}

    def get(self, path: str) -> Reply:
        return call(self.app, "GET", path, headers=self.auth)

    def refresh(self) -> int:
        batch = store.store_refresh(
            self.db,
            [
                store.Price(date(2025, 1, 1), "USD", Decimal("93000.12"), "reference", REF),
                store.Price(date(2025, 1, 1), "EUR", Decimal("90000.10"), "fx", f"ecb:eurofxref-hist*{REF}"),
            ],
            hashes={REF: H, "ecb:eurofxref-hist": H},
            usd_through=date(2025, 1, 1),
            usd_stale=False,
            flags=flags(
                mismatches=[{"day": "2025-01-01"}], gaps={"USD": [{"first": "x"}], "EUR": [], "GBP": []}
            ),
            at=datetime(2025, 1, 10, 12, tzinfo=UTC),
        )
        assert batch is not None
        return batch


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.db.close()


@pytest.mark.parametrize("path", ["/api/prices", "/api/prices/status"])
def test_the_price_routes_need_a_session_t104(world: World, path: str) -> None:
    assert call(world.app, "GET", path).status == 401


def test_an_empty_cache_has_no_prices_and_no_refresh(world: World) -> None:
    assert world.get("/api/prices").json() == {"currency": "USD", "prices": []}
    assert world.get("/api/prices/status").json() == {"last_refresh": None, "reference_through": None}


def test_the_cached_prices_are_listed_by_currency_with_their_batch(world: World) -> None:
    batch = world.refresh()
    usd = world.get("/api/prices?currency=USD").json()
    assert usd == {
        "currency": "USD",
        "prices": [
            {"day": "2025-01-01", "price": "93000.12", "method": "reference", "source": REF, "batch": batch}
        ],
    }
    eur = world.get("/api/prices?currency=EUR").json()["prices"]
    assert [(p["price"], p["method"]) for p in eur] == [("90000.10", "fx")]


@pytest.mark.parametrize("currency", ["JPY", "usd", ""])
def test_an_unsupported_currency_is_refused(world: World, currency: str) -> None:
    reply = world.get(f"/api/prices?currency={currency}")
    assert reply.status == 422 and reply.json() == {"error": "invalid request"}


def test_the_status_gives_the_last_refresh_and_counts_its_flags(world: World) -> None:
    batch = world.refresh()
    out: dict[str, Any] = world.get("/api/prices/status").json()
    assert out["reference_through"] == "2025-01-01"
    refresh = out["last_refresh"]
    assert (refresh["batch"], refresh["at"], refresh["usd_through"], refresh["usd_stale"]) == (
        batch,
        "2025-01-10T12:00:00+00:00",
        "2025-01-01",
        False,
    )
    assert refresh["hashes"] == {REF: H, "ecb:eurofxref-hist": H}
    assert (
        refresh["flags"]["mismatches"] == 1
        and refresh["flags"]["gaps"] == 1
        and refresh["flags"]["revised"] == 0
    )


def test_a_damaged_price_is_a_422_with_the_backup_remedy_t408(world: World) -> None:
    world.refresh()
    world.db.execute("PRAGMA ignore_check_constraints = ON")
    world.db.execute("DROP TRIGGER price_changes_with_its_batch")
    world.db.execute("UPDATE price SET source = ''")
    reply = world.get("/api/prices")
    assert reply.status == 422
    assert reply.json() == {"error": "a stored price is damaged (T-408): restore the user DB from a backup"}


def test_a_busy_db_is_a_503(tmp_path: Path) -> None:
    def busy() -> Any:
        raise DbBusy("the user DB is busy; try again in a moment")

    app = create_app(
        port=PORT,
        sessions=Sessions(TOKEN),
        status=lambda: NodeStatus(online=False, chain=None, reasons=()),
        on_quit=lambda: None,
        prices=PriceReader(busy),
    )
    session = call(app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
    for path in ("/api/prices", "/api/prices/status"):
        reply = call(app, "GET", path, headers={"Authorization": f"Bearer {session}"})
        assert reply.status == 503 and "busy" in reply.json()["error"]


@pytest.mark.parametrize(
    "damage",
    [
        "json_patch(flags, '{\"mismatches\": 5}')",
        'json_patch(flags, \'{"gaps": {"USD": "x"}}\')',
        'json_patch(flags, \'{"gaps": {"JPY": []}}\')',
        "json_remove(flags, '$.mismatches')",  # a kind gone: not "no mismatches"
        "json_set(flags, '$.gaps', json('[]'))",  # a per-currency kind as a plain list
        "json_set(flags, '$.gaps', json('{}'))",  # no currency at all
        "json_set(flags, '$.mismatches', json('{}'))",
        "json_set(flags, '$.mismatches', json('[5]'))",  # a record that isn't one
        "json_remove(flags, '$.gaps.GBP')",  # a currency gone
    ],
)
def test_a_damaged_flag_is_refused_not_counted_as_nothing_t303(world: World, damage: str) -> None:
    world.refresh()
    world.db.execute("DROP TRIGGER price_batch_is_append_only_update")  # damage that bypasses the module
    world.db.execute(f"UPDATE price_batch SET flags = {damage}")  # noqa: S608 - a constant
    reply = world.get("/api/prices/status")
    assert reply.status == 422
    assert reply.json() == {"error": "a stored price is damaged (T-408): restore the user DB from a backup"}


def test_a_damaged_batch_is_a_422_on_the_status_t408(world: World) -> None:
    world.refresh()
    world.db.execute("DROP TRIGGER price_batch_is_append_only_update")
    world.db.execute("UPDATE price_batch SET flags = '{}'")  # the module's own record is gone
    reply = world.get("/api/prices/status")
    assert reply.status == 422 and "backup" in reply.json()["error"]


def test_a_stale_refresh_keeps_the_last_day_an_earlier_one_priced(world: World) -> None:
    world.refresh()
    store.store_refresh(
        world.db,
        [store.Price(date(2025, 1, 2), "USD", Decimal("94000.00"), "typical", "bitstamp:ohlc:btcusd")],
        hashes={"bitstamp:ohlc:btcusd": H},
        usd_through=None,
        usd_stale=True,
        flags=flags(gaps={"USD": [{"a": 1}], "EUR": [{"b": 2}, {"c": 3}], "GBP": []}),
        at=datetime(2025, 1, 11, 12, tzinfo=UTC),
    )
    out = world.get("/api/prices/status").json()
    assert (out["last_refresh"]["usd_through"], out["last_refresh"]["usd_stale"]) == (None, True)
    assert out["reference_through"] == "2025-01-01"  # from the earlier refresh
    assert out["last_refresh"]["flags"]["gaps"] == 3  # summed over the currencies


class _Failing:
    """A reader whose queries fail with SQLite's error `code`."""

    def __init__(self, code: str) -> None:
        self.code = code

    def execute(self, *args: object) -> object:
        e = sqlite3.DatabaseError("database disk image is malformed")
        e.sqlite_errorname = self.code
        raise e

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("code", "status"),
    [("SQLITE_CORRUPT", 422), ("SQLITE_NOTADB", 422), ("SQLITE_BUSY", 503), ("SQLITE_IOERR", 422)],
)
def test_a_failing_read_is_a_fixed_refusal(code: str, status: int) -> None:
    app = create_app(
        port=PORT,
        sessions=Sessions(TOKEN),
        status=lambda: NodeStatus(online=False, chain=None, reasons=()),
        on_quit=lambda: None,
        prices=PriceReader(lambda: _Failing(code)),  # type: ignore[arg-type, return-value]
    )
    session = call(app, "POST", "/api/session", json_body={"bootstrap": TOKEN}).json()["session"]
    for path in ("/api/prices", "/api/prices/status"):
        reply = call(app, "GET", path, headers={"Authorization": f"Bearer {session}"})
        assert reply.status == status
        assert "malformed" not in reply.json()["error"]

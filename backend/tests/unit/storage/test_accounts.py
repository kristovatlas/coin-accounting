"""Entities, tax accounts, wallet clients, addresses and descriptors in the user DB (PLAN §2;
ADR 0008, ADR 0019, ADR 0021; THREAT_MODEL T-703). Synthetic scripts only."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from coinacct.domain.keys import PrivateKeyError
from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME, AccountsError
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WIF_SHAPED = ("K" + B58)[:52]  # synthetic: shaped like a key, never a real one


def spk(n: int) -> str:
    return "0014" + f"{n:040x}"


def derived(*indexes: int, base: int = 0) -> list[tuple[int, str, str | None]]:
    return [(i, spk(base + i), None) for i in indexes]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    yield c
    c.close()


@pytest.fixture
def wallet(conn: sqlite3.Connection) -> int:
    return ac.add_tax_account(conn, "Cold storage", "self_custody")


@pytest.fixture
def exchange(conn: sqlite3.Connection) -> int:
    return ac.add_entity(conn, "An exchange", "exchange")


# --- Entities ------------------------------------------------------------------------------------


def test_the_user_entity_is_seeded_stays_and_is_the_only_self(conn: sqlite3.Connection) -> None:
    [me] = ac.entities(conn)
    assert me == ac.Entity(ME, "Me", "self", knows_identity=False, notes="")
    with pytest.raises(sqlite3.IntegrityError, match="can't be removed"):
        conn.execute("DELETE FROM entity WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE entity SET kind = 'exchange' WHERE id = 1")
    with pytest.raises(AccountsError, match="second 'self'"):
        ac.add_entity(conn, "Me again", "self")
    other = ac.add_entity(conn, "A friend", "person")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE entity SET kind = 'self' WHERE id = ?", (other,))


def test_exchanges_and_employers_know_the_users_identity_by_default(conn: sqlite3.Connection) -> None:
    exchange = ac.add_entity(conn, "An exchange", "exchange")
    employer = ac.add_entity(conn, "An employer", "employer")
    friend = ac.add_entity(conn, "A friend", "person")
    shy = ac.add_entity(conn, "A quiet exchange", "exchange", knows_identity=False)
    known = {e.id: e.knows_identity for e in ac.entities(conn)}
    assert known[exchange] and known[employer] and not known[friend] and not known[shy]


def test_a_duplicate_or_malformed_entity_is_refused_without_repeating_it(conn: sqlite3.Connection) -> None:
    ac.add_entity(conn, "Exchange A", "exchange")
    bad = (("Exchange A", "exchange"), ("", "person"), ("x" * 201, "person"), ("Pirate Bay Ltd", "pirate"))
    for name, kind in bad:
        with pytest.raises(AccountsError) as e:
            ac.add_entity(conn, name, kind)  # type: ignore[arg-type]  # an unknown kind on purpose
        assert not name or name not in str(e.value)


# --- Tax accounts --------------------------------------------------------------------------------


def test_a_custodial_account_needs_an_exchange_and_self_custody_has_none(
    conn: sqlite3.Connection, exchange: int
) -> None:
    custodial = ac.add_tax_account(
        conn, "Exchange account", "custodial", entity_id=exchange, issues_1099da=True
    )
    employer = ac.add_entity(conn, "An employer", "employer")
    bad = (("custodial", None), ("self_custody", exchange), ("custodial", employer), ("custodial", ME))
    for kind, entity in bad:
        with pytest.raises(AccountsError):
            ac.add_tax_account(conn, f"bad {kind} {entity}", kind, entity_id=entity)  # type: ignore[arg-type]
    with pytest.raises(AccountsError):
        ac.add_tax_account(conn, "No 1099-DA from yourself", "self_custody", issues_1099da=True)
    with pytest.raises(sqlite3.IntegrityError, match="stays an exchange"):
        conn.execute("UPDATE entity SET kind = 'person' WHERE id = ?", (exchange,))
    [account] = ac.tax_accounts(conn)
    assert account == ac.TaxAccount(custodial, "Exchange account", "custodial", exchange, None, True, True)


def test_no_standing_order_is_not_fifo(conn: sqlite3.Connection) -> None:
    none = ac.add_tax_account(conn, "No order on file", "self_custody")
    fifo = ac.add_tax_account(conn, "FIFO on file", "self_custody", standing_method="fifo")
    methods = {a.id: a.standing_method for a in ac.tax_accounts(conn)}
    assert methods == {none: None, fifo: "fifo"}  # ADR 0021: the fallback is told apart from an order


# --- Addresses -----------------------------------------------------------------------------------


def test_owned_addresses_belong_to_one_self_custody_account_and_others_to_none(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    custodial = ac.add_tax_account(conn, "Exchange account", "custodial", entity_id=exchange)
    for entity, account in ((ME, None), (exchange, wallet), (ME, custodial)):
        with pytest.raises(AccountsError):
            ac.add_addresses(
                conn, [(spk(1), None)], entity_id=entity, tax_account_id=account, source="manual"
            )
    assert ac.addresses(conn) == []  # nothing half-written
    ac.add_addresses(conn, [(spk(3), None)], entity_id=exchange, tax_account_id=None, source="manual")
    with pytest.raises(sqlite3.IntegrityError, match="self-custody"):
        conn.execute("UPDATE address SET entity_id = 1 WHERE script_hex = ?", (spk(3),))


def test_addresses_are_keyed_by_script_and_a_second_import_adds_only_its_clients(
    conn: sqlite3.Connection, wallet: int
) -> None:
    hw = ac.add_client(conn, "A hardware wallet", "hardware")
    phone = ac.add_client(conn, "A phone wallet", "mobile")
    first = ac.add_addresses(
        conn,
        [(spk(1), "bcrt1qexample1"), (spk(2), None)],
        entity_id=ME,
        tax_account_id=wallet,
        source="import",
        label="savings",
        start_height=100,
        client_ids=[hw],
    )
    assert first == ac.Added((spk(1), spk(2)), ())
    # PLAN §2: generated on a hardware wallet, later imported into a phone wallet.
    again = ac.add_addresses(
        conn,
        [(spk(1), "bcrt1qother")],
        entity_id=ME,
        tax_account_id=wallet,
        source="manual",
        client_ids=[phone],
    )
    assert again == ac.Added((), ())
    address = ac.addresses(conn)[0]
    assert address == ac.Address(
        spk(1), "bcrt1qexample1", ME, wallet, "savings", "import", True, 100, (hw, phone)
    )


def test_a_script_known_under_another_owner_or_account_is_reported_and_left(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    ac.add_addresses(conn, [(spk(1), None)], entity_id=exchange, tax_account_id=None, source="manual")
    other = ac.add_tax_account(conn, "Hot wallet", "self_custody")
    ac.add_addresses(conn, [(spk(2), None)], entity_id=ME, tax_account_id=other, source="import")
    batch = [(spk(1), None), (spk(2), None), (spk(3), None)]
    result = ac.add_addresses(conn, batch, entity_id=ME, tax_account_id=wallet, source="import")
    assert result == ac.Added((spk(3),), (spk(1), spk(2)))
    owners = {a.script_hex: (a.entity_id, a.tax_account_id) for a in ac.addresses(conn)}
    assert owners[spk(1)] == (exchange, None) and owners[spk(2)] == (ME, other)


def test_an_address_batch_is_all_or_nothing(conn: sqlite3.Connection, wallet: int) -> None:
    with pytest.raises(AccountsError):
        ac.add_addresses(
            conn, [(spk(1), None), ("not hex", None)], entity_id=ME, tax_account_id=wallet, source="import"
        )
    assert ac.addresses(conn) == []


# --- Descriptors ---------------------------------------------------------------------------------


def add(
    conn: sqlite3.Connection,
    text: str,
    triples: list[tuple[int, str, str | None]],
    account: int | None,
    **kw: object,
) -> int:
    return ac.add_descriptor(conn, text, triples, entity_id=ME, tax_account_id=account, gap_limit=20, **kw)  # type: ignore[arg-type]  # test helper passes the optional fields through


def test_a_descriptor_keeps_its_script_to_index_table_and_its_scripts_as_addresses(
    conn: sqlite3.Connection, wallet: int
) -> None:
    hw = ac.add_client(conn, "A hardware wallet", "hardware")
    text = "wpkh([d34db33f/84h/1h/0h]tpubexample/0/*)#checksum"
    d = add(conn, text, derived(0, 1, 2), wallet, client_ids=[hw])
    assert ac.descriptor_scripts(conn, d) == [(0, spk(0)), (1, spk(1)), (2, spk(2))]
    assert {a.script_hex: a.source for a in ac.addresses(conn)} == {spk(i): "descriptor" for i in range(3)}
    assert ac.script_index(conn, spk(1)) == [(d, 1)] and ac.script_index(conn, spk(9)) == []
    assert ac.extend_descriptor(conn, d, derived(3, 4)) == 4
    assert ac.script_index(conn, spk(4)) == [(d, 4)]
    ac.mark_used(conn, d, 3)
    ac.mark_used(conn, d, 1)  # the highest used index only rises
    [desc] = ac.descriptors(conn)
    assert desc == ac.Descriptor(d, text, ME, wallet, "", 20, 4, 3, 0, (hw,))


def test_one_index_can_derive_several_scripts(conn: sqlite3.Connection, wallet: int) -> None:
    # combo(): one key, several script types, all at one derivation index.
    triples: list[tuple[int, str, str | None]] = [(0, spk(10), None), (0, spk(11), None), (1, spk(12), None)]
    d = add(conn, "combo(tpubexample/0/*)#checksum", triples, wallet)
    assert ac.descriptor_scripts(conn, d) == [(0, spk(10)), (0, spk(11)), (1, spk(12))]
    assert ac.descriptors(conn)[0].range_end == 1
    assert ac.script_index(conn, spk(11)) == [(d, 0)]


def test_a_script_has_one_owner_and_account_across_descriptors_and_addresses(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    other = ac.add_tax_account(conn, "Hot wallet", "self_custody")
    add(conn, "wpkh(tpuba/0/*)#c1", derived(0, 1), wallet)
    # The same scripts, as another descriptor's, in another account: refused.
    with pytest.raises(AccountsError, match="another owner or account"):
        add(conn, "wpkh([ff/84h]tpuba/0/*)#c2", derived(0, 1), other)
    # An exchange's address can't be a derived script of the user's descriptor.
    ac.add_addresses(conn, [(spk(50), None)], entity_id=exchange, tax_account_id=None, source="manual")
    with pytest.raises(AccountsError, match="another owner or account"):
        add(conn, "wpkh(tpubb/0/*)#c3", [(0, spk(50), None)], wallet)
    # The same scripts by a second descriptor of the same account are fine.
    d3 = add(conn, "wpkh([ee/84h]tpuba/0/*)#c4", derived(0, 1), wallet)
    assert ac.script_index(conn, spk(1))[-1] == (d3, 1)
    with pytest.raises(sqlite3.IntegrityError, match="with its descriptor"):
        conn.execute("UPDATE address SET tax_account_id = ? WHERE script_hex = ?", (other, spk(0)))
    with pytest.raises(sqlite3.IntegrityError, match="don't change"):
        conn.execute("UPDATE descriptor SET tax_account_id = ? WHERE id = ?", (other, d3))


def test_a_descriptor_is_imported_once_and_its_window_only_widens(
    conn: sqlite3.Connection, wallet: int
) -> None:
    text = "tr(tpubexample/0/*)#checksum"
    d = add(conn, text, derived(0), wallet)
    with pytest.raises(AccountsError):
        add(conn, text, derived(5, base=100), wallet)
    with pytest.raises(AccountsError, match="past the current one"):
        ac.extend_descriptor(conn, d, derived(0, base=200))
    assert ac.extend_descriptor(conn, d, []) == 0
    with pytest.raises(AccountsError, match="at least one"):
        add(conn, "pkh(tpubother/0/*)", [], wallet)
    with pytest.raises(AccountsError, match="no such"):
        ac.extend_descriptor(conn, 999, derived(1))
    with pytest.raises(sqlite3.IntegrityError, match="within its window"):
        conn.execute(
            "INSERT INTO descriptor_script (descriptor_id, idx, script_hex) VALUES (?, 7, ?)", (d, spk(0))
        )


def test_mark_used_refuses_an_unknown_descriptor_or_an_index_outside_the_window(
    conn: sqlite3.Connection, wallet: int
) -> None:
    d = add(conn, "wpkh(tpubx/0/*)#c", derived(0, 1), wallet)
    ac.mark_used(conn, d, 1)
    for descriptor, index, reason in ((999, 0, "no such"), (d, 2, "outside"), (d, -1, "outside")):
        with pytest.raises(AccountsError, match=reason):
            ac.mark_used(conn, descriptor, index)
    assert ac.descriptors(conn)[0].highest_used == 1


def test_an_owned_descriptor_needs_a_self_custody_account(
    conn: sqlite3.Connection, wallet: int, exchange: int
) -> None:
    with pytest.raises(AccountsError):
        add(conn, "wpkh(tpubx/0/*)#c", derived(0), None)
    custodial = ac.add_tax_account(conn, "Exchange account", "custodial", entity_id=exchange)
    with pytest.raises(AccountsError):
        add(conn, "wpkh(tpubx/0/*)#c", derived(0), custodial)


# --- The backstop and the links --------------------------------------------------------------------


def test_no_private_key_reaches_the_db_whoever_calls_t703(conn: sqlite3.Connection, wallet: int) -> None:
    one: list[tuple[str, str | None]] = [(spk(1), None)]
    calls: list[Callable[[], object]] = [
        lambda: ac.add_entity(conn, "A friend", "person", notes=f"key {WIF_SHAPED}"),
        lambda: ac.add_client(conn, WIF_SHAPED, "paper"),
        lambda: ac.add_tax_account(conn, WIF_SHAPED, "self_custody"),
        lambda: ac.add_addresses(
            conn, one, entity_id=ME, tax_account_id=wallet, source="import", label=WIF_SHAPED
        ),
        lambda: add(conn, f"pkh({WIF_SHAPED})#c", derived(0), wallet),
        lambda: add(conn, "wpkh(tpubx/0/*)#c", [(0, spk(2), WIF_SHAPED)], wallet),
    ]
    for call in calls:
        with pytest.raises(PrivateKeyError):
            call()
    assert ac.addresses(conn) == [] and ac.descriptors(conn) == [] and ac.clients(conn) == []


def test_a_client_or_account_in_use_can_go_only_in_the_right_way(
    conn: sqlite3.Connection, wallet: int
) -> None:
    hw = ac.add_client(conn, "A hardware wallet", "hardware")
    ac.add_addresses(
        conn, [(spk(1), None)], entity_id=ME, tax_account_id=wallet, source="import", client_ids=[hw]
    )
    conn.execute("DELETE FROM wallet_client WHERE id = ?", (hw,))  # a label: removing it unlinks it
    assert ac.addresses(conn)[0].client_ids == ()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM tax_account WHERE id = ?", (wallet,))  # basis lives here: never silently

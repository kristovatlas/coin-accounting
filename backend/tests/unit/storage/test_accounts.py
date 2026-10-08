"""Entities, tax accounts, wallet clients, addresses and descriptors in the user DB (PLAN §2;
ADR 0008, ADR 0019). Synthetic scripts only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME, AccountsError
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db


def spk(n: int) -> str:
    return "0014" + f"{n:040x}"


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


def test_the_user_entity_is_seeded_and_stays(conn: sqlite3.Connection) -> None:
    [me] = ac.entities(conn)
    assert me == ac.Entity(ME, "Me", "self", knows_identity=False, notes="")
    with pytest.raises(sqlite3.IntegrityError, match="can't be removed"):
        conn.execute("DELETE FROM entity WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="stays the user"):
        conn.execute("UPDATE entity SET kind = 'exchange' WHERE id = 1")


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
            ac.add_entity(conn, name, kind)  # type: ignore[arg-type]
        assert not name or name not in str(e.value)


def test_a_custodial_account_needs_its_custodian_and_self_custody_has_none(conn: sqlite3.Connection) -> None:
    exchange = ac.add_entity(conn, "Exchange A", "exchange")
    custodial = ac.add_tax_account(conn, "Exchange A account", "custodial", entity_id=exchange)
    with pytest.raises(AccountsError):
        ac.add_tax_account(conn, "No custodian", "custodial")
    with pytest.raises(AccountsError):
        ac.add_tax_account(conn, "A wallet with a custodian", "self_custody", entity_id=exchange)
    [account] = ac.tax_accounts(conn)
    assert account == ac.TaxAccount(
        custodial, "Exchange A account", "custodial", exchange, "fifo", automatic=True
    )


def test_owned_addresses_belong_to_exactly_one_account_and_others_to_none(
    conn: sqlite3.Connection, wallet: int
) -> None:
    other = ac.add_entity(conn, "Employer", "employer")
    with pytest.raises(AccountsError):
        ac.add_addresses(conn, [(spk(1), None)], entity_id=ME, tax_account_id=None, source="import")
    with pytest.raises(AccountsError):
        ac.add_addresses(conn, [(spk(2), None)], entity_id=other, tax_account_id=wallet, source="manual")
    assert ac.addresses(conn) == []  # nothing half-written
    ac.add_addresses(conn, [(spk(3), None)], entity_id=other, tax_account_id=None, source="manual")
    with pytest.raises(sqlite3.IntegrityError, match="exactly one tax account"):
        conn.execute("UPDATE address SET entity_id = 1 WHERE script_hex = ?", (spk(3),))


def test_addresses_are_keyed_by_script_and_a_second_import_changes_nothing(
    conn: sqlite3.Connection, wallet: int
) -> None:
    hw = ac.add_client(conn, "A hardware wallet", "hardware")
    phone = ac.add_client(conn, "A phone wallet", "mobile")
    added = ac.add_addresses(
        conn,
        [(spk(1), "bcrt1qexample1"), (spk(2), None)],
        entity_id=ME,
        tax_account_id=wallet,
        source="import",
        label="savings",
        start_height=100,
        client_ids=[hw, phone],
    )
    assert added == [spk(1), spk(2)]
    again = ac.add_addresses(
        conn, [(spk(1), "bcrt1qother")], entity_id=ME, tax_account_id=wallet, source="manual"
    )
    assert again == []
    first = ac.addresses(conn)[0]
    assert first == ac.Address(
        spk(1), "bcrt1qexample1", ME, wallet, "savings", "import", True, 100, (hw, phone)
    )


def test_an_address_batch_is_all_or_nothing(conn: sqlite3.Connection, wallet: int) -> None:
    with pytest.raises(AccountsError):
        ac.add_addresses(
            conn, [(spk(1), None), ("not hex", None)], entity_id=ME, tax_account_id=wallet, source="import"
        )
    assert ac.addresses(conn) == []


def test_a_descriptor_keeps_its_script_to_index_table(conn: sqlite3.Connection, wallet: int) -> None:
    hw = ac.add_client(conn, "A hardware wallet", "hardware")
    text = "wpkh([d34db33f/84h/1h/0h]tpubexample/0/*)#checksum"
    d = ac.add_descriptor(
        conn,
        text,
        [spk(i) for i in range(3)],
        entity_id=ME,
        tax_account_id=wallet,
        gap_limit=20,
        client_ids=[hw],
    )
    assert ac.descriptor_scripts(conn, d) == [spk(0), spk(1), spk(2)]
    assert ac.script_index(conn, spk(1)) == [(d, 1)] and ac.script_index(conn, spk(9)) == []
    assert ac.extend_descriptor(conn, d, [spk(3), spk(4)]) == 4
    assert ac.script_index(conn, spk(4)) == [(d, 4)]
    ac.mark_used(conn, d, 3)
    ac.mark_used(conn, d, 1)  # the highest used index only rises
    [desc] = ac.descriptors(conn)
    assert desc == ac.Descriptor(d, text, ME, wallet, "", 20, 4, 3, 0, (hw,))


def test_a_descriptor_is_imported_once_and_its_scripts_never_repeat(
    conn: sqlite3.Connection, wallet: int
) -> None:
    text = "tr(tpubexample/0/*)#checksum"
    d = ac.add_descriptor(conn, text, [spk(0)], entity_id=ME, tax_account_id=wallet, gap_limit=20)
    with pytest.raises(AccountsError):
        ac.add_descriptor(conn, text, [spk(5)], entity_id=ME, tax_account_id=wallet, gap_limit=20)
    with pytest.raises(AccountsError):
        ac.extend_descriptor(conn, d, [spk(0)])  # already at index 0
    with pytest.raises(AccountsError):
        ac.mark_used(conn, d, 7)  # past the window
    with pytest.raises(AccountsError, match="at least one"):
        ac.add_descriptor(conn, "pkh(tpubother/0/*)", [], entity_id=ME, tax_account_id=wallet, gap_limit=20)
    with pytest.raises(AccountsError, match="no such"):
        ac.extend_descriptor(conn, 999, [spk(1)])


def test_an_owned_descriptor_needs_an_account(conn: sqlite3.Connection, wallet: int) -> None:
    with pytest.raises(AccountsError):
        ac.add_descriptor(conn, "wpkh(tpubx/0/*)", [spk(0)], entity_id=ME, tax_account_id=None, gap_limit=20)
    other = ac.add_entity(conn, "Employer", "employer")
    d = ac.add_descriptor(
        conn, "wpkh(tpuby/0/*)", [spk(1)], entity_id=other, tax_account_id=None, gap_limit=20
    )
    with pytest.raises(sqlite3.IntegrityError, match="exactly one tax account"):
        conn.execute("UPDATE descriptor SET tax_account_id = ? WHERE id = ?", (wallet, d))


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

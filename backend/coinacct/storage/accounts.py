"""Entities, tax accounts, wallet clients, addresses and public descriptors in the user DB (PLAN §2;
ADR 0008, ADR 0019; schema step 5).

Plain reads and writes; the rules live in the schema (an owned address or descriptor belongs to
exactly one tax account, the user entity stays) and in `services/`, which validates imports
(`domain.keys`, `domain.addresses`) before anything reaches here. A refused write raises
`AccountsError` with a fixed message: it never repeats a label, an address or a descriptor (T-403).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from coinacct.storage.db import DbError, transaction

ME: Final = 1  # the user entity, seeded by the schema

type EntityKind = Literal["self", "exchange", "employer", "merchant", "person", "unknown"]
type AccountKind = Literal["self_custody", "custodial"]
type ClientKind = Literal["hardware", "mobile", "desktop", "web", "paper", "other"]
type Method = Literal["fifo", "specific"]
type Source = Literal["import", "descriptor", "discovery", "manual"]

# PLAN §2: exchanges and employers know who the user is, by default.
KNOWS_IDENTITY_BY_DEFAULT: Final = frozenset({"exchange", "employer"})


class AccountsError(DbError):
    """A write the schema refused. The message is fixed and never repeats the data."""


@dataclass(frozen=True, slots=True)
class Entity:
    id: int
    name: str
    kind: EntityKind
    knows_identity: bool
    notes: str


@dataclass(frozen=True, slots=True)
class TaxAccount:
    id: int
    name: str
    kind: AccountKind
    entity_id: int | None
    standing_method: Method
    automatic: bool


@dataclass(frozen=True, slots=True)
class WalletClient:
    id: int
    name: str
    kind: ClientKind


@dataclass(frozen=True, slots=True)
class Address:
    script_hex: str
    text: str | None
    entity_id: int
    tax_account_id: int | None
    label: str
    source: Source
    confirmed: bool
    start_height: int
    client_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Descriptor:
    id: int
    text: str
    entity_id: int
    tax_account_id: int | None
    label: str
    gap_limit: int
    range_end: int
    highest_used: int | None
    start_height: int
    client_ids: tuple[int, ...]


def _refused(what: str) -> AccountsError:
    return AccountsError(f"the user DB refused the {what}")


def add_entity(
    conn: sqlite3.Connection,
    name: str,
    kind: EntityKind,
    *,
    knows_identity: bool | None = None,
    notes: str = "",
) -> int:
    knows = kind in KNOWS_IDENTITY_BY_DEFAULT if knows_identity is None else knows_identity
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO entity (name, kind, knows_identity, notes) VALUES (?, ?, ?, ?)",
                (name, kind, int(knows), notes),
            )
    except sqlite3.IntegrityError:
        raise _refused("entity (a duplicate name, or a value out of range)") from None
    return int(cur.lastrowid or 0)


def entities(conn: sqlite3.Connection) -> list[Entity]:
    rows = conn.execute("SELECT id, name, kind, knows_identity, notes FROM entity ORDER BY id")
    return [Entity(i, n, k, bool(ki), no) for i, n, k, ki, no in rows]


def add_tax_account(  # noqa: PLR0913 - the account's fields, all keyword-only after the kind
    conn: sqlite3.Connection,
    name: str,
    kind: AccountKind,
    *,
    entity_id: int | None = None,
    standing_method: Method = "fifo",
    automatic: bool = True,
) -> int:
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO tax_account (name, kind, entity_id, standing_method, automatic)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, kind, entity_id, standing_method, int(automatic)),
            )
    except sqlite3.IntegrityError:
        raise _refused(
            "tax account (a duplicate name, a custodial account without a custodian, or the reverse)"
        ) from None
    return int(cur.lastrowid or 0)


def tax_accounts(conn: sqlite3.Connection) -> list[TaxAccount]:
    rows = conn.execute(
        "SELECT id, name, kind, entity_id, standing_method, automatic FROM tax_account ORDER BY id"
    )
    return [TaxAccount(i, n, k, e, m, bool(a)) for i, n, k, e, m, a in rows]


def add_client(conn: sqlite3.Connection, name: str, kind: ClientKind) -> int:
    try:
        with transaction(conn):
            cur = conn.execute("INSERT INTO wallet_client (name, kind) VALUES (?, ?)", (name, kind))
    except sqlite3.IntegrityError:
        raise _refused("wallet client (a duplicate name, or an unknown kind)") from None
    return int(cur.lastrowid or 0)


def clients(conn: sqlite3.Connection) -> list[WalletClient]:
    return [
        WalletClient(i, n, k)
        for i, n, k in conn.execute("SELECT id, name, kind FROM wallet_client ORDER BY id")
    ]


def add_addresses(  # noqa: PLR0913 - one batch of addresses and the owner, account and clients they share
    conn: sqlite3.Connection,
    addresses: Iterable[tuple[str, str | None]],
    *,
    entity_id: int,
    tax_account_id: int | None,
    source: Source,
    label: str = "",
    start_height: int = 0,
    client_ids: Sequence[int] = (),
) -> list[str]:
    """Add `(script_hex, text)` pairs in one transaction. A script already in the DB is left as it is
    (its owner and account don't change behind the user's back); returns the scripts added."""
    added: list[str] = []
    try:
        with transaction(conn):
            for script_hex, text in addresses:
                cur = conn.execute(
                    "INSERT INTO address"
                    " (script_hex, text, entity_id, tax_account_id, label, source, start_height)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (script_hex) DO NOTHING",
                    (script_hex, text, entity_id, tax_account_id, label, source, start_height),
                )
                if cur.rowcount:
                    added.append(script_hex)
                    conn.executemany(
                        "INSERT INTO address_client (script_hex, client_id) VALUES (?, ?)",
                        [(script_hex, c) for c in client_ids],
                    )
    except sqlite3.IntegrityError:
        raise _refused(
            "addresses (an unknown owner, account or client, or an owned address without an account)"
        ) from None
    return added


def addresses(conn: sqlite3.Connection) -> list[Address]:
    links: dict[str, list[int]] = {}
    for script, client in conn.execute("SELECT script_hex, client_id FROM address_client ORDER BY client_id"):
        links.setdefault(script, []).append(client)
    rows = conn.execute(
        "SELECT script_hex, text, entity_id, tax_account_id, label, source, confirmed, start_height"
        " FROM address ORDER BY script_hex"
    )
    return [
        Address(s, t, e, a, lb, src, bool(c), h, tuple(links.get(s, ())))
        for s, t, e, a, lb, src, c, h in rows
    ]


def add_descriptor(  # noqa: PLR0913 - the descriptor, its owner and account, and its derived scripts
    conn: sqlite3.Connection,
    text: str,
    scripts: Sequence[str],
    *,
    entity_id: int,
    tax_account_id: int | None,
    gap_limit: int,
    label: str = "",
    start_height: int = 0,
    client_ids: Sequence[int] = (),
) -> int:
    """Add a public descriptor with the scripts it derives at indexes 0..len(scripts)-1."""
    if not scripts:
        raise AccountsError("a descriptor needs at least one derived script")
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO descriptor"
                " (text, entity_id, tax_account_id, label, gap_limit, range_end, start_height)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (text, entity_id, tax_account_id, label, gap_limit, len(scripts) - 1, start_height),
            )
            descriptor_id = int(cur.lastrowid or 0)
            conn.executemany(
                "INSERT INTO descriptor_script (descriptor_id, idx, script_hex) VALUES (?, ?, ?)",
                [(descriptor_id, i, s) for i, s in enumerate(scripts)],
            )
            conn.executemany(
                "INSERT INTO descriptor_client (descriptor_id, client_id) VALUES (?, ?)",
                [(descriptor_id, c) for c in client_ids],
            )
    except sqlite3.IntegrityError:
        raise _refused(
            "descriptor (already imported, an unknown owner, account or client, or a repeated script)"
        ) from None
    return descriptor_id


def extend_descriptor(conn: sqlite3.Connection, descriptor_id: int, scripts: Sequence[str]) -> int:
    """Append the scripts at the next indexes (a wider window, PLAN §1); returns the new range end."""
    try:
        with transaction(conn):
            row = conn.execute("SELECT range_end FROM descriptor WHERE id = ?", (descriptor_id,)).fetchone()
            if row is None:
                raise AccountsError("there is no such descriptor")
            start = int(row[0]) + 1
            conn.executemany(
                "INSERT INTO descriptor_script (descriptor_id, idx, script_hex) VALUES (?, ?, ?)",
                [(descriptor_id, start + i, s) for i, s in enumerate(scripts)],
            )
            end = start + len(scripts) - 1
            conn.execute(
                "UPDATE descriptor SET range_end = ? WHERE id = ?", (max(end, start - 1), descriptor_id)
            )
    except sqlite3.IntegrityError:
        raise _refused("descriptor's new scripts (a repeated script, or past the largest window)") from None
    return max(end, start - 1)


def mark_used(conn: sqlite3.Connection, descriptor_id: int, index: int) -> None:
    """Record that index `index` has been seen on chain: the highest-used index only ever rises."""
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE descriptor SET highest_used = max(coalesce(highest_used, -1), ?) WHERE id = ?",
                (index, descriptor_id),
            )
    except sqlite3.IntegrityError:
        raise _refused("used index (past the descriptor's window)") from None


def descriptors(conn: sqlite3.Connection) -> list[Descriptor]:
    links: dict[int, list[int]] = {}
    for d, client in conn.execute(
        "SELECT descriptor_id, client_id FROM descriptor_client ORDER BY client_id"
    ):
        links.setdefault(int(d), []).append(int(client))
    rows = conn.execute(
        "SELECT id, text, entity_id, tax_account_id, label, gap_limit, range_end, highest_used, start_height"
        " FROM descriptor ORDER BY id"
    )
    return [
        Descriptor(i, t, e, a, lb, g, r, hu, h, tuple(links.get(i, ())))
        for i, t, e, a, lb, g, r, hu, h in rows
    ]


def descriptor_scripts(conn: sqlite3.Connection, descriptor_id: int) -> list[str]:
    """The descriptor's derived scripts, by index."""
    rows = conn.execute(
        "SELECT script_hex FROM descriptor_script WHERE descriptor_id = ? ORDER BY idx", (descriptor_id,)
    )
    return [s for (s,) in rows]


def script_index(conn: sqlite3.Connection, script_hex: str) -> list[tuple[int, int]]:
    """Which descriptors derive `script_hex`, and at which index: the script-to-index table that maps
    scan hits back to derivation indexes (PLAN §1)."""
    rows = conn.execute(
        "SELECT descriptor_id, idx FROM descriptor_script WHERE script_hex = ? ORDER BY descriptor_id",
        (script_hex,),
    )
    return [(int(d), int(i)) for d, i in rows]

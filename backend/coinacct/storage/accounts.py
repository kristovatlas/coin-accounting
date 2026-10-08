"""Entities, tax accounts, wallet clients, addresses and public descriptors in the user DB (PLAN §2;
ADR 0008, ADR 0019; schema step 5).

Plain reads and writes; the rules live in the schema (a script has one owner and account, an
owned one a self-custody account; the user entity stays) and in `services/`, which validates imports
(`domain.keys`, `domain.addresses`) before anything reaches here. As a backstop, every text written
here also passes `domain.keys.refuse_private` (T-703), which raises `domain.keys.PrivateKeyError`.
Any other refused write raises `AccountsError` with a fixed message: it never repeats a label, an
address or a descriptor (T-403).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Literal

from coinacct.domain.keys import refuse_private
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
    standing_method: Method | None  # None: no standing order, so the statutory FIFO fallback applies
    automatic: bool
    issues_1099da: bool


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


def _no_keys(*texts: str) -> None:
    """A backstop to the import service: no private key reaches the DB, whoever calls (T-703)."""
    for text in texts:
        refuse_private(text)


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """A read in one snapshot: a deferred transaction, which under WAL takes no write lock (unlike
    `transaction`'s BEGIN IMMEDIATE). Inside a transaction already, that transaction's snapshot."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("COMMIT")


def _gap_free(derived: Sequence[tuple[int, str, str | None]], first: int) -> int:
    """The window's new end; `AccountsError` unless the indexes are exactly first..end (no gaps)."""
    indexes = {i for i, _, _ in derived}
    end = max(indexes)
    if indexes != set(range(first, end + 1)):
        raise AccountsError("a descriptor's derived indexes run without gaps from the window's next index")
    return end


def _new_id(cur: sqlite3.Cursor) -> int:
    if cur.lastrowid is None:
        raise AccountsError("the user DB didn't return the new row's id")
    return int(cur.lastrowid)


def add_entity(
    conn: sqlite3.Connection,
    name: str,
    kind: EntityKind,
    *,
    knows_identity: bool | None = None,
    notes: str = "",
) -> int:
    knows = kind in KNOWS_IDENTITY_BY_DEFAULT if knows_identity is None else knows_identity
    _no_keys(name, notes)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO entity (name, kind, knows_identity, notes) VALUES (?, ?, ?, ?)",
                (name, kind, int(knows), notes),
            )
    except sqlite3.IntegrityError:
        raise _refused("entity (a duplicate name, a second 'self', or a value out of range)") from None
    return _new_id(cur)


def entities(conn: sqlite3.Connection) -> list[Entity]:
    rows = conn.execute("SELECT id, name, kind, knows_identity, notes FROM entity ORDER BY id")
    return [Entity(i, n, k, bool(ki), no) for i, n, k, ki, no in rows]


def add_tax_account(  # noqa: PLR0913 - the account's fields, all keyword-only after the kind
    conn: sqlite3.Connection,
    name: str,
    kind: AccountKind,
    *,
    entity_id: int | None = None,
    standing_method: Method | None = None,
    automatic: bool = True,
    issues_1099da: bool = False,
) -> int:
    _no_keys(name)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO tax_account (name, kind, entity_id, standing_method, automatic, issues_1099da)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, kind, entity_id, standing_method, int(automatic), int(issues_1099da)),
            )
    except sqlite3.IntegrityError:
        raise _refused(
            "tax account (a duplicate name, a custodial account without a custodian, or the reverse)"
        ) from None
    return _new_id(cur)


def tax_accounts(conn: sqlite3.Connection) -> list[TaxAccount]:
    rows = conn.execute(
        "SELECT id, name, kind, entity_id, standing_method, automatic, issues_1099da"
        " FROM tax_account ORDER BY id"
    )
    return [TaxAccount(i, n, k, e, m, bool(a), bool(f)) for i, n, k, e, m, a, f in rows]


def add_client(conn: sqlite3.Connection, name: str, kind: ClientKind) -> int:
    _no_keys(name)
    try:
        with transaction(conn):
            cur = conn.execute("INSERT INTO wallet_client (name, kind) VALUES (?, ?)", (name, kind))
    except sqlite3.IntegrityError:
        raise _refused("wallet client (a duplicate name, or an unknown kind)") from None
    return _new_id(cur)


def clients(conn: sqlite3.Connection) -> list[WalletClient]:
    return [
        WalletClient(i, n, k)
        for i, n, k in conn.execute("SELECT id, name, kind FROM wallet_client ORDER BY id")
    ]


@dataclass(frozen=True, slots=True)
class Added:
    """What an address batch did: the scripts added, and those already known under another owner or
    account, which were left as they are (a conflict for the user to settle, never a silent move)."""

    added: tuple[str, ...]
    conflicts: tuple[str, ...]


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
) -> Added:
    """Add `(script_hex, text)` pairs in one transaction. A script already known under the same owner
    and account gets the new client links (PLAN §2: an address can live in several wallets) and the
    earlier of the two start heights; one known under another owner or account is left as it is and
    reported."""
    rows = list(addresses)
    _no_keys(label, *(t for _, t in rows if t))
    added: list[str] = []
    conflicts: list[str] = []
    try:
        with transaction(conn):
            for script_hex, text in rows:
                cur = conn.execute(
                    "INSERT INTO address"
                    " (script_hex, text, entity_id, tax_account_id, label, source, start_height)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (script_hex) DO NOTHING",
                    (script_hex, text, entity_id, tax_account_id, label, source, start_height),
                )
                if cur.rowcount:
                    added.append(script_hex)
                elif not _owned_by(conn, script_hex, entity_id, tax_account_id):
                    conflicts.append(script_hex)
                    continue
                else:
                    # Imported again with an earlier start: its earlier history is scanned too.
                    conn.execute(
                        "UPDATE address SET start_height = MIN(start_height, ?) WHERE script_hex = ?",
                        (start_height, script_hex),
                    )
                conn.executemany(
                    "INSERT INTO address_client (script_hex, client_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    [(script_hex, c) for c in client_ids],
                )
    except sqlite3.IntegrityError:
        raise _refused(
            "addresses (an unknown owner, account or client,"
            " or an owned address without a self-custody account)"
        ) from None
    return Added(tuple(added), tuple(conflicts))


def _owned_by(conn: sqlite3.Connection, script_hex: str, entity_id: int, tax_account_id: int | None) -> bool:
    row = conn.execute(
        "SELECT 1 FROM address WHERE script_hex = ? AND entity_id = ? AND tax_account_id IS ?",
        (script_hex, entity_id, tax_account_id),
    ).fetchone()
    return row is not None


def addresses(conn: sqlite3.Connection) -> list[Address]:
    with _snapshot(conn):  # the rows and their links from one snapshot
        links: dict[str, list[int]] = {}
        for script, client in conn.execute(
            "SELECT script_hex, client_id FROM address_client ORDER BY client_id"
        ):
            links.setdefault(script, []).append(client)
        rows = conn.execute(
            "SELECT script_hex, text, entity_id, tax_account_id, label, source, confirmed, start_height"
            " FROM address ORDER BY script_hex"
        ).fetchall()
    return [
        Address(s, t, e, a, lb, src, bool(c), h, tuple(links.get(s, ())))
        for s, t, e, a, lb, src, c, h in rows
    ]


def add_descriptor(  # noqa: PLR0913 - the descriptor, its owner and account, and its derived scripts
    conn: sqlite3.Connection,
    text: str,
    derived: Sequence[tuple[int, str, str | None]],
    *,
    entity_id: int,
    tax_account_id: int | None,
    gap_limit: int,
    label: str = "",
    start_height: int = 0,
    client_ids: Sequence[int] = (),
) -> int:
    """Add a public descriptor with the `(index, script_hex, address)` triples it derives (an index can
    derive several scripts, as combo() does); its window ends at the highest index. Each derived
    script becomes an address of the descriptor's owner and account (the one record of who owns a
    script); a script already owned otherwise refuses the import."""
    if not derived:
        raise AccountsError("a descriptor needs at least one derived script")
    end = _gap_free(derived, 0)
    _no_keys(text, label, *(t for _, _, t in derived if t))
    try:
        with transaction(conn):
            known = conn.execute(
                "SELECT id, entity_id, tax_account_id FROM descriptor WHERE text = ?", (text,)
            ).fetchone()
            if known is not None:
                # Imported again, from another wallet client (PLAN §2): link it, and keep the earlier
                # start height so that history is scanned too; nothing else changes.
                if (known[1], known[2]) != (entity_id, tax_account_id):
                    raise AccountsError("the descriptor is already imported under another owner or account")
                conn.execute(
                    "UPDATE descriptor SET start_height = MIN(start_height, ?) WHERE id = ?",
                    (start_height, known[0]),
                )
                conn.executemany(
                    "INSERT INTO descriptor_client (descriptor_id, client_id) VALUES (?, ?)"
                    " ON CONFLICT DO NOTHING",
                    [(known[0], c) for c in client_ids],
                )
                return int(known[0])
            cur = conn.execute(
                "INSERT INTO descriptor"
                " (text, entity_id, tax_account_id, label, gap_limit, range_end, start_height)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    text,
                    entity_id,
                    tax_account_id,
                    label,
                    gap_limit,
                    end,
                    start_height,
                ),
            )
            descriptor_id = _new_id(cur)
            _add_derived(
                conn,
                descriptor_id,
                derived,
                entity_id=entity_id,
                tax_account_id=tax_account_id,
                start_height=start_height,
            )
            conn.executemany(
                "INSERT INTO descriptor_client (descriptor_id, client_id) VALUES (?, ?)",
                [(descriptor_id, c) for c in client_ids],
            )
    except sqlite3.IntegrityError:
        raise _refused(
            "descriptor (already imported, an unknown owner, account or client, a repeated script,"
            " or a script that already belongs to another owner or account)"
        ) from None
    return descriptor_id


def _add_derived(  # noqa: PLR0913 - where the scripts go and whose they are
    conn: sqlite3.Connection,
    descriptor_id: int,
    derived: Sequence[tuple[int, str, str | None]],
    *,
    entity_id: int,
    tax_account_id: int | None,
    start_height: int,
) -> None:
    for index, script_hex, address_text in derived:
        conn.execute(
            "INSERT INTO address (script_hex, text, entity_id, tax_account_id, source, start_height)"
            " VALUES (?, ?, ?, ?, 'descriptor', ?) ON CONFLICT (script_hex) DO NOTHING",
            (script_hex, address_text, entity_id, tax_account_id, start_height),
        )
        conn.execute(
            "INSERT INTO descriptor_script (descriptor_id, idx, script_hex) VALUES (?, ?, ?)",
            (descriptor_id, index, script_hex),
        )


def extend_descriptor(
    conn: sqlite3.Connection, descriptor_id: int, derived: Sequence[tuple[int, str, str | None]]
) -> int:
    """Add the `(index, script_hex, address)` triples past the window's end (a wider window, PLAN §1);
    returns the new range end."""
    _no_keys(*(t for _, _, t in derived if t))
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT range_end, entity_id, tax_account_id, start_height FROM descriptor WHERE id = ?",
                (descriptor_id,),
            ).fetchone()
            if row is None:
                raise AccountsError("there is no such descriptor")
            range_end, entity_id, tax_account_id, start_height = row
            if not derived:
                return int(range_end)
            end = _gap_free(derived, int(range_end) + 1)
            conn.execute("UPDATE descriptor SET range_end = ? WHERE id = ?", (end, descriptor_id))
            _add_derived(
                conn,
                descriptor_id,
                derived,
                entity_id=entity_id,
                tax_account_id=tax_account_id,
                start_height=start_height,
            )
    except sqlite3.IntegrityError:
        raise _refused(
            "descriptor's new scripts (a repeated script, one that belongs to another owner or account,"
            " or past the largest window)"
        ) from None
    return end


def mark_used(conn: sqlite3.Connection, descriptor_id: int, index: int) -> None:
    """Record that index `index` (within the window) has been seen on chain: the highest-used index
    only ever rises."""
    with transaction(conn):
        row = conn.execute("SELECT range_end FROM descriptor WHERE id = ?", (descriptor_id,)).fetchone()
        if row is None:
            raise AccountsError("there is no such descriptor")
        if not 0 <= index <= int(row[0]):
            raise AccountsError("the index is outside the descriptor's window")
        conn.execute(
            "UPDATE descriptor SET highest_used = max(coalesce(highest_used, -1), ?) WHERE id = ?",
            (index, descriptor_id),
        )


def descriptors(conn: sqlite3.Connection) -> list[Descriptor]:
    with _snapshot(conn):  # the rows and their links from one snapshot
        links: dict[int, list[int]] = {}
        for d, client in conn.execute(
            "SELECT descriptor_id, client_id FROM descriptor_client ORDER BY client_id"
        ):
            links.setdefault(int(d), []).append(int(client))
        rows = conn.execute(
            "SELECT id, text, entity_id, tax_account_id, label, gap_limit, range_end, highest_used,"
            " start_height FROM descriptor ORDER BY id"
        ).fetchall()
    return [
        Descriptor(i, t, e, a, lb, g, r, hu, h, tuple(links.get(i, ())))
        for i, t, e, a, lb, g, r, hu, h in rows
    ]


def descriptor_scripts(conn: sqlite3.Connection, descriptor_id: int) -> list[tuple[int, str]]:
    """The descriptor's derived `(index, script_hex)` pairs, by index (several for one index, as
    combo() derives)."""
    rows = conn.execute(
        "SELECT idx, script_hex FROM descriptor_script WHERE descriptor_id = ? ORDER BY idx, script_hex",
        (descriptor_id,),
    )
    return [(int(i), s) for i, s in rows]


def script_index(conn: sqlite3.Connection, script_hex: str) -> list[tuple[int, int]]:
    """Which descriptors derive `script_hex`, and at which index: the script-to-index table that maps
    scan hits back to derivation indexes (PLAN §1)."""
    rows = conn.execute(
        "SELECT descriptor_id, idx FROM descriptor_script WHERE script_hex = ? ORDER BY descriptor_id",
        (script_hex,),
    )
    return [(int(d), int(i)) for d, i in rows]

"""Tags from the graph: whose an address is, and whether a transaction is a mix (PLAN §2, §4;
THREAT_MODEL T-408, T-504).

Each change is written together with a `change_log` row, in one transaction, so the log can't miss
an edit or record one that didn't happen. The log is append-only (the schema refuses any update or
delete, `migrations/m0006_tags`).

- `tag_address` sets an address's owner, tax account, label and wallet clients. An address not yet in
  the user DB is added (`source` 'manual'); the schema's own rules still hold: the user's address
  belongs to exactly one self-custody account, anyone else's to none, and an address a descriptor
  derives changes owner only with its descriptor.
- `set_mixing` sets or clears a transaction's mixing flag, as the user's choice.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from coinacct.domain.chain import is_hash
from coinacct.domain.keys import refuse_private
from coinacct.storage.accounts import AccountsError
from coinacct.storage.db import transaction


@dataclass(frozen=True, slots=True)
class Change:
    id: int
    at: str
    kind: str
    subject: str
    before: dict[str, Any] | None
    after: dict[str, Any]


def _address(conn: sqlite3.Connection, script_hex: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT entity_id, tax_account_id, label FROM address WHERE script_hex = ?", (script_hex,)
    ).fetchone()
    if row is None:
        return None
    clients = [
        c
        for (c,) in conn.execute(
            "SELECT client_id FROM address_client WHERE script_hex = ? ORDER BY client_id", (script_hex,)
        )
    ]
    return {"entity_id": row[0], "tax_account_id": row[1], "label": row[2], "client_ids": clients}


def _log(conn: sqlite3.Connection, at: str, entry: tuple[str, str, Any, Any]) -> None:
    """Append `(kind, subject, before, after)` to the change log."""
    kind, subject, before, after = entry
    conn.execute(
        "INSERT INTO change_log (at, kind, subject, before, after) VALUES (?, ?, ?, ?, ?)",
        (
            at,
            kind,
            subject,
            None if before is None else json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True),
        ),
    )


def tag_address(  # noqa: PLR0913 - the address, its owner and account, label, clients and the time
    conn: sqlite3.Connection,
    script_hex: str,
    *,
    text: str | None,
    entity_id: int,
    tax_account_id: int | None,
    label: str,
    client_ids: Sequence[int],
    at: str,
) -> bool:
    """Set whose `script_hex` is. Returns True if the address was new to the user DB. A change that
    changes nothing is not logged."""
    refuse_private(label)  # a backstop: no private key reaches the DB, whoever calls (T-703)
    if text is not None:
        refuse_private(text)
    clients = sorted(set(client_ids))
    after: dict[str, Any] = {
        "entity_id": entity_id,
        "tax_account_id": tax_account_id,
        "label": label,
        "client_ids": clients,
    }
    try:
        with transaction(conn):
            before = _address(conn, script_hex)
            if before == after:
                return False
            if before is None:
                conn.execute(
                    "INSERT INTO address (script_hex, text, entity_id, tax_account_id, label, source)"
                    " VALUES (?, ?, ?, ?, ?, 'manual')",
                    (script_hex, text, entity_id, tax_account_id, label),
                )
            else:
                if (before["entity_id"], before["tax_account_id"]) != (entity_id, tax_account_id):
                    conn.execute(
                        "UPDATE address SET entity_id = ?, tax_account_id = ? WHERE script_hex = ?",
                        (entity_id, tax_account_id, script_hex),
                    )
                if before["label"] != label:
                    conn.execute("UPDATE address SET label = ? WHERE script_hex = ?", (label, script_hex))
                conn.execute("DELETE FROM address_client WHERE script_hex = ?", (script_hex,))
            conn.executemany(
                "INSERT INTO address_client (script_hex, client_id) VALUES (?, ?)",
                [(script_hex, c) for c in clients],
            )
            _log(conn, at, ("address_tag", script_hex, before, after))
    except sqlite3.IntegrityError:
        raise AccountsError(
            "that tag (an unknown owner, account or client; the user's address needs one self-custody"
            " account, anyone else's none; a descriptor's address changes owner only with its descriptor)"
        ) from None
    return before is None


def set_mixing(conn: sqlite3.Connection, txid: str, mixing: bool, *, at: str) -> None:
    """The user's mixing flag for `txid`. Setting the value it already has is not logged."""
    if not is_hash(txid):
        raise ValueError("a txid is 64 lowercase hex digits")
    with transaction(conn):
        row = conn.execute("SELECT mixing, source FROM tx_flag WHERE txid = ?", (txid,)).fetchone()
        before = None if row is None else {"mixing": bool(row[0]), "source": row[1]}
        after = {"mixing": mixing, "source": "user"}
        if before == after:
            return
        conn.execute(
            "INSERT INTO tx_flag (txid, mixing, source) VALUES (?, ?, 'user')"
            " ON CONFLICT (txid) DO UPDATE SET mixing = excluded.mixing, source = 'user'",
            (txid, int(mixing)),
        )
        _log(conn, at, ("tx_flag", txid, before, after))


def mixing(conn: sqlite3.Connection, txid: str) -> bool | None:
    """The mixing flag of `txid`, or None if nobody has set one."""
    row = conn.execute("SELECT mixing FROM tx_flag WHERE txid = ?", (txid,)).fetchone()
    return None if row is None else bool(row[0])


def changes(conn: sqlite3.Connection, subject: str | None = None) -> list[Change]:
    """The change log, oldest first; or one subject's."""
    sql = "SELECT id, at, kind, subject, before, after FROM change_log"
    rows = conn.execute(
        sql + (" WHERE subject = ?" if subject else "") + " ORDER BY id", (subject,) if subject else ()
    ).fetchall()
    return [
        Change(i, a, k, s, None if b is None else json.loads(b), json.loads(af)) for i, a, k, s, b, af in rows
    ]

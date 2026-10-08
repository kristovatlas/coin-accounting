"""The append-only change log (PLAN §2; THREAT_MODEL T-408): what was edited, when, before and after.

Writers append in the same transaction as the edit they record, so the log can't miss an edit or
record one that didn't happen. The schema refuses any update, delete or replacement of a row
(`migrations/m0006_tags`, `m0007`, `m0008`, `m0009`). Readers are in `storage.tags.changes`.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any, Literal


def now() -> str:
    """The time an edit is logged at, in UTC, when the caller has none of its own."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def address_state(conn: sqlite3.Connection, script_hex: str) -> dict[str, Any] | None:
    """What the change log records of an address's tag: owner, account, label, text, source and its
    wallet clients; None if the user DB doesn't hold it."""
    row = conn.execute(
        "SELECT entity_id, tax_account_id, label, text, source FROM address WHERE script_hex = ?",
        (script_hex,),
    ).fetchone()
    if row is None:
        return None
    clients = [
        c
        for (c,) in conn.execute(
            "SELECT client_id FROM address_client WHERE script_hex = ? ORDER BY client_id", (script_hex,)
        )
    ]
    return {
        "entity_id": row[0],
        "tax_account_id": row[1],
        "label": row[2],
        "text": row[3],
        "source": row[4],
        "client_ids": clients,
    }


type Origin = Literal["user", "import", "descriptor", "discovery", "heuristic", "suggestion"]


def append(conn: sqlite3.Connection, at: str, entry: tuple[str, str, Any, Any], origin: Origin) -> None:
    """Append `(kind, subject, before, after)` to the change log, with what made the change
    (`migrations/m0010_change_log_origin`)."""
    kind, subject, before, after = entry
    conn.execute(
        "INSERT INTO change_log (at, kind, subject, before, after, origin) VALUES (?, ?, ?, ?, ?, ?)",
        (
            at,
            kind,
            subject,
            None if before is None else json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True),
            origin,
        ),
    )

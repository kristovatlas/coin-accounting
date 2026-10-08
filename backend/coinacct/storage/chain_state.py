"""The chain this data directory belongs to, and the last tip the app saw (PLAN §1 "Reorgs and new
blocks"; THREAT_MODEL T-206, T-207).

The chain is recorded the first time the app talks to a node and never changes afterwards; the
node checks compare against it, and a mismatch means offline mode (ADR 0004). The tip is what the
reorg check walks back from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from coinacct.storage.db import DbError, transaction

CHAINS: Final = frozenset({"main", "test", "testnet4", "signet", "regtest"})


@dataclass(frozen=True, slots=True)
class Tip:
    blockhash: str
    height: int


def recorded_chain(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT chain FROM chain_state WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def record_chain(conn: sqlite3.Connection, chain: str) -> None:
    """Record the chain for a new data directory. Recording the same chain again is a no-op;
    another one is refused (T-206)."""
    if chain not in CHAINS:
        raise ValueError("unknown chain")
    with transaction(conn):
        existing = recorded_chain(conn)
        if existing is None:
            conn.execute("INSERT INTO chain_state (id, chain) VALUES (1, ?)", (chain,))
        elif existing != chain:
            raise DbError(f"this data directory belongs to {existing}, not {chain} (T-206)")


def last_tip(conn: sqlite3.Connection) -> Tip | None:
    row = conn.execute("SELECT tip_hash, tip_height FROM chain_state WHERE id = 1").fetchone()
    if row is None or row[0] is None:
        return None
    return Tip(str(row[0]), int(row[1]))


def set_tip(conn: sqlite3.Connection, tip: Tip) -> None:
    """Record the tip the app last processed. The chain must have been recorded first."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE chain_state SET tip_hash = ?, tip_height = ? WHERE id = 1", (tip.blockhash, tip.height)
        )
        if cur.rowcount != 1:
            raise DbError("the chain must be recorded before a tip")

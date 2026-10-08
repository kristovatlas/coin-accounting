"""The chain-data cache: the node's answers, kept in the user DB on the volume (PLAN §1 "Caching and
chain state", §2; THREAT_MODEL T-207, T-208, T-209).

It reveals which scripts and transactions the user cares about, so it lives only here (T-209). Its
rules keep a reorg from leaving stale or orphaned data behind (T-207):

- **Two recorded tips** (architecture §8). The *last-seen tip* moves only once a catch-up has
  extended coverage to the node's tip. The *scan target* is the tip that catch-up is working
  towards. Rows are written against the **reference tip**: the scan target while there is one,
  otherwise the last-seen tip. A failed scan leaves the target in place, so it is retried.
- **Every write names the tip it was computed against**, which must be the reference tip, and no
  row may lie above it (a row at its height must be from its block). A block a row came from that
  is later orphaned is then always above the fork point found by walking back from the reference
  tip, so `invalidate_above` removes it. A caller reads the tip before its RPC calls; a tip that
  moved in between is `StaleTipError` here.
- **A row is written once.** Writing the same row again is a no-op; a different row under the same
  key is refused, never silently kept or replaced.
- **Negative answers are snapshots** ("unspent at this tip"), never facts.
- **Coverage** is one contiguous height range per subject, with its stop block's hash.
- **Mempool results never come here;** they are rebuilt on every refresh.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from coinacct.domain.chain import MAX_SATS, Outpoint, Tx, TxIn, TxOut, is_hash, is_hex
from coinacct.storage.chain_state import Tip, last_tip, set_tip
from coinacct.storage.db import DbError, transaction

MAX_SEQUENCE = 0xFFFFFFFF


class StaleTipError(DbError):
    """The data was computed against a tip that is no longer the recorded one: fetch it again."""


@dataclass(frozen=True, slots=True)
class Activity:
    """A receive (`n` is the output index) or a spend (`n` is the input index, and `prevout` the
    output it spent) of `script_hex`, in block `blockhash` at `height`."""

    kind: Literal["receive", "spend"]
    script_hex: str
    txid: str
    n: int
    sats: int
    blockhash: str
    height: int
    prevout: Outpoint | None = None


@dataclass(frozen=True, slots=True)
class SpentBy:
    txid: str
    blockhash: str
    height: int


@dataclass(frozen=True, slots=True)
class Coverage:
    subject: str
    start_height: int
    stop_height: int
    stop_hash: str


@dataclass(frozen=True, slots=True)
class Invalidated:
    """What a reorg removed, for `services/` to flag the events built on it for review."""

    fork: Tip
    txids: frozenset[str]
    unspent: frozenset[Outpoint]
    subjects: frozenset[str]


def scan_target(conn: sqlite3.Connection) -> Tip | None:
    row = conn.execute("SELECT target_hash, target_height FROM chain_state WHERE id = 1").fetchone()
    return None if row is None or row[0] is None else Tip(str(row[0]), int(row[1]))


def reference_tip(conn: sqlite3.Connection) -> Tip | None:
    """The tip rows are written against, and the one a reorg check walks back from: the scan target
    while a catch-up is under way, otherwise the last-seen tip."""
    return scan_target(conn) or last_tip(conn)


def set_scan_target(conn: sqlite3.Connection, target: Tip) -> None:
    """Start (or restart) a catch-up towards `target`, the new reference tip. Rows from blocks no
    longer in the active chain must already be gone (`invalidate_above`), so `target` can't lie
    below the reference tip, or at its height in another block (#167): either would leave rows above
    the reference tip, or orphaned rows no later walk back from `target` finds (T-207)."""
    with transaction(conn):
        ref = reference_tip(conn)
        if ref is not None and (
            target.height < ref.height or (target.height == ref.height and target != ref)
        ):
            raise DbError("a scan target can't be below the reference tip; invalidate first (T-207)")
        _write_target(conn, target)


def _write_target(conn: sqlite3.Connection, target: Tip) -> None:
    cur = conn.execute(
        "UPDATE chain_state SET target_hash = ?, target_height = ? WHERE id = 1",
        (target.blockhash, target.height),
    )
    if cur.rowcount != 1:
        raise DbError("the chain must be recorded before a tip")


def complete_scan_target(conn: sqlite3.Connection, target: Tip) -> None:
    """The catch-up towards `target` is done: it becomes the last-seen tip."""
    with transaction(conn):
        if scan_target(conn) != target:
            raise StaleTipError("the scan target moved; catch up again (T-207)")
        set_tip(conn, target)
        conn.execute("UPDATE chain_state SET target_hash = NULL, target_height = NULL WHERE id = 1")


def set_scan_marker(conn: sqlite3.Connection, subject: str) -> None:
    """Record that a `scanblocks` call for `subject` is about to start (architecture §8.2, T-212)."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO scan_marker (id, subject) VALUES (1, ?)"
            " ON CONFLICT DO UPDATE SET subject = excluded.subject",
            (subject,),
        )


def clear_scan_marker(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM scan_marker")


def scan_marker(conn: sqlite3.Connection) -> str | None:
    """The subject of a `scanblocks` call the node may still be running, or None."""
    row = conn.execute("SELECT subject FROM scan_marker WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def _at(conn: sqlite3.Connection, at: Tip, height: int, blockhash: str) -> None:
    if reference_tip(conn) != at:
        raise StaleTipError("the tip moved while chain data was read from the node; read it again (T-207)")
    if height > at.height:
        raise DbError("chain data can't lie above the tip it was computed against (T-207)")
    if height == at.height and blockhash != at.blockhash:
        raise DbError("chain data at the tip's height must be from the tip's block (T-207)")


def put_tx(conn: sqlite3.Connection, tx: Tx, height: int, at: Tip) -> None:
    """Cache a confirmed transaction, found in its block at `height` (from that block's header, not
    from the transaction's confirmations, which count from whatever the node's tip was). Its
    confirmations aren't stored: `get_tx` counts them from the reference tip."""
    if tx.blockhash is None or not tx.confirmed:
        raise ValueError("only confirmed transactions are cached")
    data = _encode(tx)
    with transaction(conn):
        _at(conn, at, height, tx.blockhash)
        known = conn.execute(
            "SELECT height, data FROM tx_cache WHERE txid = ? AND blockhash = ?", (tx.txid, tx.blockhash)
        ).fetchone()
        if known is not None:
            if tuple(known) != (height, data):
                raise DbError("a cached transaction can't change (T-207)")
            return
        conn.execute(
            "INSERT INTO tx_cache (txid, blockhash, height, data) VALUES (?, ?, ?, ?)",
            (tx.txid, tx.blockhash, height, data),
        )


def get_tx(conn: sqlite3.Connection, txid: str, blockhash: str) -> Tx | None:
    """The cached transaction, with its confirmations counted from the reference tip."""
    row = conn.execute(
        "SELECT height, data FROM tx_cache WHERE txid = ? AND blockhash = ?", (txid, blockhash)
    ).fetchone()
    if row is None:
        return None
    tip = reference_tip(conn)
    height = int(row[0])
    if tip is None or height > tip.height:
        raise DbError("a cached transaction lies above the tip (T-207)")
    return _decode(txid, blockhash, tip.height - height + 1, str(row[1]))


def put_spender(conn: sqlite3.Connection, outpoint: Outpoint, spent_by: SpentBy, at: Tip) -> None:
    """Record a confirmed spend. It replaces an "unspent" snapshot of the output; recording it again
    is a no-op, and a different spend of the same output is refused."""
    if not is_hash(spent_by.txid) or not is_hash(spent_by.blockhash):
        raise ValueError("a spend needs a txid and a block hash")
    with transaction(conn):
        _at(conn, at, spent_by.height, spent_by.blockhash)
        known = get_spender(conn, outpoint)
        if known is not None:
            if known != spent_by:
                raise DbError("an output can't have two confirmed spends (T-207)")
            return
        conn.execute(
            "INSERT INTO spender (txid, vout, spend_txid, blockhash, height) VALUES (?, ?, ?, ?, ?)",
            (outpoint.txid, outpoint.vout, spent_by.txid, spent_by.blockhash, spent_by.height),
        )
        conn.execute(
            "DELETE FROM snapshot WHERE kind = 'unspent' AND txid = ? AND vout = ?",
            (outpoint.txid, outpoint.vout),
        )


def get_spender(conn: sqlite3.Connection, outpoint: Outpoint) -> SpentBy | None:
    row = conn.execute(
        "SELECT spend_txid, blockhash, height FROM spender WHERE txid = ? AND vout = ?",
        (outpoint.txid, outpoint.vout),
    ).fetchone()
    return None if row is None else SpentBy(str(row[0]), str(row[1]), int(row[2]))


def put_unspent(conn: sqlite3.Connection, outpoint: Outpoint, at: Tip) -> None:
    """Snapshot "unspent at `at`", replacing an older snapshot. A recorded spend contradicts it."""
    with transaction(conn):
        _at(conn, at, at.height, at.blockhash)
        if get_spender(conn, outpoint) is not None:
            raise DbError("an output with a recorded spend can't be unspent (T-207)")
        conn.execute(
            "INSERT INTO snapshot (kind, txid, vout, tip_hash, tip_height) VALUES ('unspent', ?, ?, ?, ?)"
            " ON CONFLICT DO UPDATE SET tip_hash = excluded.tip_hash, tip_height = excluded.tip_height",
            (outpoint.txid, outpoint.vout, at.blockhash, at.height),
        )


def unspent_at(conn: sqlite3.Connection, outpoint: Outpoint) -> Tip | None:
    """The tip at which the output was last seen unspent: a snapshot, stale once the tip moves."""
    row = conn.execute(
        "SELECT tip_hash, tip_height FROM snapshot WHERE kind = 'unspent' AND txid = ? AND vout = ?",
        (outpoint.txid, outpoint.vout),
    ).fetchone()
    return None if row is None else Tip(str(row[0]), int(row[1]))


def put_activity(conn: sqlite3.Connection, events: Iterable[Activity], at: Tip) -> None:
    """Record receive and spend events, all or none."""
    rows = []
    for e in events:
        if e.kind not in ("receive", "spend") or (e.kind == "spend") != (e.prevout is not None):
            raise ValueError("a spend, and only a spend, names the output it spent")
        rows.append(
            (
                e.script_hex,
                e.kind,
                e.txid,
                e.n,
                e.sats,
                None if e.prevout is None else e.prevout.txid,
                None if e.prevout is None else e.prevout.vout,
                e.blockhash,
                e.height,
            )
        )
    with transaction(conn):
        for row in rows:
            _at(conn, at, row[8], row[7])
            known = conn.execute(
                "SELECT script_hex, kind, txid, n, sats, prevout_txid, prevout_vout, blockhash, height"
                " FROM activity WHERE kind = ? AND txid = ? AND n = ? AND blockhash = ?",
                (row[1], row[2], row[3], row[7]),
            ).fetchone()
            if known is not None:
                if tuple(known) != row:
                    raise DbError("a recorded activity event can't change (T-207)")
                continue
            conn.execute(
                "INSERT INTO activity (script_hex, kind, txid, n, sats, prevout_txid, prevout_vout,"
                " blockhash, height) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )


def activity_for(conn: sqlite3.Connection, script_hex: str) -> list[Activity]:
    """The recorded events for a script, by height. Within a block the order is fixed (kind, txid,
    index) but is not the order of the transactions in the block."""
    rows = conn.execute(
        "SELECT kind, txid, n, sats, prevout_txid, prevout_vout, blockhash, height FROM activity"
        " WHERE script_hex = ? ORDER BY height, kind, txid, n",
        (script_hex,),
    ).fetchall()
    return [
        Activity(
            "spend" if r[0] == "spend" else "receive",
            script_hex,
            str(r[1]),
            int(r[2]),
            int(r[3]),
            str(r[6]),
            int(r[7]),
            None if r[4] is None else Outpoint(str(r[4]), int(r[5])),
        )
        for r in rows
    ]


def extend_coverage(conn: sqlite3.Connection, covered: Coverage, at: Tip) -> Coverage:
    """Add a scanned range. It must start where the subject's coverage stops (or be the first), so
    coverage stays one range with no gaps (T-210). Returns the subject's coverage now."""
    if not 0 <= covered.start_height <= covered.stop_height or not is_hash(covered.stop_hash):
        raise ValueError("a scanned range needs 0 <= start <= stop and its stop block's hash")
    if not covered.subject:
        raise ValueError("a scanned range needs a subject")
    with transaction(conn):
        _at(conn, at, covered.stop_height, covered.stop_hash)
        now = coverage(conn, covered.subject)
        if now is not None and covered.start_height != now.stop_height + 1:
            raise DbError("a scanned range must continue the subject's coverage without a gap (T-210)")
        start = covered.start_height if now is None else now.start_height
        conn.execute(
            "INSERT INTO coverage (subject, start_height, stop_height, stop_hash) VALUES (?, ?, ?, ?)"
            " ON CONFLICT DO UPDATE SET stop_height = excluded.stop_height, stop_hash = excluded.stop_hash",
            (covered.subject, start, covered.stop_height, covered.stop_hash),
        )
    return Coverage(covered.subject, start, covered.stop_height, covered.stop_hash)


def coverage(conn: sqlite3.Connection, subject: str) -> Coverage | None:
    row = conn.execute(
        "SELECT start_height, stop_height, stop_hash FROM coverage WHERE subject = ?", (subject,)
    ).fetchone()
    return None if row is None else Coverage(subject, int(row[0]), int(row[1]), str(row[2]))


def invalidate_above(conn: sqlite3.Connection, fork: Tip) -> Invalidated:
    """After a reorg: remove every row, snapshot and stretch of coverage above the fork point, at any
    depth, all in one transaction (T-207). The fork block is on both chains, so coverage that reaches
    past it now stops there. The fork must be found by walking back from the reference tip.

    The last-seen tip only ever moves back here: to the fork, if the fork is below it. A fork above
    it (inside an unfinished catch-up) becomes the scan target instead, so the catch-up still counts
    as unfinished and is retried."""
    with transaction(conn):
        ref = reference_tip(conn)
        if ref is None or fork.height > ref.height or (fork.height == ref.height and fork != ref):
            raise DbError("a fork point must be the reference tip or below it (T-207)")
        f = fork.height
        txids: set[str] = set()
        for (txid,) in conn.execute("SELECT txid FROM tx_cache WHERE height > ?", (f,)):
            txids.add(str(txid))
        for txid, prevout_txid in conn.execute(
            "SELECT txid, prevout_txid FROM activity WHERE height > ?", (f,)
        ):
            txids.update(str(t) for t in (txid, prevout_txid) if t is not None)
        for txid, spend_txid in conn.execute("SELECT txid, spend_txid FROM spender WHERE height > ?", (f,)):
            txids.update((str(txid), str(spend_txid)))
        unspent = frozenset(
            Outpoint(str(t), int(v))
            for t, v in conn.execute("SELECT txid, vout FROM snapshot WHERE tip_height > ?", (f,))
        )
        subjects = frozenset(
            str(s) for (s,) in conn.execute("SELECT subject FROM coverage WHERE stop_height > ?", (f,))
        )
        for table in ("tx_cache", "activity", "spender"):
            conn.execute(f"DELETE FROM {table} WHERE height > ?", (f,))  # noqa: S608 (fixed names)
        conn.execute("DELETE FROM snapshot WHERE tip_height > ?", (f,))
        conn.execute("DELETE FROM coverage WHERE start_height > ?", (f,))
        conn.execute(
            "UPDATE coverage SET stop_height = ?, stop_hash = ? WHERE stop_height > ?",
            (f, fork.blockhash, f),
        )
        last = last_tip(conn)
        if last is None or fork.height > last.height:
            _write_target(conn, fork)  # below the old target: it moves back, by design
        else:
            set_tip(conn, fork)
            conn.execute("UPDATE chain_state SET target_hash = NULL, target_height = NULL WHERE id = 1")
    return Invalidated(fork, frozenset(txids), unspent, subjects)


def _encode(tx: Tx) -> str:
    return json.dumps(
        {
            "time": tx.block_time,
            "in": [
                [
                    None if i.prevout is None else i.prevout.txid,
                    None if i.prevout is None else i.prevout.vout,
                    i.sequence,
                    None if i.spent is None else _encode_out(i.spent),
                ]
                for i in tx.inputs
            ],
            "out": [_encode_out(o) for o in tx.outputs],
        },
        separators=(",", ":"),
    )


def _encode_out(o: TxOut) -> list[object]:
    return [o.sats, o.script_hex, o.script_type, o.address]


def _decode(txid: str, blockhash: str, confirmations: int, data: str) -> Tx:
    """Rebuild a cached transaction, refusing anything the encoder couldn't have written (T-408)."""
    try:
        d = json.loads(data)
        if not isinstance(d, dict) or set(d) != {"time", "in", "out"}:
            raise TypeError
        time = d["time"]
        if time is not None and (type(time) is not int or time < 0):
            raise TypeError
        inputs = tuple(_decode_in(i) for i in d["in"])
        outputs = tuple(_decode_out(o) for o in d["out"])
        tx = Tx(txid, blockhash, confirmations, time, inputs, outputs)
        # What the node's parser guarantees (chain/txs.py, T-502) holds for a cached copy too.
        if not inputs or not outputs or sum(o.sats for o in outputs) > MAX_SATS:
            raise ValueError
        if sum(i.spent.sats for i in inputs if i.spent is not None) > MAX_SATS:
            raise ValueError
        fee = tx.fee_sats
        if fee is not None and fee < 0:
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise DbError("a cached transaction is damaged (T-408); remove the user DB's cache") from None
    return tx


def _decode_in(i: object) -> TxIn:
    if not isinstance(i, list) or len(i) != 4:
        raise TypeError
    prev_txid, prev_vout, sequence, spent = i
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise TypeError
    if prev_txid is None and prev_vout is None:
        prevout = None
    elif is_hash(prev_txid) and type(prev_vout) is int and prev_vout <= MAX_SEQUENCE:
        prevout = Outpoint(prev_txid, prev_vout)
    else:
        raise TypeError
    return TxIn(prevout, sequence, None if spent is None else _decode_out(spent))


def _decode_out(o: object) -> TxOut:
    if not isinstance(o, list) or len(o) != 4:
        raise TypeError
    sats, script_hex, script_type, address = o
    if type(sats) is not int or not 0 <= sats <= MAX_SATS:
        raise TypeError
    if not is_hex(script_hex) or not isinstance(script_type, str) or not script_type:
        raise TypeError
    if address is not None and not isinstance(address, str):
        raise TypeError
    return TxOut(sats, script_hex, script_type, address)

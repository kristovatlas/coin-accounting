"""The user's events and lot identifications (PLAN §2 `event`, `identification`; THREAT_MODEL T-408, T-501,
T-508).

Every add, edit and removal is written together with a `change_log` row, in one transaction
(`migrations/m0011_events`), so the log can't miss an edit or record one that didn't happen. The values
are checked here as the schema can't: USD as exact cents (`domain.money`), moments as timezone-aware
UTC, dates as dates. Whether an event makes sense for its account (a deposit into an exchange account,
say) is the engine's and `services/`'s; the schema refuses what it can (a movement without a destination,
a gift without its donor's date).

- `add_event`, `update_event` and `remove_event` keep an event; a removed event's identification goes
  with it, both logged.
- `identify` replaces an event's chosen lots whole (an empty choice removes it), with when the user chose
  them (T-508). A choice is checked as the engine takes it: the event's sats exactly, or a movement's less
  its fee (ADR 0043), each lot once, never on a withdrawal with sats its account has no lots for. The
  engine's ids are the events' own ids as text, so a lot an event made or moved is named by that id as one
  segment of the lot id (moves stack: `<buy>@<deposit>@<withdrawal>`); an event other choices name can't
  be removed, or have what its lots depend on changed (kind, accounts, moment, sats, fee, missing basis),
  until they change. The guard is on named events only: a movement without a choice of its own moves
  lots by the standing method, and an edit elsewhere (an earlier purchase, say) can change which, so a
  later choice naming those moved lots can go stale. The engine then falls back to the standing method
  and warns (THREAT_MODEL 0.10.6); it never mis-reads the choice.
- `identification`'s method is always the user's specific choice: an event without one uses the standing
  method, or the FIFO fallback, which the engine tells apart (PLAN §2's `standing_order | fifo_default`
  are derived, not stored).
- `events`, `identification` and `identifications` read them back, events in the order of their
  moments. A stored choice is checked on read by `identify`'s rules (T-408), so a damaged one is
  refused with the remedy, never half-used.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

from coinacct.domain.chain import MAX_SATS, is_hash
from coinacct.domain.keys import refuse_private
from coinacct.domain.money import signed_usd, usd
from coinacct.storage.change_log import Origin, append
from coinacct.storage.db import transaction

type EventKind = Literal[
    "buy",
    "p2p_buy",
    "income",
    "gift_in",
    "inherit",
    "self_transfer",
    "deposit",
    "withdrawal",
    "sell",
    "spend",
    "gift_out",
]
ACQUISITIONS: Final = frozenset({"buy", "p2p_buy", "income", "gift_in", "inherit"})
MOVEMENTS: Final = frozenset({"self_transfer", "deposit", "withdrawal"})
DISPOSALS: Final = frozenset({"sell", "spend", "gift_out"})
GENESIS: Final = date(2009, 1, 3)  # no bitcoin was acquired before the genesis block
# PLAN §2's kinds the schema lists but the module refuses until their steps (migration 11)
LATER_KINDS: Final = frozenset({"opening_allocation_2025", "fee"})
MAX_LOT: Final = 400  # a lot id's length
MAX_CHOICES: Final = 1000  # lots in one choice
# the engine's lot ids: event ids and its separators (lot@transfer, lot@tx#vout, @@unrecorded, @@placeholder)
_LOT: Final = re.compile(r"[0-9A-Za-z_.:@#-]+", re.ASCII)
_COLUMNS: Final = (
    "kind, tax_account_id, to_account_id, at, sats, fee_sats, usd, valuation_source, txid, fmv,"
    " donor_acquired, donor_always_long, missing_basis, missing_acquired, note"
)
_NAMES: Final = tuple(c.strip() for c in _COLUMNS.split(","))
# built once from the constant column list above, never from input
_INSERT: Final = f"INSERT INTO event ({_COLUMNS}) VALUES ({', '.join('?' * len(_NAMES))})"  # noqa: S608
_UPDATE: Final = f"UPDATE event SET {', '.join(f'{c} = ?' for c in _NAMES)} WHERE id = ?"  # noqa: S608
_SELECT_ALL: Final = f"SELECT id, {_COLUMNS} FROM event ORDER BY at, id"  # noqa: S608
_SELECT_ONE: Final = f"SELECT {_COLUMNS} FROM event WHERE id = ?"  # noqa: S608


_NAMED: Final = "other events' choices name this event's lots: change them first"


class EventError(ValueError):
    """An event or identification the user DB can't hold. The message names the field, never a value."""


class EventNotFound(EventError):
    """No stored event has that id."""


@dataclass(frozen=True, slots=True)
class Event:
    """An event as the user entered it. `usd` is an acquisition's basis or FMV (None: a gift's unknown
    donor basis) or a disposal's proceeds net of costs (which can be negative); a movement has none."""

    kind: EventKind
    account: int
    at: datetime
    sats: int
    usd: Decimal | None = None
    to: int | None = None  # a movement's destination account
    # of `sats`, the BTC fee (ADR 0043): a buy's, withheld from the sats bought; a movement's, of the sats
    # that leave. A sale's, spend's or gift's BTC fee (ADR 0043's gross sats) waits for the engine to take
    # it, so it is refused; a fee in dollars is in a sale's `usd`, net of costs.
    fee_sats: int = 0
    valuation_source: str | None = None
    txid: str | None = None
    fmv: Decimal | None = None  # gift_in: the FMV at the gift
    donor_acquired: date | None = None  # gift_in: the donor's date
    donor_always_long: bool = False  # gift_in: the donor's lot was always long-term (inherited)
    missing_basis: Decimal | None = None  # withdrawal: the basis of sats the account holds no lots for
    missing_acquired: date | None = None  # withdrawal: and their date
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Stored:
    id: int
    event: Event


@dataclass(frozen=True, slots=True)
class Choice:
    lot: str  # the engine's lot id
    sats: int


def add_event(conn: sqlite3.Connection, event: Event, *, at: str, origin: Origin = "user") -> int:
    """Store `event`; its id."""
    row = _row(_checked(event))
    _log_time(at)
    with _refusals(), transaction(conn):
        cur = conn.execute(_INSERT, row)
        event_id = cur.lastrowid
        if event_id is None:  # pragma: no cover - SQLite always sets it for an INSERT
            raise EventError("the event wasn't stored")
        append(conn, at, ("event", str(event_id), None, _state(event)), origin)
    return event_id


def update_event(
    conn: sqlite3.Connection, event_id: int, event: Event, *, at: str, origin: Origin = "user"
) -> None:
    """Replace stored event `event_id` with `event`. The same values again are not logged. Refused, until
    the choices concerned change: an edit its own choice of lots no longer fits, or that changes its
    account or moment while it has one; and, while other events' choices name its lots, an edit to what
    those lots depend on (`_lot_shape`)."""
    row = _row(_checked(event))
    _log_time(at)
    with _refusals(), transaction(conn):
        before = _read(conn, event_id)
        if before == event:
            return
        chosen, _ = identification(conn, event_id)
        if chosen:
            _check_choice(event, chosen)
            if (event.account, event.at) != (before.account, before.at):  # its lots are another account's
                raise EventError(
                    "an event with a choice of lots keeps its account and moment: clear it first"
                )
        moves_its_lots = _lot_shape(event) != _lot_shape(before)
        if moves_its_lots and _named_by_others(conn, event_id):
            raise EventError(_NAMED)
        conn.execute(_UPDATE, (*row, event_id))
        append(conn, at, ("event", str(event_id), _state(before), _state(event)), origin)


def remove_event(conn: sqlite3.Connection, event_id: int, *, at: str, origin: Origin = "user") -> None:
    """Remove stored event `event_id`, and its identification first, each logged; refused while other
    events' choices name its lots."""
    _log_time(at)
    with _refusals(), transaction(conn):
        before = _read(conn, event_id)
        if _named_by_others(conn, event_id):
            raise EventError(_NAMED)
        _replace_choice(conn, event_id, (), None, at=at, origin=origin)
        conn.execute("DELETE FROM event WHERE id = ?", (event_id,))
        append(conn, at, ("event", str(event_id), _state(before), None), origin)


def identify(  # noqa: PLR0913 - the event, its choices, when they were made, and the log's time and origin
    conn: sqlite3.Connection,
    event_id: int,
    choices: tuple[Choice, ...],
    identified_at: datetime | None,
    *,
    at: str,
    origin: Origin = "user",
) -> None:
    """The user's choice of lots for disposal or movement `event_id`, in order, made at `identified_at`
    (timezone-aware UTC, no later than `at`, when it is recorded); an empty choice removes it (the
    standing method then applies). A choice covers the event's sats, or a movement's sats less its fee
    (the fee then takes the standing method's next sats, ADR 0043), each lot once."""
    if not isinstance(choices, tuple) or not all(isinstance(c, Choice) for c in choices):
        raise EventError("a choice is a tuple of Choices")
    for c in choices:
        if not isinstance(c.lot, str) or not 1 <= len(c.lot) <= MAX_LOT or _LOT.fullmatch(c.lot) is None:
            raise EventError("a choice names a lot by the engine's id")
        refuse_private(c.lot)  # no key in any text the DB keeps (T-703)
        _check_sats(c.sats, "a choice's sats")
    if len(choices) > MAX_CHOICES:
        raise EventError(f"a choice names at most {MAX_CHOICES} lots")
    if len({c.lot for c in choices}) != len(choices):
        raise EventError("a choice names each lot once")
    if choices and identified_at is None:
        raise EventError("a choice of lots names when it was made")
    stamp = None if identified_at is None else _moment(identified_at, "when the lots were chosen")
    recorded = _log_time(at)
    if identified_at is not None:
        if identified_at.astimezone(UTC).date() < GENESIS:
            raise EventError("lots are chosen no earlier than the genesis block")
        # at the log's precision: a choice made within the second it is recorded in isn't later
        if identified_at.replace(microsecond=0) > recorded:
            raise EventError("lots can't be chosen later than the choice is recorded")
    with _refusals(), transaction(conn):
        event = _read(conn, event_id)
        if choices:
            _check_choice(event, choices)
            _check_lots_exist(conn, choices)
        before, before_at = identification(conn, event_id)
        # a movement's choice names the lots it moves, which later choices can name in turn; the same
        # lots in another order move the same lots (the engine takes picks in FIFO order)
        changed = sorted((c.lot, c.sats) for c in choices) != sorted((c.lot, c.sats) for c in before)
        if identified_at is not None and before_at is not None and identified_at < before_at:
            # a choice is made after the one it replaces: never re-dated earlier, so a late choice can't
            # be made to look timely (T-508)
            raise EventError("a choice of lots can't be dated before the one it replaces")
        if event.kind in MOVEMENTS and changed and _named_by_others(conn, event_id):
            raise EventError(_NAMED)
        _replace_choice(conn, event_id, choices, stamp, at=at, origin=origin, old=(before, before_at))


def _log_time(at: str) -> datetime:
    """The change log's time for a write: a timezone-aware ISO 8601 moment, 20 to 40 characters."""
    if not isinstance(at, str) or not 20 <= len(at) <= 40:
        raise EventError("the log's time is an ISO 8601 moment")
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        raise EventError("the log's time is an ISO 8601 moment") from None
    if moment.utcoffset() is None:
        raise EventError("the log's time is timezone-aware")
    return moment


def _check_lots_exist(conn: sqlite3.Connection, choices: tuple[Choice, ...]) -> None:
    """The event each chosen lot starts from (`_origin`) is stored, so a choice can't come to name an event
    added after it (ids are given out in order)."""
    named = {o for c in choices if (o := _origin(c.lot)) is not None}
    if not named:
        return
    marks = ", ".join("?" * len(named))
    found = conn.execute(f"SELECT count(*) FROM event WHERE id IN ({marks})", tuple(named)).fetchone()[0]  # noqa: S608
    if found != len(named):
        raise EventError("a choice names a lot of an event that isn't stored")


def _lot_shape(e: Event) -> tuple[object, ...]:
    """What an event's lots, or the parts it moves, depend on: which lots exist, where, from when, and how
    many sats (a buy's fee comes off the sats credited, ADR 0043)."""
    return (e.kind, e.account, e.to, e.at, e.sats, e.fee_sats, e.missing_basis, e.missing_acquired)


def _check_choice(event: Event, choices: tuple[Choice, ...]) -> None:
    """A choice of lots fits `event` as the engine takes it (`tax.engine`'s picks)."""
    if event.kind not in MOVEMENTS | DISPOSALS:
        raise EventError("only a disposal or a movement chooses lots")
    if event.missing_basis is not None:
        raise EventError("a withdrawal with sats its account has no lots for chooses no lots")
    total = sum(c.sats for c in choices)
    arriving = event.kind in MOVEMENTS and event.fee_sats and total == event.sats - event.fee_sats
    if total != event.sats and not arriving:
        raise EventError("the chosen lots cover the event's sats (a movement's: or those less its fee)")


def _named_by_others(conn: sqlite3.Connection, event_id: int) -> bool:
    """Whether another event's choice names a lot `event_id` made or moved: its id as any segment of the
    engine's lot id (`<id>`, `<id>@…`, `…@<id>`, `…@<id>@…`, `…@<id>#<vout>`), as `services/` names lots."""
    lots = conn.execute("SELECT lot FROM identification WHERE event_id != ?", (event_id,))
    return any(event_id in _events_named(lot) for (lot,) in lots)


_EVENT_ID: Final = re.compile(r"[1-9][0-9]{0,18}")


def _origin(lot: str) -> int | None:
    """The stored event a lot id starts from, if it starts from one: its first segment, all digits, written
    as SQLite stores an id (no leading zero, at most 19 digits), so one event has one spelling. Lot ids
    in a coin-traced wallet start from a coin instead (ADR 0041), and name no event here."""
    first = re.split(r"[@#]", lot, maxsplit=1)[0]
    if not first.isdigit():
        return None
    if not _EVENT_ID.fullmatch(first) or int(first) > 2**63 - 1:
        raise EventError("a choice names an event by its id, with no leading zero")
    return int(first)


def _events_named(lot: str) -> set[int]:
    """Every event id a lot id may name, as any `@` segment before a `#<vout>`: the lot's origin and the
    movements it went through (`<id>@<move>…`). A later all-digit segment can also be a part's index (an
    opening allocation's `<id>@<n>`), so this errs towards protecting an event, never away from it."""
    parts = lot.split("#", 1)[0].split("@")
    return {int(p) for p in parts if _EVENT_ID.fullmatch(p) and int(p) <= 2**63 - 1}


def events(conn: sqlite3.Connection) -> list[Stored]:
    """Every stored event, in the order of their moments (then ids)."""
    rows = conn.execute(_SELECT_ALL).fetchall()
    return [Stored(r[0], _event(r[1:])) for r in rows]


def identification(conn: sqlite3.Connection, event_id: int) -> tuple[tuple[Choice, ...], datetime | None]:
    """Event `event_id`'s chosen lots, in order, and when they were chosen; `((), None)` if none."""
    rows = conn.execute(
        "SELECT position, lot, sats, identified_at, method, late FROM identification"
        " WHERE event_id = ? ORDER BY position",
        (event_id,),
    ).fetchall()
    if not rows:
        return (), None
    with _damaged():
        event = _read(conn, event_id)
        choice = _stored_choice(event, rows)
        _check_lots_exist(conn, choice[0])
        return choice


@contextmanager
def _refusals() -> Iterator[None]:
    """The schema's refusals (an account that doesn't exist, a rule broken) as an `EventError` with a fixed
    message: SQLite's text names the constraint and can carry values."""
    try:
        yield
    except sqlite3.IntegrityError:
        raise EventError(
            "the user DB refused the event: an account that doesn't exist, or a rule it breaks"
        ) from None


def identifications(
    conn: sqlite3.Connection, stored: Sequence[Stored] | None = None
) -> dict[int, tuple[tuple[Choice, ...], datetime]]:
    """Every stored choice of lots, by event id, in one query: its lots in order, and when it was made.
    Each is checked as `identify` would write it (T-408): a damaged one is refused, never half-used.
    `stored` must be every event, read in the same snapshot (`events`): a choice of an event missing
    from it is refused as damage. Without it they are read here, which needs a read transaction."""
    rows: dict[int, list[tuple[Any, ...]]] = {}
    for event_id, *row in conn.execute(
        "SELECT event_id, position, lot, sats, identified_at, method, late FROM identification"
        " ORDER BY event_id, position"
    ):
        rows.setdefault(event_id, []).append(tuple(row))
    if not rows:
        return {}
    known = {s.id: s.event for s in (events(conn) if stored is None else stored)}
    with _damaged():
        out = {}
        for event_id, picks in rows.items():
            if event_id not in known:
                raise EventError("a stored choice is damaged")
            out[event_id] = choice = _stored_choice(known[event_id], picks)
            if any((o := _origin(c.lot)) is not None and o not in known for c in choice[0]):
                raise EventError("a stored choice is damaged")  # a lot of an event that isn't stored
        return out


def _stored_choice(event: Event, rows: Sequence[tuple[Any, ...]]) -> tuple[tuple[Choice, ...], datetime]:
    """One stored choice, held to `identify`'s rules: lots by the engine's id, positions 0 to n-1, each
    lot once, none holding a key, the user's specific choice not yet judged late (the only method and
    flag the module writes), whole sats, at most `MAX_CHOICES`, one UTC moment from genesis on, and
    a fit for `event` (`_check_choice`). The caller checks that each lot starts from a stored event when
    it starts from one. A choice's time isn't checked against the change log, so a stored time moved
    earlier outside the module passes (#325)."""
    stamps = {chosen_at for _, _, _, chosen_at, _, _ in rows}
    if [p for p, *_ in rows] != list(range(len(rows))):
        raise EventError("a stored choice is damaged")
    # the module writes no other method, and no late flag until the services record the engine's
    # judgment: that change must widen this rule in the same PR
    if any(method != "specific" or late != 0 for *_, method, late in rows):
        raise EventError("a stored choice is damaged")
    if len(stamps) != 1 or len(rows) > MAX_CHOICES:
        raise EventError("a stored choice is damaged")
    (stamp,) = stamps
    chosen_at = datetime.fromisoformat(stamp)
    if chosen_at.utcoffset() != timedelta(0) or _moment(chosen_at, "a choice") != stamp:
        raise EventError("a stored choice is damaged")
    if chosen_at.date() < GENESIS:
        raise EventError("a stored choice is damaged")
    choices = []
    for _, lot, sats, *_ in rows:
        if not isinstance(lot, str) or not 1 <= len(lot) <= MAX_LOT or _LOT.fullmatch(lot) is None:
            raise EventError("a stored choice is damaged")
        refuse_private(lot)  # a key planted in the DB never reaches the ledger's output (T-703)
        _check_sats(sats, "a choice's sats")
        choices.append(Choice(lot, sats))
    if len({c.lot for c in choices}) != len(choices):
        raise EventError("a stored choice is damaged")
    for c in choices:
        _origin(c.lot)  # an event id is written without a leading zero
    _check_choice(event, tuple(choices))
    return tuple(choices), chosen_at


def _replace_choice(  # noqa: PLR0913 - as identify's
    conn: sqlite3.Connection,
    event_id: int,
    choices: tuple[Choice, ...],
    stamp: str | None,
    *,
    at: str,
    origin: Origin,
    old: tuple[tuple[Choice, ...], datetime | None] | None = None,
) -> None:
    """Replace event `event_id`'s stored choice, logged; `old` is the stored one, if the caller has
    read it already in this transaction."""
    old_choices, old_at = identification(conn, event_id) if old is None else old
    before = None
    if old_choices and old_at is not None:
        before = {"choices": [asdict(c) for c in old_choices], "identified_at": old_at.isoformat()}
    after = None if not choices else {"choices": [asdict(c) for c in choices], "identified_at": stamp}
    if before == after:
        return
    conn.execute("DELETE FROM identification WHERE event_id = ?", (event_id,))
    conn.executemany(
        "INSERT INTO identification (event_id, position, lot, sats, identified_at) VALUES (?, ?, ?, ?, ?)",
        [(event_id, i, c.lot, c.sats, stamp) for i, c in enumerate(choices)],
    )
    append(conn, at, ("identification", str(event_id), before, after), origin)


def _read(conn: sqlite3.Connection, event_id: int) -> Event:
    if isinstance(event_id, bool) or not isinstance(event_id, int):
        raise EventError("an event id is an integer")
    row = conn.execute(_SELECT_ONE, (event_id,)).fetchone()
    if row is None:
        raise EventNotFound("no such event")
    return _event(row)


def _check_fee(e: Event) -> None:
    if isinstance(e.fee_sats, bool) or not isinstance(e.fee_sats, int) or not 0 <= e.fee_sats < e.sats:
        raise EventError("the fee is whole sats, fewer than the event's")
    if e.fee_sats and e.kind in DISPOSALS:
        raise EventError("a BTC fee on a sale, spend or gift isn't supported yet: enter it as its own event")
    if e.fee_sats and e.kind in ACQUISITIONS - {"buy"}:
        raise EventError("only a movement or a buy bears a BTC fee")


def _checked(e: Event) -> Event:  # noqa: PLR0912 - one check per field
    if not isinstance(e, Event):
        raise EventError("an event is an Event")
    if e.kind in LATER_KINDS:
        raise EventError("an opening allocation, or a fee on its own line, isn't supported yet")
    if e.kind not in ACQUISITIONS | MOVEMENTS | DISPOSALS:
        raise EventError("unknown kind of event")
    if isinstance(e.account, bool) or not isinstance(e.account, int):
        raise EventError("the account is an account id")
    if e.to is not None and (isinstance(e.to, bool) or not isinstance(e.to, int)):
        raise EventError("the destination is an account id")
    if (e.kind in MOVEMENTS) != (e.to is not None):
        raise EventError("a movement, and only a movement, has a destination")
    _moment(e.at, "the moment")
    if e.at.astimezone(UTC).date() < GENESIS:
        raise EventError("the moment is no earlier than the genesis block")
    _check_sats(e.sats, "the sats")
    _check_fee(e)
    if e.to is not None and e.to == e.account:
        raise EventError("a movement goes to another account")
    if e.kind in MOVEMENTS:
        if e.usd is not None:
            raise EventError("a movement has no USD amount")
    elif e.kind in DISPOSALS:
        _amount(e.usd, "the proceeds", signed=True)
        if e.kind == "gift_out" and e.usd != 0:
            raise EventError("a gift given has no proceeds (0.00)")
    elif e.usd is not None or e.kind != "gift_in":
        _amount(e.usd, "the basis", signed=False)
    gift = e.kind == "gift_in"
    if gift != (e.fmv is not None and e.donor_acquired is not None) or (
        not gift and (e.fmv is not None or e.donor_acquired is not None or e.donor_always_long)
    ):
        raise EventError("a gift received, and only a gift, names its FMV and the donor's date")
    if gift:
        _amount(e.fmv, "the FMV at the gift", signed=False)
        if type(e.donor_acquired) is not date or not GENESIS <= e.donor_acquired <= _day(e.at):
            raise EventError("the donor's date is a date from the genesis block to the gift")
    if type(e.donor_always_long) is not bool:
        raise EventError("donor_always_long is True or False")
    if (e.missing_basis is None) != (e.missing_acquired is None) or (
        e.missing_basis is not None and e.kind != "withdrawal"
    ):
        raise EventError("only a withdrawal names a missing basis, with its date")
    if e.missing_basis is not None:
        _amount(e.missing_basis, "the missing basis", signed=False)
        if type(e.missing_acquired) is not date or not GENESIS <= e.missing_acquired <= _day(e.at):
            raise EventError("the missing basis's date is a date from the genesis block to the withdrawal")
    if e.txid is not None and not is_hash(e.txid):
        raise EventError("a txid is 64 lowercase hex digits")
    for name, text, limit in (("valuation source", e.valuation_source, 200), ("note", e.note, 2000)):
        if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= limit):
            raise EventError(f"the {name} is text of 1 to {limit} characters")
        if text is not None:
            refuse_private(text)  # no key in free text, whoever calls (T-703)
    return e


def _row(e: Event) -> tuple[Any, ...]:
    return (
        e.kind,
        e.account,
        e.to,
        e.at.astimezone(UTC).isoformat(),
        e.sats,
        e.fee_sats,
        None if e.usd is None else str(signed_usd(e.usd)),
        e.valuation_source,
        e.txid,
        None if e.fmv is None else str(signed_usd(e.fmv)),
        None if e.donor_acquired is None else e.donor_acquired.isoformat(),
        int(e.donor_always_long),
        None if e.missing_basis is None else str(signed_usd(e.missing_basis)),
        None if e.missing_acquired is None else e.missing_acquired.isoformat(),
        e.note,
    )


@contextmanager
def _damaged() -> Iterator[None]:
    """A stored row that can't be read, or that the module wouldn't write, is refused, never half-read."""
    try:
        yield
    except (ValueError, TypeError, ArithmeticError, EventError):
        raise EventError("a stored event is damaged (T-408): restore the user DB from a backup") from None


def _event(r: tuple[Any, ...]) -> Event:
    """A stored row as an `Event`, checked as a write would be: a sub-cent amount, a moment not in UTC or
    any rule the schema doesn't hold is refused as damage (T-408)."""
    with _damaged():
        e = _decoded(r)
        if e.at.utcoffset() != timedelta(0) or _row(_checked(e)) != tuple(r[: len(_row(e))]):
            raise EventError("damaged")
        return e


def _decoded(r: tuple[Any, ...]) -> Event:
    def money(v: str | None) -> Decimal | None:
        return None if v is None else Decimal(v)

    def day(v: str | None) -> date | None:
        return None if v is None else date.fromisoformat(v)

    return Event(
        kind=r[0],
        account=r[1],
        to=r[2],
        at=datetime.fromisoformat(r[3]),
        sats=r[4],
        fee_sats=r[5],
        usd=money(r[6]),
        valuation_source=r[7],
        txid=r[8],
        fmv=money(r[9]),
        donor_acquired=day(r[10]),
        donor_always_long=bool(r[11]),
        missing_basis=money(r[12]),
        missing_acquired=day(r[13]),
        note=r[14],
    )


def _state(e: Event) -> dict[str, Any]:
    """What the change log records of an event: every field, as text where it isn't a number."""
    row = _row(e)
    return dict(zip(_NAMES, row, strict=True))


def _amount(value: object, what: str, *, signed: bool) -> None:
    if not isinstance(value, Decimal):
        raise EventError(f"{what} is a USD amount")
    try:
        (signed_usd if signed else usd)(value)
    except (TypeError, ValueError):
        raise EventError(f"{what} is a USD amount in exact cents") from None


def _check_sats(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_SATS:
        raise EventError(f"{what} are whole sats, more than none and no more than the supply")


def _day(moment: datetime) -> date:
    """The latest tax date a moment can have: its UTC date plus a day (time zones run to UTC+14)."""
    day = moment.astimezone(UTC).date()
    return day if day == date.max else day + timedelta(days=1)


def _moment(value: object, what: str) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EventError(f"{what} is a timezone-aware moment")
    try:
        return value.astimezone(UTC).isoformat()
    except OverflowError:  # a moment at the very edge of the calendar
        raise EventError(f"{what} is a moment within the calendar") from None


__all__ = [
    "ACQUISITIONS",
    "DISPOSALS",
    "GENESIS",
    "MAX_LOT",
    "MOVEMENTS",
    "Choice",
    "Event",
    "EventError",
    "EventKind",
    "EventNotFound",
    "Stored",
    "add_event",
    "events",
    "identification",
    "identifications",
    "identify",
    "remove_event",
    "update_event",
]

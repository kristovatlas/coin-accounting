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
  until they change.
- `identification`'s method is always the user's specific choice: an event without one uses the standing
  method, or the FIFO fallback, which the engine tells apart (PLAN §2's `standing_order | fifo_default`
  are derived, not stored).
- `events`, `identification` and `identifications` read them back, events in the order of their
  moments.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
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
MAX_LOT: Final = 400  # a lot id's length
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
    # of `sats`, the fee (ADR 0043): a buy's, withheld from the sats bought; a movement's, of the sats that
    # leave; a disposal's, of the sats disposed of, its basis joining the disposal's
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
    """Replace stored event `event_id` with `event`. The same values again are not logged. An edit its
    choice of lots no longer fits, or one that changes the account, kind or sats of a purchase other
    events' choices name, is refused: change those choices first."""
    row = _row(_checked(event))
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
            raise EventError("other events' choices name this event's lots: change them first")
        conn.execute(_UPDATE, (*row, event_id))
        append(conn, at, ("event", str(event_id), _state(before), _state(event)), origin)


def remove_event(conn: sqlite3.Connection, event_id: int, *, at: str, origin: Origin = "user") -> None:
    """Remove stored event `event_id`, and its identification first, each logged; refused while other
    events' choices name its lots."""
    with _refusals(), transaction(conn):
        before = _read(conn, event_id)
        if _named_by_others(conn, event_id):
            raise EventError("other events' choices name this event's lots: change them first")
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
    if len({c.lot for c in choices}) != len(choices):
        raise EventError("a choice names each lot once")
    if choices and identified_at is None:
        raise EventError("a choice of lots names when it was made")
    stamp = None if identified_at is None else _moment(identified_at, "when the lots were chosen")
    try:
        recorded = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        raise EventError("the log's time is an ISO 8601 moment") from None
    if recorded.utcoffset() is None:
        raise EventError("the log's time is timezone-aware")
    if identified_at is not None and identified_at > recorded:
        raise EventError("lots can't be chosen later than the choice is recorded")
    with _refusals(), transaction(conn):
        event = _read(conn, event_id)
        if choices:
            _check_choice(event, choices)
        _replace_choice(conn, event_id, choices, stamp, at=at, origin=origin)


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
    name = re.compile(rf"(?:^|@){event_id}(?:$|[@#])")
    lots = conn.execute("SELECT lot FROM identification WHERE event_id != ?", (event_id,))
    return any(name.search(lot) for (lot,) in lots)


def events(conn: sqlite3.Connection) -> list[Stored]:
    """Every stored event, in the order of their moments (then ids)."""
    rows = conn.execute(_SELECT_ALL).fetchall()
    return [Stored(r[0], _event(r[1:])) for r in rows]


def identification(conn: sqlite3.Connection, event_id: int) -> tuple[tuple[Choice, ...], datetime | None]:
    """Event `event_id`'s chosen lots, in order, and when they were chosen; `((), None)` if none."""
    rows = conn.execute(
        "SELECT lot, sats, identified_at FROM identification WHERE event_id = ? ORDER BY position",
        (event_id,),
    ).fetchall()
    if not rows:
        return (), None
    return tuple(Choice(lot, sats) for lot, sats, _ in rows), datetime.fromisoformat(rows[0][2])


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


def identifications(conn: sqlite3.Connection) -> dict[int, tuple[tuple[Choice, ...], datetime]]:
    """Every stored choice of lots, by event id, in one query: its lots in order, and when it was made."""
    out: dict[int, tuple[list[Choice], datetime]] = {}
    for event_id, lot, sats, chosen_at in conn.execute(
        "SELECT event_id, lot, sats, identified_at FROM identification ORDER BY event_id, position"
    ):
        out.setdefault(event_id, ([], datetime.fromisoformat(chosen_at)))[0].append(Choice(lot, sats))
    return {k: (tuple(v[0]), v[1]) for k, v in out.items()}


def _replace_choice(  # noqa: PLR0913 - as identify's
    conn: sqlite3.Connection,
    event_id: int,
    choices: tuple[Choice, ...],
    stamp: str | None,
    *,
    at: str,
    origin: Origin,
) -> None:
    old, old_at = identification(conn, event_id)
    before = None
    if old and old_at is not None:
        before = {"choices": [asdict(c) for c in old], "identified_at": old_at.isoformat()}
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


def _checked(e: Event) -> Event:  # noqa: PLR0912 - one check per field
    if not isinstance(e, Event):
        raise EventError("an event is an Event")
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
    if isinstance(e.fee_sats, bool) or not isinstance(e.fee_sats, int) or not 0 <= e.fee_sats < e.sats:
        raise EventError("the fee is whole sats, fewer than the event's")
    if e.fee_sats and e.kind in ACQUISITIONS - {"buy"}:
        raise EventError("only a movement, a disposal or a buy bears a fee")
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


def _event(r: tuple[Any, ...]) -> Event:
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
    return moment.astimezone(UTC).date() + timedelta(days=1)


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

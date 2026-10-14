"""The user's events and lot choices, for the API (PLAN §2, §7; THREAT_MODEL T-408, T-508, T-703).

Every change goes through `storage.events`, which writes it with its change-log row in one transaction.
The time recorded is the app's clock, in UTC, and so is when a choice of lots was made: the app records
it (ADR 0008 §3), so a late choice can't be made to look timely (T-508). An unknown event is `NotFound`.
A refusal is `ImportRefused` with the storage layer's fixed message, which names the field and never the
value (T-403); a busy DB is `Busy`; a private key in free text is refused before it reaches the DB
(T-703).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from coinacct.domain.keys import PrivateKeyError
from coinacct.services.graph import NotFound
from coinacct.services.imports import ImportRefused, db_errors
from coinacct.storage import accounts, events
from coinacct.storage.db import Connection, transaction
from coinacct.storage.events import Choice, Event, EventError, EventNotFound, Stored


def _now() -> str:
    return _second_up(datetime.datetime.now(datetime.UTC)).isoformat(timespec="seconds")


def _second_up(moment: datetime.datetime) -> datetime.datetime:
    """`moment` rounded up to its next whole second: a choice of lots made within a sale's second is
    then never recorded before it, so the late check (T-508) can only err towards warning."""
    whole = moment.replace(microsecond=0)
    return whole if whole == moment else whole + datetime.timedelta(seconds=1)


__all__ = ["Choice", "Event", "Events", "Stored"]  # the value types the API builds and reads


class Events:
    """What the API's event routes call."""

    def __init__(
        self, writer: Connection, open_reader: Callable[[], Connection], now: Callable[[], str] = _now
    ) -> None:
        self._writer, self._open_reader, self._now = writer, open_reader, now

    def list(self) -> list[tuple[Stored, tuple[tuple[Choice, ...], datetime.datetime | None]]]:
        """Every event in the order of its moment, with its choice of lots, from one snapshot."""
        with db_errors():
            reader = self._open_reader()
            try:
                reader.execute("BEGIN")  # one snapshot for both reads (architecture §3)
                stored = events.events(reader)
                chosen = events.identifications(reader)
                reader.execute("COMMIT")
            finally:
                reader.close()
        return [(s, chosen.get(s.id, ((), None))) for s in stored]

    def add(self, event: Event) -> int:
        with _refused(), transaction(self._writer):  # the check and the write, under the writer's lock
            event_id = events.add_event(self._writer, event, at=self._now())
            self._check_direction(event)  # a refusal rolls the write back
            return event_id

    def update(self, event_id: int, event: Event) -> None:
        with _refused(), transaction(self._writer):
            events.update_event(self._writer, event_id, event, at=self._now())  # an unknown event: 404
            self._check_direction(event)  # a refusal rolls the write back

    def _check_direction(self, event: Event) -> None:
        """A movement goes the way its kind says (PLAN §7): a deposit from a self-custody account into a
        custodial one, a withdrawal out of a custodial one (into a wallet, or another exchange account),
        a self-transfer between self-custody ones. Called after the write, inside its transaction, so the
        storage layer's refusals (an unknown event or account) come first."""
        if event.to is None or event.kind not in events.MOVEMENTS:
            return  # the storage layer refuses a destination on anything else
        kinds = {a.id: a.kind for a in accounts.tax_accounts(self._writer)}
        source, target = kinds.get(event.account), kinds.get(event.to)
        if source is None or target is None:
            return  # the storage layer refuses an account that doesn't exist
        expected = {
            "deposit": ("self_custody", "custodial"),
            "withdrawal": ("custodial", target),  # into a wallet, or straight into another exchange
            "self_transfer": ("self_custody", "self_custody"),
        }[event.kind]
        if (source, target) != expected:
            raise ImportRefused(
                "a deposit goes from a wallet into an exchange account, a withdrawal out of an exchange"
                " account, and a self-transfer between wallets"
            )

    def remove(self, event_id: int) -> None:
        with _refused():
            events.remove_event(self._writer, event_id, at=self._now())

    def identify(self, event_id: int, choices: tuple[Choice, ...]) -> None:
        """The user's choice of lots, made now: the app records when (ADR 0008 §3, T-508)."""
        now = self._now()
        made = datetime.datetime.fromisoformat(now) if choices else None
        with _refused():
            events.identify(self._writer, event_id, choices, made, at=now)


@contextmanager
def _refused() -> Iterator[None]:
    with db_errors():
        try:
            yield
        except EventNotFound:
            raise NotFound from None
        except EventError as e:
            raise ImportRefused(str(e)) from None
        except PrivateKeyError:
            raise ImportRefused("the text holds a private key, so nothing was saved") from None

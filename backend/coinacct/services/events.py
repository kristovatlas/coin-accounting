"""The user's events and lot choices, for the API (PLAN §2, §7; THREAT_MODEL T-408, T-508, T-703).

Every change goes through `storage.events`, which writes it with its change-log row in one transaction.
The time recorded is the app's clock, in UTC. A refusal is `ImportRefused` with the storage layer's
fixed message, which names the field and never the value (T-403); a busy DB is `Busy`; a private key in
free text is refused before it reaches the DB (T-703).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from coinacct.services.imports import ImportRefused, db_errors
from coinacct.storage import events
from coinacct.storage.db import Connection
from coinacct.storage.events import Choice, Event, EventError, Stored


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


__all__ = ["Choice", "Event", "Events", "Stored"]  # the value types the API builds and reads


class Events:
    """What the API's event routes call."""

    def __init__(
        self, writer: Connection, open_reader: Callable[[], Connection], now: Callable[[], str] = _now
    ) -> None:
        self._writer, self._open_reader, self._now = writer, open_reader, now

    def list(self) -> list[tuple[Stored, tuple[tuple[Choice, ...], datetime.datetime | None]]]:
        """Every event in the order of its moment, with its choice of lots."""
        with db_errors():
            reader = self._open_reader()
            try:
                return [(s, events.identification(reader, s.id)) for s in events.events(reader)]
            finally:
                reader.close()

    def add(self, event: Event) -> int:
        with _refused():
            return events.add_event(self._writer, event, at=self._now())

    def update(self, event_id: int, event: Event) -> None:
        with _refused():
            events.update_event(self._writer, event_id, event, at=self._now())

    def remove(self, event_id: int) -> None:
        with _refused():
            events.remove_event(self._writer, event_id, at=self._now())

    def identify(
        self, event_id: int, choices: tuple[Choice, ...], identified_at: datetime.datetime | None
    ) -> None:
        with _refused():
            events.identify(self._writer, event_id, choices, identified_at, at=self._now())


@contextmanager
def _refused() -> Iterator[None]:
    with db_errors():
        try:
            yield
        except EventError as e:
            raise ImportRefused(str(e)) from None

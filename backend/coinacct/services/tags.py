"""Tagging from the graph: whose an address is, and whether a transaction is a mix (PLAN §4, M3's
"tagging side panel" and "mixing flag"; THREAT_MODEL T-408, T-504, T-703).

Every change goes through `storage.tags`, which writes it with its change-log row in one transaction.
The time recorded is the app's clock, in UTC. A newly tagged address is scanned like an imported one:
the chain jobs are asked for a sync (`services.imports.Imports.request_sync`).

Errors map as for the import routes: the schema's refusals are `ImportRefused` with a fixed message,
a busy DB is `Busy`, and a private key in a label or address text is refused before it reaches the DB.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence

from coinacct.services.imports import Imports, db_errors
from coinacct.storage import tags
from coinacct.storage.db import Connection
from coinacct.storage.tags import Change


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class Tagging:
    """What the API's tag routes call."""

    def __init__(
        self,
        writer: Connection,
        open_reader: Callable[[], Connection],
        imports: Imports,
        now: Callable[[], str] = _now,
    ) -> None:
        self._writer, self._open_reader, self._imports, self._now = writer, open_reader, imports, now

    def tag_address(  # noqa: PLR0913 - the script and the tag's fields
        self,
        script_hex: str,
        *,
        text: str | None,
        entity_id: int,
        tax_account_id: int | None,
        label: str,
        client_ids: Sequence[int],
    ) -> bool:
        """Set whose `script_hex` is; True if it was new to the user DB (and is now being scanned)."""
        with db_errors():
            new = tags.tag_address(
                self._writer,
                script_hex,
                text=text,
                entity_id=entity_id,
                tax_account_id=tax_account_id,
                label=label,
                client_ids=client_ids,
                at=self._now(),
            )
        if new:
            self._imports.request_sync()
        return new

    def set_mixing(self, txid: str, mixing: bool) -> None:
        with db_errors():
            tags.set_mixing(self._writer, txid, mixing, at=self._now())

    def changes(self, subject: str) -> list[Change]:
        """One address's or transaction's change log, oldest first."""
        with db_errors():
            reader = self._open_reader()
            try:
                return tags.changes(reader, subject)
            finally:
                reader.close()

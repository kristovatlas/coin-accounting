"""Tagging from the graph: whose an address is, and whether a transaction is a mix (PLAN §4, M3's
"tagging side panel" and "mixing flag"; THREAT_MODEL T-408, T-504, T-703).

Every change goes through `storage.tags`, which writes it with its change-log row in one transaction.
The time recorded is the app's clock, in UTC. A newly tagged address is scanned like an imported one:
the chain jobs are asked for a sync (`services.imports.Imports.request_sync`).

Errors map as for the import routes: the schema's refusals are `ImportRefused` with a fixed message,
a busy DB is `Busy`, and a private key in a label or address text is refused before it reaches the DB.
An address text must be an address of the recorded chain that pays to the script it is stored with,
as for an import; its normal form is stored, replacing any text stored before.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence

from coinacct.domain.addresses import AddressError, parse_address
from coinacct.domain.keys import refuse_private
from coinacct.services.imports import ImportRefused, Imports, db_errors
from coinacct.storage import tags
from coinacct.storage.chain_state import recorded_chain
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
            if text is not None:
                text = self._address_text(script_hex, text)
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

    def _address_text(self, script_hex: str, text: str) -> str:
        refuse_private(text)  # before parsing, so a key gets the key refusal (T-703)
        reader = self._open_reader()
        try:
            chain = recorded_chain(reader)
        finally:
            reader.close()
        if chain is None:
            raise ImportRefused("no chain is recorded yet: connect to the node once before tagging")
        try:
            address = parse_address(text, chain)
        except AddressError:
            raise ImportRefused("that isn't an address of this chain") from None
        if address.script_hex != script_hex:
            raise ImportRefused("that address doesn't pay to that script")
        return address.text

    def set_mixing(self, txid: str, mixing: bool) -> None:
        with db_errors():
            tags.set_mixing(self._writer, txid, mixing, at=self._now())

    def changes(self, kind: str, subject: str) -> list[Change]:
        """One address's (`address_tag`) or transaction's (`tx_flag`) change log, oldest first."""
        with db_errors():
            reader = self._open_reader()
            try:
                return tags.changes(reader, (kind, subject))
            finally:
                reader.close()

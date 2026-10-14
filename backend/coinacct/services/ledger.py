"""The user's ledger through the lot engine (PLAN §7, M6): the stored events as the engine's input, and the
run (THREAT_MODEL T-501, T-507, T-508).

- **Ids.** An engine event's id is the stored event's id as text, and its account the tax account's id as
  text, so a lot an acquisition makes is named by that id, and a part of it moved on by `<id>@…`, as the
  stored choices of lots name them (`storage.events`).
- **Tax dates.** An event's tax date is the date of its moment in the user's time zone (PLAN §7), the
  `time_zone` setting (`storage.settings`), read in the same snapshot as the events. Events go to the
  engine in the order of their moments, which keeps their tax dates in order too.
- **Fees.** A buy's fee was withheld from the sats bought (ADR 0043): its lot holds the sats credited, the
  sats less the fee, at the full cash paid. A movement's fee is the engine's `fee_sats`.
- **Choices.** An event's stored choice of lots becomes its picks, with when it was made; an event
  without one uses its account's standing method. So does every event of an account in automatic mode
  (ADR 0021 §1, the default): the app selects its lots by the standing method, a stored choice is not
  used, and nothing there is late.
- **Methods.** An account's standing order of FIFO is passed on; a standing order of specific
  identification, or none, leaves FIFO, the engine's only standing method and the statutory fallback.

The engine refuses input it can't use (`EngineError`); the run then stops, and the caller shows why.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from coinacct.services.imports import ImportRefused, db_errors
from coinacct.storage import accounts, events, settings
from coinacct.storage.db import Connection
from coinacct.storage.events import ACQUISITIONS, DISPOSALS, Choice, Stored
from coinacct.tax import engine
from coinacct.tax.engine import Acquisition, Disposal, Event, Method, Pick, Result, Transfer

type Chosen = Mapping[int, tuple[tuple[Choice, ...], datetime]]


def engine_events(
    stored: Sequence[Stored], chosen: Chosen, zone: ZoneInfo, automatic: frozenset[int] = frozenset()
) -> list[Event]:
    """The engine's input for `stored` (in the order of their moments), with their choices of lots. An
    event of an account in `automatic` mode takes the standing method, whatever choice is stored."""
    out: list[Event] = []
    for s in sorted(stored, key=lambda s: (s.event.at, s.id)):
        e = s.event
        on = e.at.astimezone(zone).date()
        ident, account = str(s.id), str(e.account)
        choice = None if e.account in automatic else chosen.get(s.id)
        picks = None if choice is None else tuple(Pick(c.lot, c.sats) for c in choice[0])
        identified_at = None if choice is None else choice[1]
        if e.kind in ACQUISITIONS:
            out.append(
                Acquisition(
                    ident,
                    account,
                    on,
                    e.kind,  # type: ignore[arg-type]  # one of the engine's acquisition kinds
                    e.sats - e.fee_sats,
                    e.usd,
                    e.fmv,
                    e.donor_acquired,
                    e.donor_always_long,
                )
            )
        elif e.kind in DISPOSALS:
            if e.usd is None:  # pragma: no cover - the storage layer requires the proceeds
                raise engine.EngineError(f"event {ident!r}: a disposal names its proceeds")
            out.append(Disposal(ident, account, on, e.at, e.kind, e.sats, e.usd, picks, identified_at))  # type: ignore[arg-type]
        else:
            out.append(
                Transfer(
                    ident,
                    account,
                    str(e.to),
                    on,
                    e.at,
                    e.kind,  # type: ignore[arg-type]  # one of the engine's transfer kinds
                    e.sats,
                    e.fee_sats,
                    picks,
                    identified_at,
                    e.missing_basis,
                    e.missing_acquired,
                )
            )
    return out


def automatic_accounts(tax_accounts: Sequence[accounts.TaxAccount]) -> frozenset[int]:
    """The accounts whose lots the standing method selects automatically (ADR 0021 §1): automatic mode
    with a standing order of FIFO, or none (the FIFO fallback). Automatic mode over a standing order
    of specific identification selects nothing by itself, so the stored choices stand there."""
    return frozenset(a.id for a in tax_accounts if a.automatic and a.standing_method in ("fifo", None))


def standing_methods(tax_accounts: Sequence[accounts.TaxAccount]) -> dict[str, Method]:
    """The engine's standing method of each account that has a FIFO order on file."""
    return {str(a.id): "fifo" for a in tax_accounts if a.standing_method == "fifo"}


class Ledger:
    """The engine's run of everything stored, for the API's ledger and report routes."""

    def __init__(self, open_reader: Callable[[], Connection]) -> None:
        self._open_reader = open_reader

    def run(self) -> tuple[list[Event], Result]:
        """The engine's input, from one snapshot of the user DB, and its run."""
        with db_errors():
            reader = self._open_reader()
            try:
                reader.execute("BEGIN")
                stored = events.events(reader)
                chosen = events.identifications(reader)
                tax_accounts = accounts.tax_accounts(reader)
                methods = standing_methods(tax_accounts)
                automatic = automatic_accounts(tax_accounts)
                try:
                    zone = settings.time_zone(reader)
                except settings.SettingsError as e:  # its fixed message names the remedy (T-408)
                    raise ImportRefused(str(e)) from None
                reader.execute("COMMIT")
            finally:
                reader.close()
        inputs = engine_events(stored, chosen, zone, automatic)
        return inputs, engine.run(inputs, methods)

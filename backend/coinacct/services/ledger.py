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
  over a FIFO standing order or none (ADR 0021 §1, the default): the app selects its lots by the
  standing method, a stored choice is not used, and nothing there is late. A choice that went stale,
  naming a lot the run doesn't make, more sats than that lot holds, or lots of an account the run
  leaves without any (an edit elsewhere moved other lots, or, until #313, the choice named another
  account's lot), takes the standing method too: the engine refuses it, and the run drops that choice
  and runs again. A withdrawal's stored basis for unrecorded sats is the event's own data, not a
  choice, so an account that has lots after an edit still refuses it, naming the event. So does a
  transfer's choice that leaves too few other sats for its fee: its lots are still held, and the
  standing method for the whole transfer would move other lots (ADR 0043). A choice naming a lot the
  run does make stands. The run lists both apart: automatic mode's unused choices, and the stale
  ones, which a report shows with a warning, since the user's identification wasn't used.
- **Methods.** An account's standing order of FIFO is passed on; a standing order of specific
  identification, or none, leaves FIFO, the engine's only standing method and the statutory fallback.

The engine refuses input it can't use (`EngineError`); the run then stops with a fixed message naming
the event when the refusal names one (`refused`), which the API shows as a 422.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from coinacct.services.imports import ImportRefused, db_errors
from coinacct.storage import accounts, events, settings
from coinacct.storage.db import Connection
from coinacct.storage.events import ACQUISITIONS, DISPOSALS, Choice, Stored
from coinacct.tax import engine
from coinacct.tax.engine import (
    Acquisition,
    Allocation,
    Disposal,
    Event,
    GiftGiven,
    Method,
    MissingLots,
    Moved,
    Pick,
    Result,
    Transfer,
    UnknownBasis,
)

# the engine's result types are re-exported for the API, which imports only services (architecture §2)
__all__ = [
    "Allocation",
    "GiftGiven",
    "Ledger",
    "MissingLots",
    "Moved",
    "Run",
    "UnknownBasis",
    "automatic_accounts",
    "engine_events",
    "ignored_choices",
    "refused",
    "standing_methods",
]

type Chosen = Mapping[int, tuple[tuple[Choice, ...], datetime]]


def ignored_choices(stored: Sequence[Stored], chosen: Chosen, automatic: frozenset[int]) -> frozenset[int]:
    """The events whose stored choice the run doesn't use from the start: those of accounts in
    automatic mode (ADR 0021 §1)."""
    account = {s.id: s.event.account for s in stored}
    return frozenset(i for i in chosen if account.get(i) in automatic)


# the engine's refusal of a stored choice naming a lot the run doesn't make, more sats than a lot holds
# (`tax.engine` `_checked_picks`), or lots in an account the run leaves without any
# (`_unrecorded_if_needed`): the choice went stale after an edit elsewhere, or, until a choice is
# checked when it is made (#313), named another account's lot. A transfer's choice that leaves too few
# other sats for its fee (`_with_fee`) is not stale: its lots are still held, and taking the standing
# method for all of it would move other lots than the user identified (ADR 0043), so that refusal
# stops the run, naming the event (#317)
_STALE: Final = re.compile(
    r"(?:event '(?P<event>[1-9][0-9]{0,18})': "
    r"(?:lot '[0-9A-Za-z_.:@#-]{1,400}' (?:isn't held in account '[1-9][0-9]{0,18}'"
    r"|holds fewer sats than chosen))"
    r"|transfer '(?P<transfer>[1-9][0-9]{0,18})': an account without lots has none to choose)",
    re.ASCII,
)
# the event an engine refusal names, as `str(id)` of a stored event (`engine_events`)
_REFUSED: Final = re.compile(r"(?:event|acquisition|disposal|transfer|gift) '([1-9][0-9]{0,18})'", re.ASCII)


def _stale_choice(error: engine.EngineError, chosen: Chosen, ignored: set[int]) -> int | None:
    """The event whose stored choice `error` refuses as stale, if it is one the run can ignore."""
    match = _STALE.fullmatch(str(error))
    event_id = None if match is None else int(match.group("event") or match.group("transfer"))
    return event_id if event_id in chosen and event_id not in ignored else None


def refused(error: engine.EngineError) -> ImportRefused:
    """The engine's refusal as a fixed message naming only the event's id: the engine's own text can
    repeat stored lot ids and kinds (T-403)."""
    match = _REFUSED.match(str(error))
    if match is None:
        return ImportRefused(
            "the lot engine can't use the stored events: check them, or restore from a backup"
        )
    return ImportRefused(
        f"the lot engine can't use event {int(match.group(1))} as stored: check it, its accounts and its lots"
    )


def engine_events(
    stored: Sequence[Stored], chosen: Chosen, zone: ZoneInfo, ignored: frozenset[int] = frozenset()
) -> list[Event]:
    """The engine's input for `stored` (in the order of their moments), with their choices of lots. An
    event in `ignored` (`ignored_choices`) takes its account's standing method, whatever choice is stored."""
    out: list[Event] = []
    for s in sorted(stored, key=lambda s: (s.event.at, s.id)):
        e = s.event
        on = e.at.astimezone(zone).date()
        ident, account = str(s.id), str(e.account)
        choice = None if s.id in ignored else chosen.get(s.id)
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


@dataclass(frozen=True, slots=True)
class Run:
    inputs: list[Event]
    result: Result
    automatic: tuple[int, ...]  # events whose stored choice wasn't used: automatic mode (ADR 0021 §1)
    stale: tuple[int, ...]  # events whose stored choice went stale and took the standing method: warn

    @property
    def ignored(self) -> tuple[int, ...]:
        """Every event whose stored choice of lots wasn't used."""
        return tuple(sorted(self.automatic + self.stale))


class Ledger:
    """The engine's run of everything stored, for the API's ledger and report routes."""

    def __init__(self, open_reader: Callable[[], Connection]) -> None:
        self._open_reader = open_reader

    def run(self) -> Run:
        """The engine's input, from one snapshot of the user DB, its run, and the events whose stored
        choice wasn't used. A damaged stored row or setting is refused with its remedy (T-408)."""
        with db_errors():
            reader = self._open_reader()
            try:
                reader.execute("BEGIN")
                try:
                    stored = events.events(reader)
                    chosen = events.identifications(reader, stored)  # checked against the same events
                except events.EventError as e:  # its fixed message names the remedy (T-408)
                    raise ImportRefused(str(e)) from None
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
        unused = ignored_choices(stored, chosen, automatic)
        ignored, stale = set(unused), set[int]()
        while True:  # at most once per stored choice; each run is a whole engine run, accepted
            try:
                inputs = engine_events(stored, chosen, zone, frozenset(ignored))
                result = engine.run(inputs, methods)
            except engine.EngineError as e:
                event_id = _stale_choice(e, chosen, ignored)
                if event_id is None:
                    raise refused(e) from None
                ignored.add(event_id)  # the stale choice takes the standing method, and is listed
                stale.add(event_id)
                continue
            return Run(inputs, result, tuple(sorted(unused)), tuple(sorted(stale)))

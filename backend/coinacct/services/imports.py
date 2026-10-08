"""Importing addresses and public descriptors (PLAN §3; ADR 0019; THREAT_MODEL T-203, T-701, T-703).

An import is two steps, so the user confirms what will be added (T-701: "the import preview must be
confirmed"):
1. a **preview** parses the upload and says what it holds: the addresses or derived scripts that are
   new, the ones already in the user DB with their current owner and account (so a conflict shows
   before the user confirms), and the lines that aren't addresses (by number, never repeated);
2. the **import** writes what the preview found. A known script under the same owner and account only
   gains the new wallet-client links; one under another owner or account is reported, never moved.

Every line passes `domain.keys.refuse_private` in exactly the form that is then parsed, stored and
sent to the node: the upload is split on newlines, surrounding whitespace is stripped, and nothing is
decoded afterwards (`domain.keys`' requirement on callers). Core's normal form of a descriptor is
checked again before it is derived and stored. A private key anywhere refuses the whole upload, and
the error never repeats it.

Descriptors need the node (`getdescriptorinfo`, `deriveaddresses`). Offline (`rpc` is None), a
descriptor preview is refused before anything is sent (T-203); address lists need no node.

After an import, the caller asks the chain jobs for a sync (`services.jobs.ChainJobs.request_sync`):
the tip poller otherwise queues one only when the tip moves.

`subjects` lists what the chain jobs scan (architecture §8.2). A subject's name is the key of its
coverage, so it names exactly what is scanned:
- a descriptor: a digest of its text, its window and its start height, so a wider window, an earlier
  start (a re-import) or a new descriptor that reuses a deleted one's id is a new subject, scanned
  from its start height, never a continuation of the old coverage (T-207, T-210);
- addresses no descriptor derives (or derives only from a later start height): batched into at
  most `ADDRESS_BUCKETS` subjects per start height, by a hash of each script, so a list of many
  addresses isn't one full scan each (PLAN §3). A bucket's name is a digest of its members, so adding
  an address rescans only its own bucket.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from coinacct.chain import descriptors, node_checks
from coinacct.chain.descriptors import DescriptorError, DescriptorInfo
from coinacct.chain.scans import Scan
from coinacct.chain.txs import ChainRpc, NodeError
from coinacct.domain.addresses import AddressError, parse_address
from coinacct.domain.keys import refuse_private
from coinacct.domain.secret import Secret
from coinacct.storage import accounts
from coinacct.storage.accounts import AccountsError, Source
from coinacct.storage.chain_state import recorded_chain
from coinacct.storage.db import Connection, DbBusy, DbError, transaction

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES: Final = 1_000_000
MAX_LINES: Final = 10_000
DEFAULT_GAP_LIMIT: Final = 20
MAX_GAP_LIMIT: Final = 1000
ADDRESS_BUCKETS: Final = 16


class ImportRefused(ValueError):
    """The upload can't be imported. The message never repeats its content (T-403, T-703)."""


class OfflineError(ImportRefused):
    """This import needs the node, and the app is in offline mode (T-203)."""


class Busy(Exception):
    """The user DB is busy (another thread's transaction held the writer's lock past its timeout)."""


@contextmanager
def _db_errors() -> Iterator[None]:
    """The storage layer's refusals as this module's, for the API:
    - a write the schema refused (`AccountsError`, a fixed message that never repeats the data) is
      `ImportRefused` with that message;
    - a busy DB (`DbBusy`) is `Busy`;
    - anything else (a reader that failed its file checks, T-401) is `ImportRefused` with a fixed
      message: those carry the DB's path, which stays in the log, never in a response."""
    try:
        yield
    except AccountsError as e:
        raise ImportRefused(str(e)) from None
    except DbBusy as e:
        raise Busy(str(e)) from None
    except DbError as e:
        log.warning("the user DB couldn't be used for an import or account request: %s", e)
        raise ImportRefused("the user DB can't be used right now; see the log") from None


@contextmanager
def _node_errors() -> Iterator[None]:
    """A node that went away, or refused the call outright, after start-up: the import can't run now.
    Node error text never reaches a message (`chain.descriptors`)."""
    try:
        yield
    except NodeError:
        raise OfflineError("the node didn't answer; check that it's running and try again") from None


@dataclass(frozen=True, slots=True)
class Known:
    """A script the user DB already holds, with its current owner and account."""

    script_hex: str
    address: str | None
    entity_id: int
    tax_account_id: int | None


@dataclass(frozen=True, slots=True)
class AddressPreview:
    new: tuple[tuple[str, str], ...]  # (script_hex, address) to add, in upload order
    known: tuple[Known, ...]  # already in the user DB
    repeated: int  # lines repeating an address earlier in the upload
    invalid_lines: tuple[int, ...]  # 1-based numbers of lines that aren't addresses of this chain


@dataclass(frozen=True, slots=True)
class DescriptorPreview:
    info: DescriptorInfo
    gap_limit: int
    derived: tuple[tuple[int, str, str], ...]  # (index, script_hex, address), from index 0
    already_imported: bool  # the same descriptor is in the user DB
    known: tuple[Known, ...]  # derived scripts already in the user DB


def _chain(conn: Connection) -> str:
    chain = recorded_chain(conn)
    if chain is None:
        raise ImportRefused("no chain is recorded yet: connect to the node once before importing")
    return chain


def _lines(upload: str) -> list[str]:
    try:
        size = len(upload.encode("utf-8"))
    except UnicodeEncodeError:
        raise ImportRefused("the upload isn't valid text") from None
    if size > MAX_UPLOAD_BYTES:
        raise ImportRefused(f"the upload is larger than {MAX_UPLOAD_BYTES} bytes")
    # Newlines only, so the reported line numbers match the user's editor.
    lines = upload.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()  # the final newline ends the last line; it doesn't start another
    if len(lines) > MAX_LINES:
        raise ImportRefused(f"the upload has more than {MAX_LINES} lines")
    stripped = [line.strip() for line in lines]
    for line in stripped:
        refuse_private(line)
    return stripped


def _known(conn: Connection, scripts: Sequence[str]) -> list[Known]:
    wanted = set(scripts)
    return [
        Known(a.script_hex, a.text, a.entity_id, a.tax_account_id)
        for a in accounts.addresses(conn)
        if a.script_hex in wanted
    ]


def preview_addresses(conn: Connection, upload: str) -> AddressPreview:
    """One address per line; blank lines are skipped."""
    chain = _chain(conn)
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    repeated, invalid = 0, []
    for number, line in enumerate(_lines(upload), start=1):
        if not line:
            continue
        try:
            address = parse_address(line, chain)
        except AddressError:
            invalid.append(number)
            continue
        if address.script_hex in seen:
            repeated += 1
            continue
        seen.add(address.script_hex)
        parsed.append((address.script_hex, address.text))
    known = _known(conn, [s for s, _ in parsed])
    known_scripts = {k.script_hex for k in known}
    new = tuple(p for p in parsed if p[0] not in known_scripts)
    return AddressPreview(new, tuple(known), repeated, tuple(invalid))


def import_addresses(  # noqa: PLR0913 - the preview and the owner, account and labels it gets
    conn: Connection,
    preview: AddressPreview,
    *,
    entity_id: int,
    tax_account_id: int | None,
    label: str = "",
    start_height: int = 0,
    client_ids: Sequence[int] = (),
    source: Source = "import",
) -> accounts.Added:
    """Write what `preview` found. Known scripts under the same owner and account gain the new
    client links; any under another owner or account (from the preview, or added since) are
    reported as conflicts, never moved."""
    rows = list(preview.new) + [(k.script_hex, k.address) for k in preview.known]
    try:
        return accounts.add_addresses(
            conn,
            rows,
            entity_id=entity_id,
            tax_account_id=tax_account_id,
            source=source,
            label=label,
            start_height=start_height,
            client_ids=client_ids,
        )
    except AccountsError as e:
        raise ImportRefused(str(e)) from None


def preview_descriptor(
    rpc: ChainRpc | None, conn: Connection, upload: str, *, gap_limit: int = DEFAULT_GAP_LIMIT
) -> DescriptorPreview:
    """One public descriptor. A ranged one is derived for its first window (indexes 0..gap_limit-1)."""
    if not 1 <= gap_limit <= MAX_GAP_LIMIT:
        raise ImportRefused(f"the gap limit is from 1 to {MAX_GAP_LIMIT}")
    chain = _chain(conn)
    lines = [line for line in _lines(upload) if line]
    if len(lines) != 1:
        raise ImportRefused("a descriptor import holds exactly one descriptor")
    if rpc is None:
        raise OfflineError("importing a descriptor needs the node; the app is in offline mode")
    try:
        info = descriptors.describe(rpc, lines[0])
        # Core's normal form is what is derived, stored and scanned: checked as such before use.
        refuse_private(info.text)
        if not info.is_solvable:
            raise ImportRefused("the descriptor isn't solvable: its scripts can't be derived")
        end = gap_limit - 1 if info.is_range else 0
        found = descriptors.derive(rpc, info, 0, end)
    except DescriptorError as e:
        raise ImportRefused(str(e)) from None
    derived: list[tuple[int, str, str]] = []
    for index, text in enumerate(found):
        try:
            derived.append((index, parse_address(text, chain).script_hex, text))
        except AddressError:
            raise ImportRefused("the node derived an address that isn't of this chain") from None
    if len({script for _, script, _ in derived}) != len(derived):
        raise ImportRefused("the descriptor derives the same script twice")
    already = any(d.text == info.text for d in accounts.descriptors(conn))
    known = _known(conn, [script for _, script, _ in derived])
    return DescriptorPreview(info, gap_limit if info.is_range else 1, tuple(derived), already, tuple(known))


def import_descriptor(  # noqa: PLR0913 - the preview and the owner, account and labels it gets
    conn: Connection,
    preview: DescriptorPreview,
    *,
    entity_id: int,
    tax_account_id: int | None,
    label: str = "",
    start_height: int = 0,
    client_ids: Sequence[int] = (),
) -> int:
    """Write the descriptor. Imported again under the same owner and account, it only gains the new
    client links; a derived script owned otherwise refuses the import."""
    try:
        return accounts.add_descriptor(
            conn,
            preview.info.text,
            preview.derived,
            entity_id=entity_id,
            tax_account_id=tax_account_id,
            gap_limit=preview.gap_limit,
            label=label,
            start_height=start_height,
            client_ids=client_ids,
        )
    except AccountsError as e:
        raise ImportRefused(str(e)) from None


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def descriptor_subject(d: accounts.Descriptor) -> str:
    """The scan subject of a descriptor's current window (see the module docstring)."""
    return f"desc:{_digest(d.text, str(d.range_end), str(d.start_height))}"


def subjects(conn: Connection) -> list[Scan]:
    """What the chain jobs scan (architecture §8.2): each descriptor over its derived window, and the
    addresses no descriptor derives, in buckets (see the module docstring for the names)."""
    scans: list[Scan] = []
    derived_from: dict[str, int] = {}  # script -> the earliest start height of a descriptor deriving it
    for d in accounts.descriptors(conn):
        for _, script in accounts.descriptor_scripts(conn, d.id):
            derived_from[script] = min(derived_from.get(script, d.start_height), d.start_height)
        ranged = "*" in d.text
        scanobject: object = {"desc": d.text, "range": [0, d.range_end]} if ranged else d.text
        scans.append(Scan(descriptor_subject(d), (scanobject,), start_height=d.start_height))
    buckets: dict[tuple[int, int], list[str]] = {}
    for a in accounts.addresses(conn):
        # Covered by a descriptor only from its start: an address imported with an earlier start
        # keeps its own subject, so its earlier history is scanned too.
        if a.script_hex not in derived_from or a.start_height < derived_from[a.script_hex]:
            bucket = int(_digest(a.script_hex), 16) % ADDRESS_BUCKETS
            buckets.setdefault((a.start_height, bucket), []).append(a.script_hex)
    for (start_height, _), scripts in sorted(buckets.items()):
        members = sorted(scripts)
        name = f"addrs:{start_height}:{_digest(*members)}"
        scans.append(Scan(name, tuple(f"raw({s})" for s in members), start_height=start_height))
    return scans


class Imports:
    """What the API's import and account routes call (architecture §2: `api/` reaches the DB and the
    node only through `services/`; §3: one writer, readers apart).

    - **Reads** (account lists, previews of address lists) use a reader of their own, opened and
      closed per call, on the calling thread.
    - **Writes** go through the writer. An address import parses the upload again and writes what it
      found in one writer transaction, so the import is exactly what a preview of that same upload
      shows at that moment; the user confirms the preview in the UI before the import is sent (T-701).
    - **Descriptors** need the node: `deriveaddresses` runs before the writer is touched, so the
      writer's lock is never held across an RPC call. Offline (`rpc` is None) they are refused.
    - **After an import,** the chain jobs are asked for a sync, so the new scripts are scanned without
      waiting for a block. The jobs start after the app is built: `jobs_started` connects them.
    """

    def __init__(
        self, writer: Connection, open_reader: Callable[[], Connection], rpc: ChainRpc | None
    ) -> None:
        self._writer, self._open_reader, self._rpc = writer, open_reader, rpc
        self._request_sync: Callable[[], None] = _nothing

    def jobs_started(self, request_sync: Callable[[], None]) -> None:
        self._request_sync = request_sync

    @property
    def online(self) -> bool:
        return self._rpc is not None

    def _read[T](self, read: Callable[[Connection], T]) -> T:
        """`read` on a reader of its own, in one snapshot (a deferred transaction: under WAL it takes
        no write lock), so related rows read together are consistent."""
        with _db_errors():
            reader = self._open_reader()
            try:
                reader.execute("BEGIN")
                try:
                    return read(reader)
                finally:
                    reader.execute("COMMIT")
            finally:
                reader.close()

    def overview(self) -> Overview:
        return self._read(
            lambda r: Overview(
                tuple(accounts.entities(r)), tuple(accounts.tax_accounts(r)), tuple(accounts.clients(r))
            )
        )

    def add_entity(self, name: str, kind: accounts.EntityKind) -> int:
        with _db_errors():
            return accounts.add_entity(self._writer, name, kind)

    def add_tax_account(self, name: str, kind: accounts.AccountKind, *, entity_id: int | None = None) -> int:
        with _db_errors():
            return accounts.add_tax_account(self._writer, name, kind, entity_id=entity_id)

    def add_client(self, name: str, kind: accounts.ClientKind) -> int:
        with _db_errors():
            return accounts.add_client(self._writer, name, kind)

    def preview_addresses(self, upload: str) -> AddressPreview:
        return self._read(lambda r: preview_addresses(r, upload))

    def import_addresses(self, upload: str, owner: Owner) -> accounts.Added:
        with _db_errors(), transaction(self._writer):
            preview = preview_addresses(self._writer, upload)
            result = import_addresses(
                self._writer,
                preview,
                entity_id=owner.entity_id,
                tax_account_id=owner.tax_account_id,
                label=owner.label,
                start_height=owner.start_height,
                client_ids=owner.client_ids,
            )
        # Any script that isn't a conflict: a new one, or a known one whose start height may have
        # gone down. A sync with nothing new to scan is cheap; a missed one leaves history unscanned.
        if len(preview.new) + len(preview.known) > len(result.conflicts):
            self._request_sync()
        return result

    def preview_descriptor(self, upload: str, gap_limit: int = DEFAULT_GAP_LIMIT) -> DescriptorPreview:
        with _node_errors():
            return self._read(lambda r: preview_descriptor(self._rpc, r, upload, gap_limit=gap_limit))

    def import_descriptor(self, upload: str, owner: Owner, gap_limit: int = DEFAULT_GAP_LIMIT) -> int:
        preview = self.preview_descriptor(upload, gap_limit)  # the node first, without the writer's lock
        with _db_errors():
            descriptor_id = import_descriptor(
                self._writer,
                preview,
                entity_id=owner.entity_id,
                tax_account_id=owner.tax_account_id,
                label=owner.label,
                start_height=owner.start_height,
                client_ids=owner.client_ids,
            )
        self._request_sync()
        return descriptor_id


def _nothing() -> None:
    pass


@dataclass(frozen=True, slots=True)
class Owner:
    """Who an import belongs to, and its labels (PLAN §3: an entity, a tax account, wallet clients)."""

    entity_id: int
    tax_account_id: int | None
    label: str = ""
    start_height: int = 0
    client_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Overview:
    entities: tuple[accounts.Entity, ...]
    tax_accounts: tuple[accounts.TaxAccount, ...]
    clients: tuple[accounts.WalletClient, ...]


def service(  # noqa: PLR0913, PLR0917 - the DB, its readers, and the node endpoint and credentials
    writer: Connection,
    open_reader: Callable[[], Connection],
    host: str,
    port: int,
    user: str,
    password: Secret,
    *,
    online: bool,
) -> Imports:
    """The `Imports` the API uses. Online, it gets its own node client (`chain.node_checks.connect`;
    the endpoint was checked to be loopback by `config`); offline, none, so descriptors are refused
    before anything is sent (T-203)."""
    rpc = node_checks.connect(host, port, user, password) if online else None
    return Imports(writer, open_reader, rpc)

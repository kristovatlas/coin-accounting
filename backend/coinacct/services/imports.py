"""Importing addresses and public descriptors (PLAN §3; ADR 0019; THREAT_MODEL T-203, T-701, T-703).

An import is two steps, so the user confirms what will be added (T-701: "the import preview must be
confirmed"):
1. a **preview** parses the upload and says what it holds: the addresses or derived scripts that will
   be added, the lines that aren't addresses (by number, never repeated), and the ones already known;
2. the **import** writes exactly what the preview found.

Every line, and a descriptor as a whole, passes `domain.keys.refuse_private` in exactly the form that
is then parsed, stored and sent to the node: surrounding whitespace is stripped first, nothing is
decoded afterwards (`domain.keys`' requirement on callers). A private key anywhere refuses the whole
upload, and the error never repeats it.

Descriptors need the node (`getdescriptorinfo`, `deriveaddresses`). Offline (`rpc` is None), a
descriptor preview is refused before anything is sent (T-203); address lists need no node.

`subjects` lists what the chain jobs scan: one subject per descriptor (its derived window) and one per
address that no descriptor derives.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from coinacct.chain import descriptors
from coinacct.chain.descriptors import DescriptorError, DescriptorInfo
from coinacct.chain.scans import Scan
from coinacct.chain.txs import ChainRpc
from coinacct.domain.addresses import AddressError, parse_address
from coinacct.domain.keys import refuse_private
from coinacct.storage import accounts
from coinacct.storage.accounts import Source
from coinacct.storage.chain_state import recorded_chain
from coinacct.storage.db import Connection

MAX_UPLOAD_BYTES: Final = 1_000_000
MAX_LINES: Final = 10_000
DEFAULT_GAP_LIMIT: Final = 20
MAX_GAP_LIMIT: Final = 1000


class ImportRefused(ValueError):
    """The upload can't be imported. The message never repeats its content (T-403, T-703)."""


class OfflineError(ImportRefused):
    """This import needs the node, and the app is in offline mode (T-203)."""


@dataclass(frozen=True, slots=True)
class AddressPreview:
    new: tuple[tuple[str, str], ...]  # (script_hex, address) to add, in upload order
    known: int  # already in the DB, or repeated in the upload
    invalid_lines: tuple[int, ...]  # 1-based numbers of lines that aren't addresses of this chain


@dataclass(frozen=True, slots=True)
class DescriptorPreview:
    info: DescriptorInfo
    gap_limit: int
    derived: tuple[tuple[int, str, str], ...]  # (index, script_hex, address), from index 0


def _chain(conn: Connection) -> str:
    chain = recorded_chain(conn)
    if chain is None:
        raise ImportRefused("no chain is recorded yet: connect to the node once before importing")
    return chain


def _lines(upload: str) -> list[str]:
    if len(upload.encode()) > MAX_UPLOAD_BYTES:
        raise ImportRefused(f"the upload is larger than {MAX_UPLOAD_BYTES} bytes")
    lines = upload.splitlines()
    if len(lines) > MAX_LINES:
        raise ImportRefused(f"the upload has more than {MAX_LINES} lines")
    stripped = [line.strip() for line in lines]
    for line in stripped:
        refuse_private(line)
    return stripped


def preview_addresses(conn: Connection, upload: str) -> AddressPreview:
    """One address per line; blank lines are skipped."""
    chain = _chain(conn)
    known_scripts = {a.script_hex for a in accounts.addresses(conn)}
    new: list[tuple[str, str]] = []
    seen: set[str] = set()
    known, invalid = 0, []
    for number, line in enumerate(_lines(upload), start=1):
        if not line:
            continue
        try:
            address = parse_address(line, chain)
        except AddressError:
            invalid.append(number)
            continue
        if address.script_hex in known_scripts or address.script_hex in seen:
            known += 1
            continue
        seen.add(address.script_hex)
        new.append((address.script_hex, address.text))
    return AddressPreview(tuple(new), known, tuple(invalid))


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
    """Write what `preview` found. A script added meanwhile under the same owner and account is
    skipped; one added under another is reported as a conflict, never moved."""
    return accounts.add_addresses(
        conn,
        preview.new,
        entity_id=entity_id,
        tax_account_id=tax_account_id,
        source=source,
        label=label,
        start_height=start_height,
        client_ids=client_ids,
    )


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
        if not info.is_solvable:
            raise ImportRefused("the descriptor isn't solvable: its scripts can't be derived")
        found = (
            descriptors.derive(rpc, info, 0, gap_limit - 1)
            if info.is_range
            else descriptors.derive(rpc, info, 0, 0)
        )
    except DescriptorError as e:
        raise ImportRefused(str(e)) from None
    refuse_private(info.text)  # Core's normal form is what gets stored and sent: checked as such
    derived: list[tuple[int, str, str]] = []
    for index, text in enumerate(found):
        try:
            derived.append((index, parse_address(text, chain).script_hex, text))
        except AddressError:
            raise ImportRefused("the node derived an address that isn't of this chain") from None
    if len({script for _, script, _ in derived}) != len(derived):
        raise ImportRefused("the descriptor derives the same script twice")
    return DescriptorPreview(info, gap_limit, tuple(derived))


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


def subjects(conn: Connection) -> list[Scan]:
    """What the chain jobs scan (architecture §8.2): each descriptor over its derived window, and each
    address that no descriptor derives, by its script (`raw()` covers every script type)."""
    scans: list[Scan] = []
    derived: set[str] = set()
    for d in accounts.descriptors(conn):
        derived.update(script for _, script in accounts.descriptor_scripts(conn, d.id))
        scanobject: object = {"desc": d.text, "range": [0, d.range_end]} if "*" in d.text else d.text
        scans.append(Scan(f"desc:{d.id}", (scanobject,), start_height=d.start_height))
    for a in accounts.addresses(conn):
        if a.script_hex not in derived:
            scans.append(Scan(f"addr:{a.script_hex}", (f"raw({a.script_hex})",), start_height=a.start_height))
    return scans

"""Importing addresses and public descriptors (PLAN §3; ADR 0019; THREAT_MODEL T-203, T-701, T-703).
Synthetic regtest addresses only; the descriptor path also runs against a real node in
`integration/services/test_imports_regtest.py`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coinacct.domain.keys import PrivateKeyError
from coinacct.rpc import RpcCallError
from coinacct.services import imports
from coinacct.services.imports import ImportRefused, OfflineError
from coinacct.storage import accounts as ac
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import record_chain
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def p2wpkh(n: int, hrp: str = "bcrt") -> tuple[str, str]:
    """A synthetic regtest P2WPKH address for program `n`, and its script (BIP173 encoding)."""
    program = n.to_bytes(20, "big")
    acc, bits, data = 0, 0, [0]
    for byte in program:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = _polymod(expanded + data + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum), "0014" + program.hex()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    record_chain(c, "regtest")
    yield c
    c.close()


@pytest.fixture
def wallet(conn: sqlite3.Connection) -> int:
    return ac.add_tax_account(conn, "Cold storage", "self_custody")


# --- Address lists -------------------------------------------------------------------------------


def test_an_address_list_is_previewed_then_imported_t701(conn: sqlite3.Connection, wallet: int) -> None:
    (a1, s1), (a2, s2) = p2wpkh(1), p2wpkh(2)
    upload = f"  {a1}  \n\nnot an address\n{a2}\n{a1}\n{p2wpkh(3, 'bc')[0]}\n"
    preview = imports.preview_addresses(conn, upload)
    assert preview.new == ((s1, a1), (s2, a2))
    assert preview.known == 1 and preview.invalid_lines == (3, 6)  # by number, never repeated
    result = imports.import_addresses(conn, preview, entity_id=ME, tax_account_id=wallet, label="cold")
    assert result.added == (s1, s2)
    again = imports.preview_addresses(conn, a1)
    assert again.new == () and again.known == 1


def test_a_private_key_anywhere_refuses_the_whole_upload_t703(conn: sqlite3.Connection) -> None:
    wif_shaped = ("K" + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")[:52]
    with pytest.raises(PrivateKeyError) as e:
        imports.preview_addresses(conn, f"{p2wpkh(1)[0]}\n  {wif_shaped}\n")
    assert wif_shaped not in str(e.value)


def test_an_upload_too_large_or_too_long_is_refused_t701(conn: sqlite3.Connection) -> None:
    with pytest.raises(ImportRefused, match="bytes"):
        imports.preview_addresses(conn, "x" * (imports.MAX_UPLOAD_BYTES + 1))
    with pytest.raises(ImportRefused, match="lines"):
        imports.preview_addresses(conn, "\n" * (imports.MAX_LINES + 1))


def test_nothing_is_imported_before_a_chain_is_recorded(tmp_path: Path) -> None:
    d = tmp_path / "fresh"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    with pytest.raises(ImportRefused, match="no chain"):
        imports.preview_addresses(c, p2wpkh(1)[0])
    c.close()


# --- Descriptors ---------------------------------------------------------------------------------

TPUB_DESC = "wpkh([d34db33f/84h/1h/0h]tpubexample/0/*)"


class Node:
    """getdescriptorinfo and deriveaddresses for one ranged descriptor."""

    def __init__(self, *, private: bool = False, refuse: bool = False, ranged: bool = True) -> None:
        self.private, self.refuse, self.ranged = private, refuse, ranged
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append((method, params))
        if self.refuse:
            raise RpcCallError(method, -5, f"key '{params[0]}' is not valid")
        if method == "getdescriptorinfo":
            return {
                "descriptor": params[0] + "#abcdefgh",
                "checksum": "abcdefgh",
                "isrange": self.ranged,
                "issolvable": True,
                "hasprivatekeys": self.private,
            }
        if method == "deriveaddresses":
            if not self.ranged:
                return [p2wpkh(1000)[0]]
            start, end = params[1]
            return [p2wpkh(1000 + i)[0] for i in range(start, end + 1)]
        raise AssertionError(method)


def test_a_descriptor_is_previewed_through_the_node_and_imported_with_its_window(
    conn: sqlite3.Connection, wallet: int
) -> None:
    node = Node()
    preview = imports.preview_descriptor(node, conn, f"  {TPUB_DESC}  \n", gap_limit=5)
    assert preview.info.text == TPUB_DESC + "#abcdefgh"
    assert [(i, s) for i, s, _ in preview.derived] == [(i, p2wpkh(1000 + i)[1]) for i in range(5)]
    assert ("deriveaddresses", [TPUB_DESC + "#abcdefgh", [0, 4]]) in node.calls
    d = imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=7)
    [desc] = ac.descriptors(conn)
    assert desc.id == d and desc.range_end == 4 and desc.gap_limit == 5 and desc.start_height == 7


def test_offline_a_descriptor_is_refused_before_anything_is_sent_t203(conn: sqlite3.Connection) -> None:
    with pytest.raises(OfflineError, match="offline"):
        imports.preview_descriptor(None, conn, TPUB_DESC)


def test_the_nodes_error_text_never_reaches_the_refusal_t403(conn: sqlite3.Connection) -> None:
    with pytest.raises(ImportRefused) as e:
        imports.preview_descriptor(Node(refuse=True), conn, TPUB_DESC)
    assert "tpubexample" not in str(e.value) and "can't read" in str(e.value)


def test_a_descriptor_the_node_says_is_private_is_refused_t703(conn: sqlite3.Connection) -> None:
    with pytest.raises(ImportRefused, match="private keys"):
        imports.preview_descriptor(Node(private=True), conn, TPUB_DESC)


@pytest.mark.parametrize(
    ("upload", "reason"),
    [
        (f"{TPUB_DESC}\n{TPUB_DESC}", "exactly one"),
        ("", "exactly one"),
        ("combo(tpubexample/0/*)", "combo"),
        ("wpkh(tpubexample/<0;1>/*)", "multipath"),
    ],
)
def test_an_upload_that_isnt_one_usable_descriptor_is_refused(
    conn: sqlite3.Connection, upload: str, reason: str
) -> None:
    with pytest.raises(ImportRefused, match=reason):
        imports.preview_descriptor(Node(), conn, upload)


def test_the_gap_limit_is_bounded(conn: sqlite3.Connection) -> None:
    for bad in (0, imports.MAX_GAP_LIMIT + 1):
        with pytest.raises(ImportRefused, match="gap limit"):
            imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=bad)


def test_an_unranged_descriptor_derives_one_script(conn: sqlite3.Connection, wallet: int) -> None:
    preview = imports.preview_descriptor(Node(ranged=False), conn, "wpkh(tpubexample/0/7)")
    assert [i for i, _, _ in preview.derived] == [0]


# --- Subjects ------------------------------------------------------------------------------------


def test_subjects_scan_each_descriptor_and_each_address_no_descriptor_derives(
    conn: sqlite3.Connection, wallet: int
) -> None:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=3)
    d = imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=7)
    lone_address, lone_script = p2wpkh(5)
    derived_address = p2wpkh(1001)[0]  # already the descriptor's
    addresses = imports.preview_addresses(conn, f"{lone_address}\n{derived_address}")
    imports.import_addresses(conn, addresses, entity_id=ME, tax_account_id=wallet, start_height=9)
    subjects = imports.subjects(conn)
    assert [s.subject for s in subjects] == [f"desc:{d}", f"addr:{lone_script}"]
    assert subjects[0].scanobjects == ({"desc": TPUB_DESC + "#abcdefgh", "range": [0, 2]},)
    assert subjects[0].start_height == 7
    assert subjects[1].scanobjects == (f"raw({lone_script})",) and subjects[1].start_height == 9


class Odd(Node):
    def __init__(self, *, solvable: bool = True, chain_hrp: str = "bcrt", repeat: bool = False) -> None:
        super().__init__()
        self.solvable, self.chain_hrp, self.repeat = solvable, chain_hrp, repeat

    def call(self, method: str, params: Any = ()) -> Any:
        reply = super().call(method, params)
        if method == "getdescriptorinfo":
            return {**reply, "issolvable": self.solvable}
        if self.repeat:
            return [p2wpkh(1000)[0]] * len(reply)
        return [p2wpkh(1000 + i, self.chain_hrp)[0] for i in range(len(reply))]


@pytest.mark.parametrize(
    ("node", "reason"),
    [
        (Odd(solvable=False), "solvable"),
        (Odd(chain_hrp="bc"), "isn't of this chain"),
        (Odd(repeat=True), "same script twice"),
    ],
)
def test_a_descriptor_whose_scripts_cant_be_trusted_is_refused(
    conn: sqlite3.Connection, node: Node, reason: str
) -> None:
    with pytest.raises(ImportRefused, match=reason):
        imports.preview_descriptor(node, conn, TPUB_DESC, gap_limit=3)

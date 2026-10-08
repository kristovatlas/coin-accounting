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
    assert preview.repeated == 1 and preview.known == ()
    assert preview.invalid_lines == (3, 6)  # by number, never repeated
    result = imports.import_addresses(conn, preview, entity_id=ME, tax_account_id=wallet, label="cold")
    assert result.added == (s1, s2)
    again = imports.preview_addresses(conn, a1)
    assert again.new == () and again.known == (imports.Known(s1, a1, ME, wallet),)


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


def test_subjects_scan_each_descriptor_and_the_lone_addresses_in_buckets(
    conn: sqlite3.Connection, wallet: int
) -> None:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=3)
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=7)
    lone_address, lone_script = p2wpkh(5)
    derived_address = p2wpkh(1001)[0]  # already the descriptor's
    addresses = imports.preview_addresses(conn, f"{lone_address}\n{derived_address}")
    imports.import_addresses(conn, addresses, entity_id=ME, tax_account_id=wallet, start_height=9)
    [desc, lone] = imports.subjects(conn)
    assert (
        desc.scanobjects == ({"desc": TPUB_DESC + "#abcdefgh", "range": [0, 2]},) and desc.start_height == 7
    )
    assert lone.scanobjects == (f"raw({lone_script})",) and lone.start_height == 9


def test_many_addresses_are_a_few_subjects(conn: sqlite3.Connection, wallet: int) -> None:
    upload = "\n".join(p2wpkh(n)[0] for n in range(1, 201))
    imports.import_addresses(
        conn, imports.preview_addresses(conn, upload), entity_id=ME, tax_account_id=wallet
    )
    subjects = imports.subjects(conn)
    assert len(subjects) <= imports.ADDRESS_BUCKETS
    assert sum(len(s.scanobjects) for s in subjects) == 200


def test_adding_an_address_renames_only_its_bucket(conn: sqlite3.Connection, wallet: int) -> None:
    upload = "\n".join(p2wpkh(n)[0] for n in range(1, 101))
    imports.import_addresses(
        conn, imports.preview_addresses(conn, upload), entity_id=ME, tax_account_id=wallet
    )
    before = {s.subject for s in imports.subjects(conn)}
    imports.import_addresses(
        conn, imports.preview_addresses(conn, p2wpkh(500)[0]), entity_id=ME, tax_account_id=wallet
    )
    after = {s.subject for s in imports.subjects(conn)}
    assert len(after - before) == 1 and len(before - after) == 1  # one bucket is a new subject


def test_a_wider_window_is_a_new_subject_scanned_from_its_start_t210(
    conn: sqlite3.Connection, wallet: int
) -> None:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=3)
    d = imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=7)
    [before] = imports.subjects(conn)
    ac.extend_descriptor(conn, d, [(3, p2wpkh(1003)[1], p2wpkh(1003)[0])])
    [after] = imports.subjects(conn)
    # Coverage is keyed by the subject's name: the wider window can't continue the narrower one's.
    assert after.subject != before.subject and after.start_height == 7
    assert after.scanobjects == ({"desc": TPUB_DESC + "#abcdefgh", "range": [0, 3]},)


def test_an_unranged_descriptor_is_scanned_as_itself(conn: sqlite3.Connection, wallet: int) -> None:
    preview = imports.preview_descriptor(Node(ranged=False), conn, "wpkh(tpubexample/0/7)")
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet)
    [subject] = imports.subjects(conn)
    assert subject.scanobjects == ("wpkh(tpubexample/0/7)#abcdefgh",) and preview.gap_limit == 1


# --- Known scripts, conflicts and client links -------------------------------------------------------


def test_a_reimport_from_another_wallet_client_links_it(conn: sqlite3.Connection, wallet: int) -> None:
    hw = ac.add_client(conn, "A hardware wallet", "hardware")
    phone = ac.add_client(conn, "A phone wallet", "mobile")
    address, _ = p2wpkh(1)
    imports.import_addresses(
        conn, imports.preview_addresses(conn, address), entity_id=ME, tax_account_id=wallet, client_ids=[hw]
    )
    again = imports.preview_addresses(conn, address)
    result = imports.import_addresses(conn, again, entity_id=ME, tax_account_id=wallet, client_ids=[phone])
    assert result == ac.Added((), ())
    assert ac.addresses(conn)[0].client_ids == (hw, phone)


def test_a_script_owned_elsewhere_shows_in_the_preview_and_is_never_moved(
    conn: sqlite3.Connection, wallet: int
) -> None:
    exchange = ac.add_entity(conn, "An exchange", "exchange")
    address, script = p2wpkh(1)
    ac.add_addresses(conn, [(script, address)], entity_id=exchange, tax_account_id=None, source="manual")
    preview = imports.preview_addresses(conn, address)
    assert preview.known == (imports.Known(script, address, exchange, None),)
    result = imports.import_addresses(conn, preview, entity_id=ME, tax_account_id=wallet)
    assert result.conflicts == (script,)
    assert ac.addresses(conn)[0].entity_id == exchange


def test_the_descriptor_preview_says_what_is_already_there(conn: sqlite3.Connection, wallet: int) -> None:
    exchange = ac.add_entity(conn, "An exchange", "exchange")
    taken_address, taken_script = p2wpkh(1001)
    ac.add_addresses(
        conn, [(taken_script, taken_address)], entity_id=exchange, tax_account_id=None, source="manual"
    )
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=3)
    assert not preview.already_imported
    assert preview.known == (imports.Known(taken_script, taken_address, exchange, None),)
    with pytest.raises(ImportRefused, match="another owner or account"):  # the storage refusal, as ours
        imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet)


def test_a_descriptor_imported_twice_is_flagged_and_gains_only_links(
    conn: sqlite3.Connection, wallet: int
) -> None:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=3)
    d = imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet)
    again = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=3)
    assert again.already_imported and len(again.known) == 3
    phone = ac.add_client(conn, "A phone wallet", "mobile")
    assert (
        imports.import_descriptor(conn, again, entity_id=ME, tax_account_id=wallet, client_ids=[phone]) == d
    )


# --- What reaches the node -------------------------------------------------------------------------


@pytest.mark.parametrize("pad", ["", "  ", "\t"])
def test_a_private_key_in_a_descriptor_never_reaches_the_node_t703(
    conn: sqlite3.Connection, pad: str
) -> None:
    tprv_shaped = "tprv" + ("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" * 2)[:100]
    node = Node()
    with pytest.raises(PrivateKeyError) as e:
        imports.preview_descriptor(node, conn, f"{pad}wpkh({tprv_shaped}/0/*){pad}")
    assert node.calls == [] and tprv_shaped not in str(e.value)


def test_text_that_isnt_valid_unicode_is_refused_without_its_content(conn: sqlite3.Connection) -> None:
    with pytest.raises(ImportRefused, match="valid text") as e:
        imports.preview_addresses(conn, "bcrt1q\ud800secretish")
    assert "secretish" not in str(e.value) and e.value.__cause__ is None


def test_line_numbers_count_newlines_only(conn: sqlite3.Connection) -> None:
    upload = "not one\u2028still line one\r\nline two\n" + p2wpkh(1)[0]
    preview = imports.preview_addresses(conn, upload)
    assert preview.invalid_lines == (1, 2)


def test_an_address_imported_with_an_earlier_start_keeps_its_own_subject(
    conn: sqlite3.Connection, wallet: int
) -> None:
    address, script = p2wpkh(1000)  # the descriptor's index 0
    imports.import_addresses(
        conn, imports.preview_addresses(conn, address), entity_id=ME, tax_account_id=wallet, start_height=0
    )
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=2)
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=100)
    subjects = imports.subjects(conn)
    lone = [s for s in subjects if s.subject.startswith("addrs:")]
    assert len(lone) == 1 and lone[0].scanobjects == (f"raw({script})",) and lone[0].start_height == 0


class PrivateNormalForm(Node):
    """A node that turns a public descriptor into a private one while claiming it has no keys."""

    def call(self, method: str, params: Any = ()) -> Any:
        reply = super().call(method, params)
        if method == "getdescriptorinfo":
            tprv_shaped = "tprv" + ("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" * 2)[:100]
            return {**reply, "descriptor": f"wpkh({tprv_shaped}/0/*)#abcdefgh"}
        return reply


def test_a_private_normal_form_is_never_derived_t703(conn: sqlite3.Connection) -> None:
    node = PrivateNormalForm()
    with pytest.raises(PrivateKeyError):
        imports.preview_descriptor(node, conn, TPUB_DESC)
    assert [method for method, _ in node.calls] == ["getdescriptorinfo"]


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


def test_an_address_imported_after_its_descriptor_with_an_earlier_start_is_scanned_from_it(
    conn: sqlite3.Connection, wallet: int
) -> None:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=2)
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=100)
    address, script = p2wpkh(1000)  # the descriptor's index 0
    again = imports.preview_addresses(conn, address)
    imports.import_addresses(conn, again, entity_id=ME, tax_account_id=wallet, start_height=0)
    lone = [s for s in imports.subjects(conn) if s.subject.startswith("addrs:")]
    assert len(lone) == 1 and lone[0].scanobjects == (f"raw({script})",) and lone[0].start_height == 0


def test_a_later_start_on_reimport_keeps_the_earlier_one(conn: sqlite3.Connection, wallet: int) -> None:
    address, _ = p2wpkh(1)
    for start in (5, 50):
        imports.import_addresses(
            conn,
            imports.preview_addresses(conn, address),
            entity_id=ME,
            tax_account_id=wallet,
            start_height=start,
        )
    assert ac.addresses(conn)[0].start_height == 5


def test_a_descriptor_reimported_with_an_earlier_start_is_a_new_subject_from_it(
    conn: sqlite3.Connection, wallet: int
) -> None:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=2)
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=100)
    [before] = imports.subjects(conn)
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet, start_height=10)
    [after] = imports.subjects(conn)
    assert after.subject != before.subject and after.start_height == 10


def test_an_upload_of_exactly_the_line_limit_ending_in_a_newline_passes(conn: sqlite3.Connection) -> None:
    address = p2wpkh(1)[0]
    preview = imports.preview_addresses(conn, f"{address}\n" * imports.MAX_LINES)
    assert preview.repeated == imports.MAX_LINES - 1
    with pytest.raises(ImportRefused, match="more than"):
        imports.preview_addresses(conn, f"{address}\n" * (imports.MAX_LINES + 1))

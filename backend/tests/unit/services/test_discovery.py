"""Growing ranged descriptor windows as their addresses are used (PLAN §1, §3; T-207, T-210).
Synthetic regtest scripts only; `integration/services/test_discovery_regtest.py` runs it against a
real node and wallet."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from coinacct.chain import descriptors
from coinacct.services import discovery, imports
from coinacct.services.discovery import DiscoveryError, wanted_end
from coinacct.storage import accounts as ac
from coinacct.storage import chain_cache as cc
from coinacct.storage.accounts import ME
from coinacct.storage.chain_state import Tip, set_tip
from coinacct.storage.datadir import open_data_dir
from coinacct.storage.db import open_db
from tests.unit.services.test_imports import TPUB_DESC, Node, conn, p2wpkh, wallet

__all__ = ["conn", "wallet"]  # the fixtures, shared with the import tests

TIP = Tip("aa" * 32, 500)


@pytest.mark.parametrize(
    ("range_end", "gap", "used", "want"),
    [
        (19, 20, None, 19),  # nothing used: the first window stays
        (19, 20, 0, 39),  # index 0 used: 20 unused indexes after it need index 20
        (19, 20, 5, 39),  # doubled (39) beats 5 + 20 = 25
        (39, 20, 38, 79),  # doubled again
        (19, 20, 19, 39),
        (99, 20, 99, 199),
        (999, 5000, 999, 1999),  # at most MAX_DERIVE more at once
        (discovery.MAX_RANGE_END - 10, 20, discovery.MAX_RANGE_END - 10, discovery.MAX_RANGE_END),
        (discovery.MAX_RANGE_END, 20, discovery.MAX_RANGE_END, discovery.MAX_RANGE_END),
    ],
)
def test_the_wanted_window_end(range_end: int, gap: int, used: int | None, want: int) -> None:
    assert wanted_end(range_end, gap, used) == want


def test_the_gap_limit_follows_the_last_used_index() -> None:
    for end in range(0, 300):
        for used in range(end + 1):
            grown = wanted_end(end, 20, used)
            assert grown == end or grown >= used + 20  # never grows short of the gap limit
            assert (grown == end) == (used + 20 <= end)
            assert grown - end <= descriptors.MAX_DERIVE


def _import(conn: sqlite3.Connection, wallet: int, gap: int = 4) -> int:
    preview = imports.preview_descriptor(Node(), conn, TPUB_DESC, gap_limit=gap)
    return imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet)


def _receive(conn: sqlite3.Connection, index: int) -> None:
    """Index `index` of TPUB_DESC (see Node) received coins, in a block below the tip."""
    if cc.reference_tip(conn) is None:
        set_tip(conn, TIP)
    event = cc.Activity("receive", p2wpkh(1000 + index)[1], f"{index:064x}", 0, 1000, "bb" * 32, 400)
    cc.put_activity(conn, [event], TIP)


def test_an_unused_window_stays(conn: sqlite3.Connection, wallet: int) -> None:
    d = _import(conn, wallet)
    node = Node()
    assert discovery.extend_windows(node, conn) == []
    assert node.calls == [] and ac.descriptors(conn)[0].range_end == 3
    assert ac.descriptors(conn)[0].highest_used is None and d


def test_a_used_index_near_the_end_grows_the_window_from_the_node(
    conn: sqlite3.Connection, wallet: int
) -> None:
    d = _import(conn, wallet)  # indexes 0..3
    _receive(conn, 1)
    node = Node()
    assert discovery.extend_windows(node, conn) == [d]
    [desc] = ac.descriptors(conn)
    assert desc.highest_used == 1 and desc.range_end == 7  # doubled; 1 + 4 = 5 is less
    assert node.calls == [("deriveaddresses", [TPUB_DESC + "#abcdefgh", [4, 7]])]
    assert ac.descriptor_scripts(conn, d)[4:] == [(i, p2wpkh(1000 + i)[1]) for i in range(4, 8)]
    # The next sync scans the wider window, and finds no more use: nothing grows.
    assert discovery.extend_windows(Node(), conn) == []


def test_unconfirmed_use_grows_the_window_but_isnt_recorded_as_used(
    conn: sqlite3.Connection, wallet: int
) -> None:
    d = _import(conn, wallet)
    assert discovery.extend_windows(Node(), conn, [p2wpkh(1003)[1]]) == [d]
    [desc] = ac.descriptors(conn)
    assert desc.range_end == 7 and desc.highest_used is None  # mempool txs can be replaced or evicted


def test_the_recorded_highest_used_index_counts(conn: sqlite3.Connection, wallet: int) -> None:
    d = _import(conn, wallet)
    ac.mark_used(conn, d, 3)  # recorded by an earlier sync whose activity a reorg has since removed
    assert discovery.extend_windows(Node(), conn) == [d]
    assert ac.descriptors(conn)[0].range_end == 7


def test_an_unranged_descriptor_never_grows(conn: sqlite3.Connection, wallet: int) -> None:
    preview = imports.preview_descriptor(Node(ranged=False), conn, "wpkh(tpubexample/0/7)")
    imports.import_descriptor(conn, preview, entity_id=ME, tax_account_id=wallet)
    set_tip(conn, TIP)
    cc.put_activity(conn, [cc.Activity("receive", p2wpkh(1000)[1], "11" * 32, 0, 1, "bb" * 32, 400)], TIP)
    node = Node(ranged=False)
    assert discovery.extend_windows(node, conn) == [] and node.calls == []


class Wrong(Node):
    """A node whose new addresses can't be used: another chain's, or one already in the window."""

    def __init__(self, how: str) -> None:
        super().__init__()
        self.how = how

    def call(self, method: str, params: Any = ()) -> Any:
        reply = super().call(method, params)
        if method != "deriveaddresses" or params[1][0] == 0:
            return reply
        if self.how == "chain":
            return [p2wpkh(5000 + i, "bc")[0] for i in range(len(reply))]
        if self.how == "repeat":
            return [p2wpkh(1000)[0]] * len(reply)
        return reply[:-1]  # "short"


@pytest.mark.parametrize("how", ["chain", "repeat", "short"])
def test_new_addresses_that_cant_be_trusted_grow_nothing(
    conn: sqlite3.Connection, wallet: int, how: str
) -> None:
    d = _import(conn, wallet)
    _receive(conn, 2)
    with pytest.raises(DiscoveryError, match=f"descriptor {d}") as e:
        discovery.extend_windows(Wrong(how), conn)
    assert "tpub" not in str(e.value)  # names the descriptor's id, never its text
    assert ac.descriptors(conn)[0].range_end == 3


def test_nothing_happens_before_a_chain_is_recorded(tmp_path: Path) -> None:
    d = tmp_path / "fresh"
    d.mkdir(mode=0o700)
    c = open_db(open_data_dir(str(d)))
    node = Node()
    assert discovery.extend_windows(node, c) == [] and node.calls == []
    c.close()

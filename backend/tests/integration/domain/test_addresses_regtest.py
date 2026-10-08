"""Address parsing against a real regtest node (THREAT_MODEL T-701): for every address type the
node's wallet makes, `parse_address` gives the script the node reports, and refuses the address on
other chains.

The node's wallet is used only as the harness user, to make addresses; the app never uses a wallet
(ADR 0004). Synthetic regtest data only.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from coinacct.domain.addresses import AddressError, parse_address
from harness.regtest import RegtestNode, regtest_node


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-addresses")) as n:
        n.admin("createwallet", ["harness"])
        yield n


@pytest.mark.parametrize("kind", ["legacy", "p2sh-segwit", "bech32", "bech32m"])
def test_each_address_type_gives_the_nodes_script_t701(node: RegtestNode, kind: str) -> None:
    text = node.admin("getnewaddress", ["", kind])
    expected = node.admin("getaddressinfo", [text])["scriptPubKey"]
    assert parse_address(text, "regtest").script_hex == expected
    if kind.startswith("bech32"):
        assert parse_address(text.upper(), "regtest").script_hex == expected
    # Base58 testnet versions are shared by testnet, signet and regtest; only segwit's prefix differs.
    others = ("main", "signet") if kind.startswith("bech32") else ("main",)
    for other in others:
        with pytest.raises(AddressError):
            parse_address(text, other)

"""Startup node checks against a real regtest bitcoind (ADR 0004; architecture §8.1;
THREAT_MODEL T-203, T-206, T-209, T-210)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from coinacct.chain.node_checks import MIN_VERSION, REQUIRED_INDEXES, Problem, check_node
from coinacct.rpc import RpcForbiddenError, RpcMethodNotAllowedError
from harness.regtest import APP_WHITELIST, RegtestNode, datadir_files, regtest_node
from tests.integration.conftest import app_client

# A public BIP32 test-vector key (BIP 32, test vector 1, chain m), as a regtest descriptor. Public data,
# never a developer's own key (ENGINEERING §3.1).
TEST_DESCRIPTOR = (
    "wpkh(tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1RhGjxbaTLV3X4FyWuejif"
    "B9jusQ46QzG87VKp/0/*)"
)


def problems_of(
    node: RegtestNode, expected_chain: str | None = "regtest", password: str | None = None
) -> list[Problem]:
    result = check_node(app_client(node, password), expected_chain, sync_delay=0.2)
    return [f.problem for f in result.findings]


def test_a_correctly_configured_node_passes_every_check(node: RegtestNode) -> None:
    result = check_node(app_client(node), "regtest")
    assert result.ok, [str(f) for f in result.findings]
    assert result.facts is not None
    assert result.facts.version >= MIN_VERSION
    assert result.facts.chain == "regtest"
    assert result.facts.block_count == 101
    assert set(REQUIRED_INDEXES) <= set(result.facts.indexes)


def test_a_new_data_directory_accepts_the_nodes_chain(node: RegtestNode) -> None:
    assert problems_of(node, expected_chain=None) == []


def test_a_mainnet_data_directory_refuses_a_regtest_node_t206(node: RegtestNode) -> None:
    assert problems_of(node, expected_chain="main") == [Problem.CHAIN_MISMATCH]


def test_wrong_credentials_are_an_auth_failure(node: RegtestNode) -> None:
    assert problems_of(node, password="not-the-password") == [Problem.AUTH_FAILED]


def test_the_whitelisted_node_refuses_the_canary_t203(node: RegtestNode) -> None:
    assert app_client(node).canary_refused() is True


def test_the_app_user_cant_call_a_wallet_method_even_without_the_client_allowlist_t203(
    node: RegtestNode,
) -> None:
    client = app_client(node)
    with pytest.raises(RpcMethodNotAllowedError):
        client.call("getwalletinfo")
    # Bypassing the client allowlist, the node's rpcwhitelist still refuses it.
    status, _ = client._post("getwalletinfo", [])
    assert status == 403


def test_amounts_from_the_node_are_decimal(node: RegtestNode) -> None:
    client = app_client(node)
    block = client.call("getblock", [client.call("getblockhash", [1]), 2])
    coinbase_value = block["tx"][0]["vout"][0]["value"]
    assert coinbase_value == Decimal("50.00000000")
    assert isinstance(coinbase_value, Decimal)


def test_a_node_without_rpcwhitelist_fails_the_canary_t203(tmp_path: Path) -> None:
    with regtest_node(tmp_path / "node", whitelist=None) as n:
        assert problems_of(n) == [Problem.WHITELIST_MISSING]


@pytest.mark.parametrize(
    ("enabled", "missing"),
    [
        (("txindex", "blockfilterindex"), "txospenderindex"),
        (("txindex", "txospenderindex"), "basic block filter index"),
        (("blockfilterindex", "txospenderindex"), "txindex"),
    ],
)
def test_a_node_without_a_required_index_is_refused_t210(
    tmp_path: Path, enabled: tuple[str, ...], missing: str
) -> None:
    with regtest_node(tmp_path / "node", indexes=enabled) as n:
        result = check_node(app_client(n), "regtest", sync_delay=0.2)
    assert [(f.problem, f.detail) for f in result.findings] == [(Problem.INDEX_MISSING, missing)]


def test_a_whitelist_without_a_needed_method_is_reported(tmp_path: Path) -> None:
    narrow = [m for m in APP_WHITELIST if m != "getindexinfo"]
    with regtest_node(tmp_path / "node", whitelist=narrow) as n:
        with pytest.raises(RpcForbiddenError):
            app_client(n).call("getindexinfo")
        assert problems_of(n) == [Problem.METHOD_REFUSED]


@pytest.mark.parametrize(
    "debug", [(), ("-debug=rpc", "-debug=http")], ids=["default-logging", "debug-rpc-http"]
)
def test_lookups_and_scans_leave_no_trace_in_the_node_datadir_except_the_canary_line_t209(
    tmp_path: Path, debug: tuple[str, ...]
) -> None:
    # THREAT_MODEL T-209's M0 check: after a run that looks up a txid and **scans a descriptor**
    # (scanblocks, getdescriptoractivity), search every file in the node's datadir for the
    # descriptor's key, its addresses and the txids. The only expected trace is the canary's
    # warning. Run with default logging and with debug=rpc,http, which T-209 and T-204 name.
    with regtest_node(tmp_path / "node", extra_args=debug) as n:
        client = app_client(n)
        descriptor = client.call("getdescriptorinfo", [TEST_DESCRIPTOR])["descriptor"]
        addresses = client.call("deriveaddresses", [descriptor, [0, 2]])
        hit_block = n.admin("generatetoaddress", [1, addresses[1]])[0]
        n.mine(100)  # so the scan's stop height (tip - 100 in the app) would reach the hit
        scan = client.call("scanblocks", ["start", [{"desc": descriptor, "range": [0, 2]}]])
        assert scan["completed"] is True
        assert hit_block in scan["relevant_blocks"]
        activity = client.call(
            "getdescriptoractivity", [[hit_block], [{"desc": descriptor, "range": [0, 2]}], False]
        )
        found_txid = activity["activity"][0]["txid"]
        coinbase_txid = client.call("getblock", [client.call("getblockhash", [1]), 1])["tx"][0]
        client.call("getrawtransaction", [coinbase_txid, 2])
        assert check_node(client, "regtest").ok
        debug_log = n.debug_log
    needles = {
        "descriptor key": b"tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7",
        "scanned txid": found_txid.encode(),
        "looked-up txid": coinbase_txid.encode(),
        **{f"address {i}": address.encode() for i, address in enumerate(addresses)},
    }
    hits = [
        (name, path.name)
        for path in datadir_files(tmp_path / "node")
        for name, needle in needles.items()
        if needle in path.read_bytes()
    ]
    assert hits == []
    assert b"RPC User ro-client not allowed to call method uptime" in debug_log.read_bytes()

"""The mempool pass against a real regtest node, as the app's whitelisted user (PLAN §1
"Unconfirmed activity"; THREAT_MODEL T-207).

The node's wallet is used only as the harness user, to make transactions to find; the app never
uses a wallet (ADR 0004). Synthetic regtest data only.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from coinacct.chain.mempool import pending_activity
from coinacct.storage.chain_state import Tip
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    with regtest_node(tmp_path_factory.mktemp("regtest-mempool"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        n.mine(101, n.admin("getnewaddress"))
        yield n


def tip(node: RegtestNode) -> Tip:
    return Tip(node.admin("getbestblockhash"), node.admin("getblockcount"))


def test_unconfirmed_activity_shows_until_it_confirms_t207(node: RegtestNode) -> None:
    rpc = app_client(node)
    watched = node.admin("getnewaddress")
    desc = [f"addr({watched})"]
    spk = node.admin("getaddressinfo", [watched])["scriptPubKey"]
    assert pending_activity(rpc, desc, tip(node)) == []

    paid = node.admin("sendtoaddress", [watched, "0.4"])
    pending = pending_activity(rpc, desc, tip(node))
    assert [(p.kind, p.txid, p.sats, p.script_hex) for p in pending] == [("receive", paid, 40_000_000, spk)]

    # Spend the still-unconfirmed output: the pass shows the spend too.
    vout = pending[0].n
    spend = node.admin(
        "sendall",
        [[node.admin("getnewaddress")], None, "unset", None, {"inputs": [{"txid": paid, "vout": vout}]}],
    )["txid"]
    kinds = {(p.kind, p.txid) for p in pending_activity(rpc, desc, tip(node))}
    assert kinds == {("receive", paid), ("spend", spend)}

    node.mine(1)  # confirmed: nothing is left in the mempool pass
    assert pending_activity(rpc, desc, tip(node)) == []


def test_a_replaced_transaction_is_gone_and_its_replacement_shows_t207(node: RegtestNode) -> None:
    rpc = app_client(node)
    watched = node.admin("getnewaddress")
    desc = [f"addr({watched})"]
    original = node.admin("sendtoaddress", [watched, "0.2"])  # the wallet signals RBF by default
    assert [p.txid for p in pending_activity(rpc, desc, tip(node))] == [original]
    replacement = node.admin("bumpfee", [original])["txid"]
    assert [p.txid for p in pending_activity(rpc, desc, tip(node))] == [replacement]  # never both
    node.mine(1)

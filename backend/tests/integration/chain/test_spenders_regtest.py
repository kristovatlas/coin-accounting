"""Spender lookups against a real regtest node, as the app's whitelisted user (PLAN §1).

The wallet is used only as the harness user, to make transactions. Synthetic regtest data only.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from coinacct.chain.spenders import SpendState, spend_of, spends
from coinacct.chain.txs import fetch_tx
from coinacct.rpc import RpcCallError
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client


@pytest.fixture(scope="module")
def wallet_node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    # A fixed fallback fee: a fresh regtest chain has no fee estimates.
    with regtest_node(tmp_path_factory.mktemp("regtest-spenders"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        n.mine(101, n.admin("getnewaddress"))
        yield n


def test_a_spend_is_seen_in_the_mempool_then_in_its_block(wallet_node: RegtestNode) -> None:
    rpc = app_client(wallet_node)
    child_id = wallet_node.admin("sendtoaddress", [wallet_node.admin("getnewaddress"), "1"])
    child = fetch_tx(rpc, child_id)
    parent_ref = child.inputs[0].prevout
    assert parent_ref is not None
    parent = fetch_tx(rpc, parent_ref.txid)
    unconfirmed = spend_of(rpc, parent, parent_ref.vout)
    assert unconfirmed.state is SpendState.SPENT_UNCONFIRMED and unconfirmed.spending_txid == child_id
    [block] = wallet_node.mine(1)
    confirmed = spend_of(rpc, parent, parent_ref.vout)
    assert confirmed.state is SpendState.SPENT and confirmed.spending_txid == child_id
    assert confirmed.blockhash == block


def test_unspent_outputs_and_a_batch(wallet_node: RegtestNode) -> None:
    rpc = app_client(wallet_node)
    txid = wallet_node.admin("sendtoaddress", [wallet_node.admin("getnewaddress"), "0.3"])
    [block] = wallet_node.mine(1)
    tx = fetch_tx(rpc, txid, block)
    states = [s.state for s in spends(rpc, [(tx, n) for n in range(len(tx.outputs))])]
    assert states == [SpendState.UNSPENT] * len(tx.outputs)


def test_an_op_return_output_is_unspendable(wallet_node: RegtestNode) -> None:
    rpc = app_client(wallet_node)
    raw = wallet_node.admin("createrawtransaction", [[], [{"data": "68656c6c6f"}]])
    funded = wallet_node.admin("fundrawtransaction", [raw])["hex"]
    signed = wallet_node.admin("signrawtransactionwithwallet", [funded])["hex"]
    txid = wallet_node.admin("sendrawtransaction", [signed])
    [block] = wallet_node.mine(1)
    tx = fetch_tx(rpc, txid, block)
    states = [s.state for s in spends(rpc, [(tx, n) for n in range(len(tx.outputs))])]
    assert states == [
        SpendState.UNSPENDABLE if o.script_type == "nulldata" else SpendState.UNSPENT for o in tx.outputs
    ]
    assert states.count(SpendState.UNSPENDABLE) == 1


def test_a_node_without_txospenderindex_fails_loudly_never_reads_unspent_t210(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    indexes = ("txindex", "blockfilterindex")  # no txospenderindex
    with regtest_node(tmp_path_factory.mktemp("regtest-noindex"), indexes=indexes) as n:
        n.mine(101)
        rpc = app_client(n)
        coinbase = n.admin("getblock", [n.admin("getblockhash", [1])])["tx"][0]
        tx = fetch_tx(rpc, coinbase, n.admin("getblockhash", [1]))
        with pytest.raises(RpcCallError):
            spend_of(rpc, tx, 0)

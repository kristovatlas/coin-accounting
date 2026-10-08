"""The transaction fetch layer against a real regtest node, as the app's whitelisted user (PLAN §1).

The node's wallet is used only as the harness user, to make transactions to look at; the app never
uses a wallet (ADR 0004). Synthetic regtest data only.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from coinacct.chain.txs import StaleBlockError, TxNotFoundError, fetch_tx, fill_prevouts
from harness.regtest import RegtestNode, regtest_node
from tests.integration.conftest import app_client


@pytest.fixture(scope="module")
def wallet_node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    # A fixed fallback fee: a fresh regtest chain has no fee estimates.
    with regtest_node(tmp_path_factory.mktemp("regtest-txs"), extra_args=["-fallbackfee=0.0001"]) as n:
        n.admin("createwallet", ["harness"])
        n.mine(101, n.admin("getnewaddress"))
        yield n


def test_a_confirmed_spend_is_decoded_with_its_spent_outputs(wallet_node: RegtestNode) -> None:
    to = wallet_node.admin("getnewaddress")
    txid = wallet_node.admin("sendtoaddress", [to, "1.25"])
    [blockhash] = wallet_node.mine(1)
    tx = fetch_tx(app_client(wallet_node), txid, blockhash)
    assert tx.confirmed and tx.blockhash == blockhash and not tx.coinbase
    assert tx.prevouts_known  # Core includes them for a confirmed tx
    assert 125_000_000 in [o.sats for o in tx.outputs]
    fee = wallet_node.admin("gettransaction", [txid])["fee"]
    assert tx.fee_sats == int(-Decimal(str(fee)) * 100_000_000)


def test_a_mempool_transaction_gets_its_prevouts_from_its_parents(wallet_node: RegtestNode) -> None:
    rpc = app_client(wallet_node)
    txid = wallet_node.admin("sendtoaddress", [wallet_node.admin("getnewaddress"), "0.5"])
    try:
        tx = fetch_tx(rpc, txid)
        assert not tx.confirmed
        filled = fill_prevouts(rpc, tx)
        assert filled.prevouts_known
        fee = wallet_node.admin("gettransaction", [txid])["fee"]
        assert filled.fee_sats == int(-Decimal(str(fee)) * 100_000_000)
    finally:
        wallet_node.mine(1)  # leave the mempool empty for the other tests


def test_an_op_return_output_is_unspendable(wallet_node: RegtestNode) -> None:
    rpc = app_client(wallet_node)
    raw = wallet_node.admin("createrawtransaction", [[], [{"data": "68656c6c6f"}]])
    funded = wallet_node.admin("fundrawtransaction", [raw])["hex"]
    signed = wallet_node.admin("signrawtransactionwithwallet", [funded])["hex"]
    txid = wallet_node.admin("sendrawtransaction", [signed])
    [blockhash] = wallet_node.mine(1)
    tx = fetch_tx(rpc, txid, blockhash)
    assert [o.unspendable for o in tx.outputs].count(True) == 1


def test_a_coinbase_is_fetched_by_its_block(wallet_node: RegtestNode) -> None:
    blockhash = wallet_node.admin("getblockhash", [5])
    coinbase = wallet_node.admin("getblock", [blockhash])["tx"][0]
    tx = fetch_tx(app_client(wallet_node), coinbase, blockhash)
    assert tx.coinbase and tx.fee_sats == 0


def test_the_genesis_coinbase_and_unknown_txids_are_not_found(wallet_node: RegtestNode) -> None:
    rpc = app_client(wallet_node)
    genesis = wallet_node.admin("getblockhash", [0])
    with pytest.raises(TxNotFoundError):
        fetch_tx(rpc, wallet_node.admin("getblock", [genesis])["tx"][0], genesis)
    with pytest.raises(TxNotFoundError):
        fetch_tx(rpc, "00" * 32)


def test_a_transaction_in_a_reorged_away_block_is_stale_t207(wallet_node: RegtestNode) -> None:
    txid = wallet_node.admin("sendtoaddress", [wallet_node.admin("getnewaddress"), "0.1"])
    [blockhash] = wallet_node.mine(1)
    wallet_node.admin("invalidateblock", [blockhash])  # test harness only, never the app
    rpc = app_client(wallet_node)
    try:
        with pytest.raises(StaleBlockError):
            fetch_tx(rpc, txid, blockhash)
        # Through getblockheader: a tx the node doesn't have in a side-branch block, and an unknown block.
        with pytest.raises(StaleBlockError):
            fetch_tx(rpc, "00" * 32, blockhash)
        with pytest.raises(StaleBlockError):
            fetch_tx(rpc, txid, "11" * 32)
        # An active block that doesn't hold the tx: the tx is missing, the block isn't stale.
        with pytest.raises(TxNotFoundError):
            fetch_tx(rpc, txid, wallet_node.admin("getblockhash", [5]))
    finally:
        wallet_node.admin("reconsiderblock", [blockhash])
        wallet_node.wait_for_indexes()

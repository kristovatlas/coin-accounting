"""The transaction fetch layer, against a fake node (PLAN §1; THREAT_MODEL T-205, T-207, T-208, T-502)."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from coinacct.chain import txs
from coinacct.chain.txs import (
    BudgetExceededError,
    MalformedTxError,
    StaleBlockError,
    TxNotFoundError,
    fetch_tx,
    fill_prevouts,
    parse_tx,
)
from coinacct.rpc import RpcCallError

PARENT, CHILD, BLOCK = "11" * 32, "22" * 32, "bb" * 32
P2WPKH = {"hex": "0014" + "ab" * 20, "type": "witness_v0_keyhash", "address": "bcrt1qexample"}


def vout(n: int, btc: str, spk: dict[str, Any] = P2WPKH) -> dict[str, Any]:
    return {"value": Decimal(btc), "n": n, "scriptPubKey": dict(spk)}


def confirmed_parent() -> dict[str, Any]:
    return {
        "txid": PARENT,
        "blockhash": BLOCK,
        "confirmations": 3,
        "blocktime": 1_700_000_000,
        "in_active_chain": True,
        "vin": [{"coinbase": "51", "sequence": 0xFFFFFFFF}],
        "vout": [vout(0, "50"), vout(1, "0", {"hex": "6a0141", "type": "nulldata"})],
    }


def mempool_child() -> dict[str, Any]:
    return {
        "txid": CHILD,
        "confirmations": 0,
        "vin": [{"txid": PARENT, "vout": 0, "sequence": 0xFFFFFFFD}],  # no prevout: unconfirmed
        "vout": [vout(0, "49.99990000")],
    }


class FakeNode:
    def __init__(
        self,
        txs: dict[str, dict[str, Any]],
        error: RpcCallError | None = None,
        headers: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.txs, self.error, self.calls = txs, error, list[list[Any]]()
        self.headers = headers or {}

    def call(self, method: str, params: Any = ()) -> Any:
        if method == "getblockheader":
            self.calls.append([method, *params])
            if params[0] not in self.headers:
                raise RpcCallError(method, -5, "Block not found")
            return self.headers[params[0]]
        assert method == "getrawtransaction"
        self.calls.append(list(params))
        if self.error is not None:
            raise self.error
        if params[0] not in self.txs:
            raise RpcCallError(method, -5, "No such mempool or blockchain transaction")
        return copy.deepcopy(self.txs[params[0]])


def test_a_confirmed_transaction_is_fetched_with_its_block_and_decoded_exactly() -> None:
    node = FakeNode({PARENT: confirmed_parent()})
    tx = fetch_tx(node, PARENT, BLOCK)
    assert node.calls == [[PARENT, 2, BLOCK]]  # verbosity 2, keyed by block for BIP30
    assert tx.txid == PARENT and tx.blockhash == BLOCK and tx.confirmed and tx.coinbase
    assert [o.sats for o in tx.outputs] == [5_000_000_000, 0]
    assert [o.unspendable for o in tx.outputs] == [False, True]
    assert tx.block_time == 1_700_000_000


def test_an_unconfirmed_transaction_gets_its_spent_outputs_from_the_parent() -> None:
    node = FakeNode({PARENT: confirmed_parent(), CHILD: mempool_child()})
    child = fetch_tx(node, CHILD)
    assert not child.confirmed and not child.prevouts_known and child.fee_sats is None
    filled = fill_prevouts(node, child)
    assert filled.inputs[0].spent is not None and filled.inputs[0].spent.sats == 5_000_000_000
    assert filled.fee_sats == 10_000
    assert node.calls[-1] == [PARENT, 2]


def test_prevouts_core_included_are_used_without_another_call() -> None:
    raw = mempool_child()
    raw["vin"][0]["prevout"] = {
        "generated": True,
        "height": 1,
        "value": Decimal("50"),
        "scriptPubKey": P2WPKH,
    }
    node = FakeNode({CHILD: raw})
    tx = fetch_tx(node, CHILD)
    assert fill_prevouts(node, tx) is tx and len(node.calls) == 1


def test_an_unknown_txid_is_not_found_and_other_node_errors_pass_through() -> None:
    with pytest.raises(TxNotFoundError):
        fetch_tx(FakeNode({}), PARENT)
    with pytest.raises(RpcCallError):
        fetch_tx(FakeNode({}, RpcCallError("getrawtransaction", -8, "parameter error")), PARENT)


def stale_parent() -> dict[str, Any]:
    """What Core sends for a block off the active chain: no confirmations and no block time."""
    raw = confirmed_parent() | {"in_active_chain": False, "confirmations": 0}
    del raw["blocktime"]
    return raw


def test_a_transaction_in_a_reorged_away_block_is_stale_t207() -> None:
    with pytest.raises(StaleBlockError):
        fetch_tx(FakeNode({PARENT: stale_parent()}), PARENT, BLOCK)
    assert not parse_tx(stale_parent()).confirmed


@pytest.mark.parametrize("value", [KeyError, None, "true", 1])
def test_a_missing_or_odd_in_active_chain_is_malformed_not_a_reorg(value: object) -> None:
    raw = confirmed_parent()
    if value is KeyError:
        del raw["in_active_chain"]
    else:
        raw["in_active_chain"] = value
    with pytest.raises(MalformedTxError, match="in_active_chain"):
        fetch_tx(FakeNode({PARENT: raw}), PARENT, BLOCK)


@pytest.mark.parametrize(("field", "value"), [("confirmations", 0), ("blocktime", KeyError)])
def test_an_active_block_needs_confirmations_and_a_block_time(field: str, value: object) -> None:
    raw = confirmed_parent()
    if value is KeyError:
        del raw[field]
    else:
        raw[field] = value
    with pytest.raises(MalformedTxError, match="active block"):
        fetch_tx(FakeNode({PARENT: raw}), PARENT, BLOCK)


def test_an_unknown_or_side_branch_block_is_stale_not_a_missing_tx_t207() -> None:
    with pytest.raises(StaleBlockError):  # the node doesn't know the block at all
        fetch_tx(FakeNode({}), PARENT, BLOCK)
    with pytest.raises(StaleBlockError):  # a known block on a side branch
        fetch_tx(FakeNode({}, headers={BLOCK: {"confirmations": -1}}), PARENT, BLOCK)
    with pytest.raises(TxNotFoundError):  # an active block that doesn't hold the tx
        fetch_tx(FakeNode({}, headers={BLOCK: {"confirmations": 4}}), PARENT, BLOCK)
    for bad in ({"height": 4}, {"confirmations": 0}, {"confirmations": -2}, {"confirmations": "1"}):
        with pytest.raises(MalformedTxError, match="getblockheader"):
            fetch_tx(FakeNode({}, headers={BLOCK: bad}), PARENT, BLOCK)


def test_other_getblockheader_errors_pass_through() -> None:
    class Failing(FakeNode):
        def call(self, method: str, params: Any = ()) -> Any:
            if method == "getblockheader":
                raise RpcCallError(method, -1, "something else")
            return super().call(method, params)

    with pytest.raises(RpcCallError):
        fetch_tx(Failing({}), PARENT, BLOCK)


def test_a_negative_fee_is_malformed_t502() -> None:
    child = mempool_child()
    child["vout"][0]["value"] = Decimal("50.00000001")  # more than the parent's 50 BTC
    node = FakeNode({PARENT: confirmed_parent(), CHILD: child})
    with pytest.raises(MalformedTxError, match="worth more"):
        fill_prevouts(node, fetch_tx(node, CHILD))
    with_prevout = mempool_child()
    with_prevout["vout"][0]["value"] = Decimal("50.00000001")
    with_prevout["vin"][0]["prevout"] = {"value": Decimal("50"), "scriptPubKey": P2WPKH}
    with pytest.raises(MalformedTxError, match="worth more"):
        parse_tx(with_prevout)


def test_outputs_totalling_more_than_all_bitcoin_are_malformed_t502() -> None:
    raw = confirmed_parent()
    raw["vout"] = [vout(0, "21000000"), vout(1, "0.00000001")]
    with pytest.raises(MalformedTxError, match="21 million"):
        parse_tx(raw)


def test_spent_outputs_totalling_more_than_all_bitcoin_are_malformed_t502() -> None:
    raw = mempool_child()
    raw["vin"] = [
        {
            "txid": PARENT,
            "vout": n,
            "sequence": 0,
            "prevout": {"value": Decimal("21000000"), "scriptPubKey": P2WPKH},
        }
        for n in range(2)
    ]
    with pytest.raises(MalformedTxError, match="spent outputs total"):
        parse_tx(raw)


def test_fill_prevouts_has_a_parent_budget_t205(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(txs, "MAX_PARENTS", 1)
    other = "33" * 32
    child = mempool_child()
    child["vin"].append({"txid": other, "vout": 0, "sequence": 0})
    node = FakeNode({PARENT: confirmed_parent(), CHILD: child, other: confirmed_parent() | {"txid": other}})
    with pytest.raises(BudgetExceededError):
        fill_prevouts(node, fetch_tx(node, CHILD))
    monkeypatch.setattr(txs, "MAX_PARENTS", 2)
    assert fill_prevouts(node, fetch_tx(node, CHILD)).prevouts_known


@pytest.mark.parametrize(
    ("txid", "blockhash"), [("AA" * 32, None), ("aa" * 31, None), (PARENT, "BB" * 32), (PARENT, "")]
)
def test_malformed_ids_are_refused_before_any_call(txid: str, blockhash: str | None) -> None:
    node = FakeNode({})
    with pytest.raises(ValueError, match="hex"):
        fetch_tx(node, txid, blockhash)
    assert node.calls == []


def test_a_reply_for_another_txid_or_block_is_refused() -> None:
    other = confirmed_parent() | {"txid": CHILD}
    with pytest.raises(MalformedTxError, match="different transaction"):
        fetch_tx(FakeNode({PARENT: other}), PARENT)
    moved = confirmed_parent() | {"blockhash": "cc" * 32}
    with pytest.raises(MalformedTxError, match="different block"):
        fetch_tx(FakeNode({PARENT: moved}), PARENT, BLOCK)


def test_a_parent_without_the_spent_output_is_malformed() -> None:
    child = mempool_child()
    child["vin"][0]["vout"] = 5
    node = FakeNode({PARENT: confirmed_parent(), CHILD: child})
    with pytest.raises(MalformedTxError, match="parent"):
        fill_prevouts(node, fetch_tx(node, CHILD))


def test_each_parent_is_fetched_once() -> None:
    child = mempool_child()
    child["vin"].append({"txid": PARENT, "vout": 1, "sequence": 0})
    node = FakeNode({PARENT: confirmed_parent(), CHILD: child})
    filled = fill_prevouts(node, fetch_tx(node, CHILD))
    assert filled.prevouts_known and [c[0] for c in node.calls] == [CHILD, PARENT]
    spent = [i.spent for i in filled.inputs]
    assert [(o.sats, o.script_type) for o in spent if o] == [
        (5_000_000_000, "witness_v0_keyhash"),
        (0, "nulldata"),
    ]


def _broken(path: str, value: object) -> dict[str, Any]:
    raw = confirmed_parent()
    target: Any = raw
    *keys, last = path.split(".")
    for k in keys:
        target = target[int(k)] if k.isdigit() else target[k]
    if value is KeyError:
        del target[int(last) if last.isdigit() else last]
    else:
        target[int(last) if last.isdigit() else last] = value
    return raw


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("txid", "nothex"),
        ("blockhash", 12),
        ("confirmations", -1),
        ("confirmations", True),
        ("blocktime", "yesterday"),
        ("vin", []),
        ("vout", []),
        ("vout.0.n", 1),
        ("vout.0.value", 50),  # an int, not a Decimal: the RPC client never produces one
        ("vout.0.value", Decimal("0.000000001")),
        ("vout.0.scriptPubKey.hex", "zz"),
        ("vout.0.scriptPubKey.type", ""),
        ("vout.0.scriptPubKey.address", 7),
        ("vin.0.sequence", 2**32),
        ("vin.0.coinbase", "xyz"),
        ("vin.0", "not an object"),
    ],
)
def test_a_reply_of_the_wrong_shape_is_malformed_t205(path: str, value: object) -> None:
    with pytest.raises(MalformedTxError):
        parse_tx(_broken(path, value))


def test_an_unconfirmed_tx_with_confirmations_is_malformed() -> None:
    raw = mempool_child() | {"confirmations": 2}
    with pytest.raises(MalformedTxError, match="confirmations"):
        parse_tx(raw)


def test_a_coinbase_input_among_others_is_malformed() -> None:
    raw = mempool_child()
    raw["vin"].append({"coinbase": "51", "sequence": 0})
    with pytest.raises(MalformedTxError, match="coinbase"):
        parse_tx(raw)


def test_an_input_that_names_no_output_is_malformed() -> None:
    for bad in ({"txid": PARENT, "sequence": 0}, {"txid": "x", "vout": 0, "sequence": 0}):
        raw = mempool_child()
        raw["vin"] = [bad]
        with pytest.raises(MalformedTxError, match="name the output"):
            parse_tx(raw)


def test_a_reply_that_isnt_an_object_is_malformed() -> None:
    with pytest.raises(MalformedTxError):
        parse_tx(["not", "a", "tx"])

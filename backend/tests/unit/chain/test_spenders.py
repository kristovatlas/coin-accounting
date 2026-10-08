"""Spender lookups against a fake node (PLAN §1; THREAT_MODEL T-205)."""

from __future__ import annotations

from typing import Any

import pytest

from coinacct.chain import spenders
from coinacct.chain.spenders import Spend, SpendState, spend_of, spends
from coinacct.chain.txs import MalformedTxError
from coinacct.domain.chain import Outpoint, Tx, TxIn, TxOut

TXID, SPENDER, BLOCK = "11" * 32, "22" * 32, "bb" * 32
PAY = TxOut(1000, "0014" + "ab" * 20, "witness_v0_keyhash")
DATA = TxOut(0, "6a0141", "nulldata")


def tx(*outputs: TxOut, txid: str = TXID) -> Tx:
    return Tx(txid, BLOCK, 1, 0, (TxIn(None, 0xFFFFFFFF),), outputs)


class FakeNode:
    def __init__(self, answers: dict[tuple[str, int], dict[str, Any]] | None = None) -> None:
        self.answers, self.calls = answers or {}, list[Any]()

    def call(self, method: str, params: Any = ()) -> Any:
        assert method == "gettxspendingprevout"
        self.calls.append(params)
        outs, options = params
        assert options == {"mempool_only": False}  # a missing index must fail, never read as unspent
        return [
            {"txid": o["txid"], "vout": o["vout"], **self.answers.get((o["txid"], o["vout"]), {})}
            for o in outs
        ]


def test_unspent_unconfirmed_and_confirmed_spends_are_told_apart() -> None:
    node = FakeNode(
        {(TXID, 1): {"spendingtxid": SPENDER}, (TXID, 2): {"spendingtxid": SPENDER, "blockhash": BLOCK}}
    )
    t = tx(PAY, PAY, PAY)
    assert spends(node, [(t, 0), (t, 1), (t, 2)]) == [
        Spend(Outpoint(TXID, 0), SpendState.UNSPENT),
        Spend(Outpoint(TXID, 1), SpendState.SPENT_UNCONFIRMED, SPENDER),
        Spend(Outpoint(TXID, 2), SpendState.SPENT, SPENDER, BLOCK),
    ]
    assert len(node.calls) == 1  # one call for the batch


def test_an_unspendable_output_is_answered_without_asking_the_node() -> None:
    node = FakeNode()
    assert spend_of(node, tx(DATA), 0) == Spend(Outpoint(TXID, 0), SpendState.UNSPENDABLE)
    assert node.calls == []


def test_unspendable_and_spendable_outputs_keep_their_order() -> None:
    node = FakeNode({(TXID, 2): {"spendingtxid": SPENDER}})
    t = tx(PAY, DATA, PAY)
    states = [s.state for s in spends(node, [(t, 2), (t, 1), (t, 0)])]
    assert states == [SpendState.SPENT_UNCONFIRMED, SpendState.UNSPENDABLE, SpendState.UNSPENT]


def test_large_batches_are_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spenders, "MAX_BATCH", 2)
    node = FakeNode()
    t = tx(*[PAY] * 5)
    assert len(spends(node, [(t, n) for n in range(5)])) == 5
    assert [len(c[0]) for c in node.calls] == [2, 2, 1]


def test_an_output_the_tx_doesnt_have_is_refused() -> None:
    for n in (1, -1):  # -1 must not wrap around to the last output
        with pytest.raises(IndexError):
            spend_of(FakeNode(), tx(PAY), n)


class Broken:
    def __init__(self, reply: Any) -> None:
        self.reply = reply

    def call(self, method: str, params: Any = ()) -> Any:
        return self.reply


@pytest.mark.parametrize(
    "reply",
    [
        {"not": "a list"},
        [],  # fewer answers than outputs
        [{"txid": SPENDER, "vout": 0}],  # another output
        [{"txid": TXID, "vout": 1}],
        [{"txid": TXID, "vout": 0, "blockhash": BLOCK}],  # a block without a spender
        [{"txid": TXID, "vout": 0, "spendingtxid": "nothex"}],
        [{"txid": TXID, "vout": 0, "spendingtxid": SPENDER, "blockhash": 7}],
        ["not an object"],
    ],
)
def test_a_reply_of_the_wrong_shape_is_malformed_t205(reply: Any) -> None:
    with pytest.raises(MalformedTxError):
        spend_of(Broken(reply), tx(PAY), 0)

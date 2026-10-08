"""Spender lookups against a fake node (PLAN §1; THREAT_MODEL T-205)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from coinacct.chain import spenders
from coinacct.chain.spenders import Spend, SpendState, spend_of, spends
from coinacct.chain.txs import BudgetExceededError, MalformedTxError
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


@pytest.mark.parametrize(
    ("dup", "earlier", "later"),
    [
        (  # heights 91812 and 91842
            "d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599",
            "00000000000af0aed4792b1acee3d966af36cf5def14935db8de83d6f9306f2f",
            "00000000000a4d0a398161ffc163c503763b1f4360639393e0e4c8e300e0caec",
        ),
        (  # heights 91722 and 91880
            "e3bf3d07d4b0375638d5f1db5255fe07ba2c4cb067cd81b84ee974b6585fb468",
            "00000000000271a2dc26e7667f8419f2e15416dc6955e5a6c6cdf3f2574dd08e",
            "00000000000743f190a18c5577a3c2d2a1f610ae9601ac046a38084ccb7cd721",
        ),
    ],
)
def test_the_earlier_bip30_duplicate_coinbase_is_unspendable_t208(dup: str, earlier: str, later: str) -> None:
    def copy(blockhash: str | None) -> Tx:
        return Tx(
            dup, blockhash, 1 if blockhash else 0, 0 if blockhash else None, (TxIn(None, 0xFFFFFFFF),), (PAY,)
        )

    node = FakeNode()
    assert spend_of(node, copy(earlier), 0).state is SpendState.UNSPENDABLE and node.calls == []
    # The later copy, one in any other block, or one without a block: Core's answer (txindex keeps the later).
    for blockhash in (later, "cc" * 32):
        assert spend_of(node, copy(blockhash), 0).state is SpendState.UNSPENT
    assert len(node.calls) == 2


def test_a_call_has_an_output_budget_t205(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spenders, "MAX_OUTPUTS", 2)
    t = tx(PAY, PAY, PAY)
    with pytest.raises(BudgetExceededError):
        spends(FakeNode(), [(t, 0), (t, 1), (t, 2)])
    assert len(spends(FakeNode(), [(t, 0), (t, 1)])) == 2


@pytest.mark.parametrize(
    ("state", "spender", "block"),
    [
        (SpendState.SPENT, SPENDER, None),
        (SpendState.SPENT_UNCONFIRMED, None, None),
        (SpendState.UNSPENT, SPENDER, None),
        (SpendState.UNSPENDABLE, None, BLOCK),
    ],
)
def test_a_spend_must_match_its_state(state: SpendState, spender: str | None, block: str | None) -> None:
    with pytest.raises(ValueError, match="match its state"):
        Spend(Outpoint(TXID, 0), state, spender, block)


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
        [{"txid": TXID, "vout": False}],  # a bool isn't an output index, even though False == 0
        [{"txid": TXID, "vout": Decimal("0.0")}],
        [{"txid": TXID, "vout": 0, "blockhash": BLOCK}],  # a block without a spender
        [{"txid": TXID, "vout": 0, "spendingtxid": "nothex"}],
        [{"txid": TXID, "vout": 0, "spendingtxid": SPENDER, "blockhash": 7}],
        ["not an object"],
    ],
)
def test_a_reply_of_the_wrong_shape_is_malformed_t205(reply: Any) -> None:
    with pytest.raises(MalformedTxError):
        spend_of(Broken(reply), tx(PAY), 0)

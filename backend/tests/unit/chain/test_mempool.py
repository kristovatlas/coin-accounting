"""The mempool pass against a fake node (PLAN §1 "Unconfirmed activity"; THREAT_MODEL T-207)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from coinacct.chain.mempool import PendingActivity, pending_activity
from coinacct.chain.scans import MalformedScanError
from coinacct.domain.chain import Outpoint

SPK = "0014" + "11" * 20
DESC = ["addr(bcrt1qexample)"]


def h(n: int) -> str:
    return f"{n:064x}"


class Node:
    def __init__(self, activity: Any) -> None:
        self.activity = activity
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append((method, params))
        assert method == "getdescriptoractivity"
        return {"activity": self.activity} if not isinstance(self.activity, Exception) else self.activity


RECEIVE = {"type": "receive", "amount": Decimal("0.25"), "txid": h(1), "vout": 2, "output_spk": {"hex": SPK}}
SPEND = {
    "type": "spend",
    "amount": Decimal("0.25"),
    "spend_txid": h(3),
    "spend_vin": 0,
    "prevout_txid": h(1),
    "prevout_vout": 2,
    "prevout_spk": {"hex": SPK},
}


def test_only_the_mempool_is_asked_and_nothing_is_stored_t207() -> None:
    node = Node([RECEIVE, SPEND])
    got = pending_activity(node, DESC)
    assert node.calls == [("getdescriptoractivity", [[], DESC, True])]  # no blocks, mempool only
    assert got == [
        PendingActivity("receive", SPK, h(1), 2, 25_000_000),
        PendingActivity("spend", SPK, h(3), 0, 25_000_000, Outpoint(h(1), 2)),
    ]


def test_an_empty_mempool_is_no_activity() -> None:
    assert pending_activity(Node([]), DESC) == []


@pytest.mark.parametrize(
    "event",
    [
        {**RECEIVE, "blockhash": h(9), "height": 5},  # a confirmed event never comes from this pass
        {**RECEIVE, "height": 5},
        {**RECEIVE, "amount": 0.25},
        {**RECEIVE, "txid": "zz"},
        {**SPEND, "prevout_txid": None},
        "not an event",
    ],
)
def test_a_malformed_mempool_event_is_refused_t205(event: Any) -> None:
    with pytest.raises(MalformedScanError):
        pending_activity(Node([event]), DESC)


def test_the_same_event_twice_is_refused() -> None:
    with pytest.raises(MalformedScanError, match="twice"):
        pending_activity(Node([RECEIVE, dict(RECEIVE)]), DESC)


@pytest.mark.parametrize("reply", [None, [], {"activity": None}])
def test_a_malformed_reply_is_refused(reply: Any) -> None:
    class Odd(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            return reply

    with pytest.raises(MalformedScanError, match="activity list"):
        pending_activity(Odd([]), DESC)

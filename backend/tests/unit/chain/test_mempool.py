"""The mempool pass against a fake node (PLAN §1 "Unconfirmed activity"; THREAT_MODEL T-207)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from coinacct.chain import mempool
from coinacct.chain.mempool import PendingActivity, pending_activity
from coinacct.chain.scans import ActivityBudgetError, MalformedScanError, StaleScanError
from coinacct.domain.chain import Outpoint
from coinacct.storage.chain_state import Tip

SPK = "0014" + "11" * 20
DESC = ["addr(bcrt1qexample)"]


def h(n: int) -> str:
    return f"{n:064x}"


TIP = Tip(h(100), 100)


class Node:
    def __init__(self, activity: Any, best: list[str] | None = None) -> None:
        self.activity = activity
        self.best = best or [TIP.blockhash, TIP.blockhash]  # getbestblockhash before, after
        self.calls: list[tuple[str, Any]] = []

    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append((method, params))
        if method == "getbestblockhash":
            return self.best.pop(0)
        assert method == "getdescriptoractivity"
        return {"activity": self.activity}


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
    got = pending_activity(node, DESC, TIP)
    # The tip is checked before and after; no blocks are asked for, only the mempool.
    assert node.calls == [
        ("getbestblockhash", ()),
        ("getdescriptoractivity", [[], DESC, True]),
        ("getbestblockhash", ()),
    ]
    assert got == [
        PendingActivity("receive", SPK, h(1), 2, 25_000_000),
        PendingActivity("spend", SPK, h(3), 0, 25_000_000, Outpoint(h(1), 2)),
    ]


def test_an_empty_mempool_is_no_activity() -> None:
    assert pending_activity(Node([]), DESC, TIP) == []


@pytest.mark.parametrize(
    "event",
    [
        {**RECEIVE, "blockhash": h(9), "height": 5},  # a confirmed event never comes from this pass
        {**RECEIVE, "blockhash": h(9)},
        {**RECEIVE, "height": 5},
        {**RECEIVE, "amount": 0.25},
        {**RECEIVE, "txid": "zz"},
        {**SPEND, "prevout_txid": None},
        "not an event",
    ],
)
def test_a_malformed_mempool_event_is_refused_t205(event: Any) -> None:
    with pytest.raises(MalformedScanError):
        pending_activity(Node([event]), DESC, TIP)


def test_the_same_event_twice_is_refused() -> None:
    with pytest.raises(MalformedScanError, match="twice"):
        pending_activity(Node([RECEIVE, dict(RECEIVE)]), DESC, TIP)


@pytest.mark.parametrize("reply", [None, [], {"activity": None}])
def test_a_malformed_reply_is_refused(reply: Any) -> None:
    class Odd(Node):
        def call(self, method: str, params: Any = ()) -> Any:
            return TIP.blockhash if method == "getbestblockhash" else reply

    with pytest.raises(MalformedScanError, match="activity list"):
        pending_activity(Odd([]), DESC, TIP)


@pytest.mark.parametrize("best", [[h(101), TIP.blockhash], [TIP.blockhash, h(101)]])
def test_a_pass_on_another_tip_than_the_caches_is_stale_t207(best: list[str]) -> None:
    with pytest.raises(StaleScanError, match="catch up"):
        pending_activity(Node([RECEIVE], best=best), DESC, TIP)


def test_two_spends_of_one_output_are_refused() -> None:
    other = {**SPEND, "spend_txid": h(4)}
    with pytest.raises(MalformedScanError, match="two spends"):
        pending_activity(Node([SPEND, other]), DESC, TIP)


def test_a_busy_scripts_mempool_is_bounded_t205(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mempool, "MAX_PENDING", 2)
    events = [{**RECEIVE, "vout": v} for v in range(3)]
    with pytest.raises(ActivityBudgetError):
        pending_activity(Node(events), DESC, TIP)
    assert len(pending_activity(Node(events[:2]), DESC, TIP)) == 2


def test_a_malformed_tip_reply_is_refused() -> None:
    with pytest.raises(MalformedScanError, match="block hash"):
        pending_activity(Node([], best=["zz", "zz"]), DESC, TIP)

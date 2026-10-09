"""Coin tracing through one wallet transaction (ADR 0041 §2 and §3; T-507, T-508, T-509). The two worked
examples are ADR 0041's own; every other figure is worked by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.tax.engine import EngineError
from coinacct.tax.tracing import (
    Blocked,
    FeeCarry,
    Fragment,
    Input,
    Leaving,
    LeavingKind,
    Output,
    Traced,
    WalletTx,
    trace,
)

BTC = 100_000_000
T0 = datetime(2024, 1, 1, tzinfo=UTC)
PAY = b"\x00\x14pay"
CHANGE = b"\x00\x14chg"
DEST = b"\x00\x14dst"


def frag(lot: str, sats: int, days: int = 0, event: str | None = None) -> Fragment:
    return Fragment(lot, sats, T0 + timedelta(days=days), event or f"e-{lot}")


def coin(*fragments: Fragment, account: str | None = "w") -> Input:
    return Input(account, sum(f.sats for f in fragments), fragments)


def tx(inputs: list[Input], outputs: list[Output], leaving: Leaving | None = None, **kw: Any) -> WalletTx:
    return WalletTx(kw.get("txid", "t1"), kw.get("account", "w"), tuple(inputs), tuple(outputs), leaving)


def lots(parts: tuple[Fragment, ...]) -> list[tuple[str, int]]:
    return [(f.lot, f.sats) for f in parts]


def traced(t: WalletTx) -> Traced:
    result = trace(t)
    assert isinstance(result, Traced), result
    return result


def by_vout(result: Traced) -> dict[int, list[tuple[str, int]]]:
    return {vout: lots(parts) for vout, parts in result.outputs}


# ADR 0041's coin: 1 BTC holding L1 (0.4, oldest) and L2 (0.6).
ADR_COIN = coin(frag("L1", 40_000_000, 0), frag("L2", 60_000_000, 10))


@pytest.mark.parametrize("change_first", [False, True])
def test_adr_worked_example_spend(change_first: bool) -> None:
    pay = Output(0, 50_000_000, PAY, event="s1")
    change = Output(1, 49_990_000, CHANGE, account="w")
    outputs = [change, pay] if change_first else [pay, change]
    result = traced(tx([ADR_COIN], outputs, Leaving("s1", "spend", 50_000_000)))
    assert by_vout(result) == {0: [("L1", 40_000_000), ("L2", 10_000_000)], 1: [("L2", 49_990_000)]}
    assert lots(result.fee) == [("L2", 10_000)]
    assert result.carries == ()  # a spend's fee is part of the disposal: no carry


@pytest.mark.parametrize("change_first", [False, True])
def test_adr_worked_example_transfer(change_first: bool) -> None:
    dest = Output(0, 50_000_000, DEST, account="w2", event="x1")
    change = Output(1, 49_990_000, CHANGE, account="w")
    outputs = [change, dest] if change_first else [dest, change]
    result = traced(tx([ADR_COIN], outputs, Leaving("x1", "transfer", 50_000_000)))
    assert by_vout(result) == {0: [("L1", 40_000_000), ("L2", 10_000_000)], 1: [("L2", 49_990_000)]}
    # The fee's L2 basis passes to the destination's L2 fragment.
    assert result.carries == (FeeCarry("L2", 10_000, 0, "L2"),)


def test_fee_lot_not_on_the_destination_carries_onto_its_oldest_fragment() -> None:
    c = coin(frag("L1", 50_000_000, 0), frag("L2", 50_000_000, 10))
    dest = Output(3, 50_000_000, DEST, account="x", event="d1")
    change = Output(4, 49_990_000, CHANGE, account="w")
    result = traced(tx([c], [dest, change], Leaving("d1", "deposit", 50_000_000)))
    assert by_vout(result) == {3: [("L1", 50_000_000)], 4: [("L2", 49_990_000)]}
    assert result.carries == (FeeCarry("L2", 10_000, 3, "L1"),)


def test_fallback_carry_goes_to_the_destinations_oldest_fragment() -> None:
    c = coin(frag("L1", 300, 0), frag("L2", 300, 1), frag("L3", 1_000, 2))
    dest = Output(0, 600, DEST, account="w2", event="x1")
    change = Output(1, 900, CHANGE, account="w")
    result = traced(tx([c], [dest, change], Leaving("x1", "transfer", 600)))
    assert by_vout(result) == {0: [("L1", 300), ("L2", 300)], 1: [("L3", 900)]}
    # The fee's L3 isn't on the destination: its basis goes to the destination's oldest fragment, L1.
    assert result.carries == (FeeCarry("L3", 100, 0, "L1"),)


def test_identical_destinations_block_when_one_would_take_the_fee_basis() -> None:
    c = coin(frag("L1", 3_000))
    outs = [Output(0, 1_000, DEST, account="x", event="d"), Output(1, 1_000, DEST, account="x", event="d")]
    blocked = trace(tx([c], [*outs, Output(2, 900, CHANGE, account="w")], Leaving("d", "deposit", 2_000)))
    assert blocked == Blocked(
        "t1", "identical_outputs", "outputs 0, 1 would get different basis from the fee"
    )
    # As a sale, the fee isn't carried, so the identical outputs are fine.
    assert isinstance(trace(tx([c], outs, Leaving("d", "sell", 2_000))), Traced)


def test_fee_spanning_two_lots_carries_each() -> None:
    c = coin(frag("L1", 1_000, 0), frag("L2", 5_000, 1), frag("L3", 10_000, 2))
    dest = Output(0, 900, DEST, account="x", event="d1")
    change = Output(1, 10_000, CHANGE, account="w")
    result = traced(tx([c], [dest, change], Leaving("d1", "deposit", 900)))
    # The destination takes L1 900; the fee 5,100: L1 100 then L2 5,000; the change L3.
    assert lots(result.fee) == [("L1", 100), ("L2", 5_000)]
    assert result.carries == (FeeCarry("L1", 100, 0, "L1"), FeeCarry("L2", 5_000, 0, "L1"))


def test_consolidation_fee_goes_first_and_carries_to_the_change() -> None:
    a = coin(frag("L1", 3_000, 0))
    b = coin(frag("L2", 7_000, 1))
    result = traced(tx([b, a], [Output(0, 9_000, CHANGE, account="w")]))
    assert lots(result.fee) == [("L1", 1_000)]
    assert by_vout(result) == {0: [("L1", 2_000), ("L2", 7_000)]}
    assert result.carries == (FeeCarry("L1", 1_000, 0, "L1"),)


@pytest.mark.parametrize("kind", ["sell", "spend", "gift_out"])
def test_disposal_fees_are_not_carried(kind: LeavingKind) -> None:
    result = traced(
        tx([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event="e")], Leaving("e", kind, 9_000))
    )
    assert lots(result.fee) == [("L1", 1_000)]
    assert result.carries == ()


def test_lots_leave_oldest_first_across_coins() -> None:
    newer = coin(frag("L2", 5_000, 5))
    older = coin(frag("L1", 5_000, 1))
    result = traced(tx([newer, older], [Output(0, 6_000, PAY, event="s")], Leaving("s", "sell", 6_000)))
    assert by_vout(result)[0] == [("L1", 5_000), ("L2", 1_000)]


def test_same_moment_lots_order_by_event_id() -> None:
    c = coin(frag("La", 5_000, 0, event="e-2"), frag("Lb", 5_000, 0, event="e-1"))
    result = traced(tx([c], [Output(0, 5_000, PAY, event="s")], Leaving("s", "sell", 5_000)))
    assert by_vout(result)[0] == [("Lb", 5_000)]


def test_parts_of_one_lot_on_two_coins_merge() -> None:
    result = traced(
        tx(
            [coin(frag("L1", 2_000)), coin(frag("L1", 3_000))],
            [Output(0, 4_000, PAY, event="s"), Output(1, 1_000, CHANGE, account="w")],
            Leaving("s", "sell", 4_000),
        )
    )
    assert by_vout(result) == {0: [("L1", 4_000)], 1: [("L1", 1_000)]}
    assert result.fee == ()


def test_several_outputs_of_one_event_fill_largest_first_then_by_script() -> None:
    c = coin(frag("L1", 1_000, 0), frag("L2", 2_000, 1), frag("L3", 3_000, 2))
    outs = [
        Output(0, 1_000, b"\x02", event="s"),
        Output(1, 3_000, b"\x09", event="s"),
        Output(2, 1_000, b"\x01", event="s"),
    ]
    result = traced(tx([c], outs, Leaving("s", "spend", 5_000)))
    # Order: vout 1 (3,000), then vout 2 (script 01), then vout 0 (script 02).
    assert by_vout(result) == {1: [("L1", 1_000), ("L2", 2_000)], 2: [("L3", 1_000)], 0: [("L3", 1_000)]}
    assert lots(result.fee) == [("L3", 1_000)]


def test_zero_value_outputs_get_nothing_and_never_block() -> None:
    result = traced(
        tx(
            [coin(frag("L1", 10_000))],
            [Output(0, 0, b"\x6a"), Output(1, 9_000, PAY, event="s")],
            Leaving("s", "sell", 9_000),
        )
    )
    assert by_vout(result) == {1: [("L1", 9_000)]}


def test_identical_outputs_block_only_when_their_lots_differ() -> None:
    outs = [Output(0, 4_000, PAY, event="s"), Output(1, 4_000, PAY, event="s")]
    c = coin(frag("L1", 4_000), frag("L2", 4_000, 1), frag("L3", 1_000, 2))
    blocked = trace(tx([c], outs, Leaving("s", "sell", 8_000)))
    assert isinstance(blocked, Blocked) and blocked.reason == "identical_outputs"
    single = coin(frag("L1", 9_000))
    assert isinstance(trace(tx([single], outs, Leaving("s", "sell", 8_000))), Traced)


@pytest.mark.parametrize(
    ("inputs", "outputs", "leaving", "reason"),
    [
        ([coin(frag("L1", 10_000), account=None)], [Output(0, 9_000, CHANGE, account="w")], None, "shared"),
        (
            [coin(frag("L1", 10_000), account="w2")],
            [Output(0, 9_000, CHANGE, account="w")],
            None,
            "several_accounts",
        ),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 4_000, PAY, event="a"), Output(1, 4_000, DEST, account="x", event="b")],
            Leaving("a", "spend", 4_000),
            "several_leaving_events",
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY)], None, "unclassified_output"),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, DEST, account="w2")], None, "unclassified_output"),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 6_000, PAY, event="s"), Output(1, 3_000, PAY)],
            Leaving("s", "sell", 6_000),
            "unclassified_output",
        ),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 6_000, PAY, event="s")],
            Leaving("s", "sell", 5_000),
            "amount_mismatch",
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 0, b"\x6a")], None, "fee_only"),
    ],
)
def test_unsupported_cases_block(
    inputs: list[Input], outputs: list[Output], leaving: Leaving | None, reason: str
) -> None:
    result = trace(tx(inputs, outputs, leaving))
    assert isinstance(result, Blocked)
    assert (result.txid, result.reason) == ("t1", reason)


@pytest.mark.parametrize(
    ("inputs", "outputs", "leaving"),
    [
        ([], [Output(0, 1, PAY)], None),
        ([Input("w", 10_000, (frag("L1", 9_000),))], [Output(0, 9_000, CHANGE, account="w")], None),
        ([coin(frag("L1", 10_000))], [Output(0, 11_000, CHANGE, account="w")], None),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 5_000, CHANGE, account="w"), Output(0, 1_000, CHANGE, account="w")],
            None,
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event="s")], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, CHANGE, account="w")], Leaving("s", "sell", 9_000)),
        ([coin(frag("L1", 10_000))], [Output(0, -1, CHANGE, account="w")], None),
        ([coin(frag("L1", 1), frag("L1", 2, 3))], [Output(0, 2, CHANGE, account="w")], None),
    ],
)
def test_inconsistent_input_raises(
    inputs: list[Input], outputs: list[Output], leaving: Leaving | None
) -> None:
    with pytest.raises(EngineError):
        trace(tx(inputs, outputs, leaving))


@st.composite
def wallet_txs(draw: st.DrawFn) -> WalletTx:
    n_lots = draw(st.integers(1, 5))
    fragments = [frag(f"L{i}", draw(st.integers(1, 10_000)), draw(st.integers(0, 3))) for i in range(n_lots)]
    split = draw(st.integers(1, n_lots))
    inputs = [coin(*fragments[:split]), *([coin(*fragments[split:])] if split < n_lots else [])]
    total = sum(f.sats for f in fragments)
    values = draw(st.lists(st.integers(1, total), min_size=1, max_size=4))
    if sum(values) > total:
        values = [total]
    kinds = draw(st.lists(st.booleans(), min_size=len(values), max_size=len(values)))
    outputs = [
        Output(v, value, draw(st.sampled_from([PAY, DEST])), event="e")
        if leave
        else Output(v, value, CHANGE, account="w")
        for v, (value, leave) in enumerate(zip(values, kinds, strict=True))
    ]
    leaving_sats = sum(o.value for o in outputs if o.event)
    leaving = Leaving("e", draw(st.sampled_from(["sell", "deposit"])), leaving_sats) if leaving_sats else None
    return tx(inputs, draw(st.permutations(outputs)), leaving)


@settings(max_examples=300)
@given(wallet_txs(), st.randoms())
def test_output_order_never_changes_the_result(t: WalletTx, rnd: Any) -> None:
    shuffled = list(t.outputs)
    rnd.shuffle(shuffled)
    assert trace(t) == trace(WalletTx(t.txid, t.account, t.inputs, tuple(shuffled), t.leaving))


@settings(max_examples=300)
@given(wallet_txs())
def test_every_output_holds_its_value_and_nothing_is_lost(t: WalletTx) -> None:
    result = trace(t)
    if isinstance(result, Blocked):
        assert result.reason in {"identical_outputs", "fee_only"}
        return
    values = {o.vout: o.value for o in t.outputs}
    for vout, parts in result.outputs:
        assert sum(f.sats for f in parts) == values[vout]
    spent: dict[str, int] = {}
    for i in t.inputs:
        for f in i.fragments:
            spent[f.lot] = spent.get(f.lot, 0) + f.sats
    out: dict[str, int] = {}
    for _, parts in result.outputs:
        for f in parts:
            out[f.lot] = out.get(f.lot, 0) + f.sats
    for f in result.fee:
        out[f.lot] = out.get(f.lot, 0) + f.sats
    assert out == spent
    assert sum(c.sats for c in result.carries) == (sum(f.sats for f in result.fee) if result.carries else 0)

"""Coin tracing through one wallet transaction (ADR 0041 §2 and §3; T-507, T-508, T-509). The two worked
examples are ADR 0041's own; every other figure is worked by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coinacct.tax.engine import EngineError, FeeTreatment
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


def bad(value: Any) -> Any:
    """A value of the wrong type, passed on purpose."""
    return value


def frag(lot: str, sats: int, days: int = 0, event: str | None = None) -> Fragment:
    return Fragment(lot, sats, T0 + timedelta(days=days), event or f"e-{lot}")


def coin(*fragments: Fragment, account: str | None = "w") -> Input:
    return Input(account, sum(f.sats for f in fragments), fragments)


def tx(inputs: list[Input], outputs: list[Output], leaving: Leaving | None = None, **kw: Any) -> WalletTx:
    return WalletTx(kw.get("txid", "t1"), kw.get("account", "w"), tuple(inputs), tuple(outputs), leaving)


def lots(parts: tuple[Fragment, ...]) -> list[tuple[str, int]]:
    return [(f.lot, f.sats) for f in parts]


def traced(t: WalletTx, fee_treatment: FeeTreatment = "carry") -> Traced:
    result = trace(t, fee_treatment)
    assert isinstance(result, Traced), result
    return result


def by_vout(result: Traced) -> dict[int, list[tuple[str, int]]]:
    return {vout: lots(parts) for vout, parts in result.outputs}


# ADR 0041's coin: 1 BTC holding L1 (0.4, oldest) and L2 (0.6).
ADR_COIN = coin(frag("L1", 40_000_000, 0), frag("L2", 60_000_000, 10))


@pytest.mark.parametrize("change_first", [False, True])
def test_adr_worked_example_spend(change_first: bool) -> None:
    pay = Output(0, 50_000_000, PAY, event="s1")
    change = Output(1, 49_990_000, CHANGE, account="w", kind="self_custody")
    outputs = [change, pay] if change_first else [pay, change]
    result = traced(tx([ADR_COIN], outputs, Leaving("s1", "spend", 50_000_000)))
    assert by_vout(result) == {0: [("L1", 40_000_000), ("L2", 10_000_000)], 1: [("L2", 49_990_000)]}
    assert lots(result.fee) == [("L2", 10_000)]
    assert result.carries == ()  # a spend's fee is part of the disposal: no carry


@pytest.mark.parametrize("change_first", [False, True])
def test_adr_worked_example_transfer(change_first: bool) -> None:
    dest = Output(0, 50_000_000, DEST, account="w2", kind="self_custody", event="x1")
    change = Output(1, 49_990_000, CHANGE, account="w", kind="self_custody")
    outputs = [change, dest] if change_first else [dest, change]
    result = traced(tx([ADR_COIN], outputs, Leaving("x1", "transfer", 50_000_000)))
    assert by_vout(result) == {0: [("L1", 40_000_000), ("L2", 10_000_000)], 1: [("L2", 49_990_000)]}
    # The fee's L2 basis passes to the destination's L2 fragment.
    assert result.carries == (FeeCarry("L2", 10_000, 0, "L2"),)


def test_fee_lot_not_on_the_destination_carries_onto_its_oldest_fragment() -> None:
    c = coin(frag("L1", 50_000_000, 0), frag("L2", 50_000_000, 10))
    dest = Output(3, 50_000_000, DEST, account="x", kind="custodial", event="d1")
    change = Output(4, 49_990_000, CHANGE, account="w", kind="self_custody")
    result = traced(tx([c], [dest, change], Leaving("d1", "deposit", 50_000_000)))
    assert by_vout(result) == {3: [("L1", 50_000_000)], 4: [("L2", 49_990_000)]}
    assert result.carries == (FeeCarry("L2", 10_000, 3, "L1"),)


def test_fallback_carry_goes_to_the_destinations_oldest_fragment() -> None:
    c = coin(frag("L1", 300, 0), frag("L2", 300, 1), frag("L3", 1_000, 2))
    dest = Output(0, 600, DEST, account="w2", kind="self_custody", event="x1")
    change = Output(1, 900, CHANGE, account="w", kind="self_custody")
    result = traced(tx([c], [dest, change], Leaving("x1", "transfer", 600)))
    assert by_vout(result) == {0: [("L1", 300), ("L2", 300)], 1: [("L3", 900)]}
    # The fee's L3 isn't on the destination: its basis goes to the destination's oldest fragment, L1.
    assert result.carries == (FeeCarry("L3", 100, 0, "L1"),)


def test_identical_destinations_block_when_one_would_take_the_fee_basis() -> None:
    c = coin(frag("L1", 3_000))
    outs = [
        Output(0, 1_000, DEST, account="x", kind="custodial", event="d"),
        Output(1, 1_000, DEST, account="x", kind="custodial", event="d"),
    ]
    blocked = trace(
        tx(
            [c],
            [*outs, Output(2, 900, CHANGE, account="w", kind="self_custody")],
            Leaving("d", "deposit", 2_000),
        )
    )
    assert blocked == Blocked(
        "t1", "identical_outputs", "outputs 0, 1 would get different basis from the fee"
    )
    # With the fee disposed of, nothing is carried, so the identical outputs are fine.
    assert isinstance(trace(tx([c], outs, Leaving("d", "deposit", 2_000)), "dispose"), Traced)


def test_identical_outputs_across_roles_count_as_identical() -> None:
    c = coin(frag("L1", 1_000), frag("L2", 1_000, 1))
    outs = [
        Output(0, 1_000, CHANGE, account="w2", kind="self_custody", event="x"),
        Output(1, 1_000, CHANGE, account="w", kind="self_custody"),
    ]
    blocked = trace(tx([c, coin(frag("L3", 100, 2))], outs, Leaving("x", "transfer", 1_000)))
    assert blocked == Blocked("t1", "identical_outputs", "outputs 0, 1 would get different lots")


def test_the_dispose_setting_carries_nothing() -> None:
    result = traced(
        tx(
            [ADR_COIN],
            [Output(0, 99_990_000, DEST, account="w2", kind="self_custody", event="x")],
            Leaving("x", "transfer", 99_990_000),
        ),
        "dispose",
    )
    assert lots(result.fee) == [("L2", 10_000)]
    assert result.carries == ()


def test_fee_spanning_two_lots_carries_each() -> None:
    c = coin(frag("L1", 1_000, 0), frag("L2", 5_000, 1), frag("L3", 10_000, 2))
    dest = Output(0, 900, DEST, account="x", kind="custodial", event="d1")
    change = Output(1, 10_000, CHANGE, account="w", kind="self_custody")
    result = traced(tx([c], [dest, change], Leaving("d1", "deposit", 900)))
    # The destination takes L1 900; the fee 5,100: L1 100 then L2 5,000; the change L3.
    assert lots(result.fee) == [("L1", 100), ("L2", 5_000)]
    assert result.carries == (FeeCarry("L1", 100, 0, "L1"), FeeCarry("L2", 5_000, 0, "L1"))


def test_consolidation_fee_goes_first_and_carries_to_the_change() -> None:
    a = coin(frag("L1", 3_000, 0))
    b = coin(frag("L2", 7_000, 1))
    result = traced(tx([b, a], [Output(0, 9_000, CHANGE, account="w", kind="self_custody")]))
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
            [Output(0, 4_000, PAY, event="s"), Output(1, 1_000, CHANGE, account="w", kind="self_custody")],
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


def test_a_deposit_must_reach_an_exchange_and_a_transfer_a_wallet() -> None:
    c = [coin(frag("L1", 10_000))]
    to_wallet = Output(0, 9_000, DEST, account="w2", event="d", kind="self_custody")
    to_exchange = Output(0, 9_000, DEST, account="x", event="d", kind="custodial")
    for out, kind in ((to_wallet, "deposit"), (to_exchange, "transfer")):
        result = trace(tx(c, [out], Leaving("d", bad(kind), 9_000)))
        assert result == Blocked("t1", "mismatched_owner", f"outputs 0 aren't owned as a {kind} needs")
    assert isinstance(trace(tx(c, [to_exchange], Leaving("d", "deposit", 9_000))), Traced)
    assert isinstance(trace(tx(c, [to_wallet], Leaving("d", "transfer", 9_000))), Traced)


def test_every_group_of_identical_outputs_is_named() -> None:
    c = coin(
        frag("L1", 1_000),
        frag("L2", 1_000, 1),
        frag("L3", 2_000, 2),
        frag("L4", 1_500, 3),
        frag("L5", 600, 4),
    )
    outs = [
        Output(0, 2_000, PAY, event="s"),
        Output(3, 2_000, PAY, event="s"),
        Output(1, 1_000, b"\x01", event="s"),
        Output(2, 1_000, b"\x01", event="s"),
    ]
    result = trace(tx([c], outs, Leaving("s", "sell", 6_000)))
    assert result == Blocked("t1", "identical_outputs", "outputs 0, 1, 2, 3 would get different lots")


def test_block_details_name_the_inputs_and_outputs() -> None:
    shared = trace(
        tx(
            [coin(frag("L1", 5_000)), Input(None, 5_000, ())],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
        )
    )
    assert shared == Blocked("t1", "shared", "inputs 1 aren't the user's")
    outs = [
        Output(4, 3_000, PAY, event="a"),
        Output(1, 3_000, DEST, account="x", event="b", kind="custodial"),
    ]
    several = trace(tx([coin(frag("L1", 10_000))], outs, Leaving("a", "spend", 3_000)))
    assert several == Blocked("t1", "several_leaving_events", "events a, b on outputs 1, 4")


def test_a_zero_value_coin_can_be_spent() -> None:
    empty = Input("w", 0, ())
    result = traced(
        tx([empty, coin(frag("L1", 1_000))], [Output(0, 900, PAY, event="s")], Leaving("s", "sell", 900))
    )
    assert by_vout(result) == {0: [("L1", 900)]}


@pytest.mark.parametrize(
    ("inputs", "outputs", "leaving", "reason"),
    [
        (
            [Input(None, 10_000, ())],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
            "shared",
        ),
        (
            [coin(frag("L1", 10_000)), Input(None, 5_000, ())],
            [Output(0, 14_000, CHANGE, account="w", kind="self_custody")],
            None,
            "shared",
        ),
        (
            [coin(frag("L1", 10_000), account="w2")],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
            "several_accounts",
        ),
        (
            [coin(frag("L1", 10_000))],
            [
                Output(0, 4_000, PAY, event="a"),
                Output(1, 4_000, DEST, account="x", kind="custodial", event="b"),
            ],
            Leaving("a", "spend", 4_000),
            "several_leaving_events",
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY)], None, "unclassified_output"),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 9_000, DEST, account="w2", kind="self_custody")],
            None,
            "unclassified_output",
        ),
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
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 9_000, PAY, event="x")],
            Leaving("x", "transfer", 9_000),
            "mismatched_owner",
        ),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 9_000, DEST, account="w", kind="self_custody", event="x")],
            Leaving("x", "deposit", 9_000),
            "mismatched_owner",
        ),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 9_000, DEST, account="x", kind="custodial", event="s")],
            Leaving("s", "sell", 9_000),
            "mismatched_owner",
        ),
    ],
)
def test_unsupported_cases_block(
    inputs: list[Input], outputs: list[Output], leaving: Leaving | None, reason: str
) -> None:
    result = trace(tx(inputs, outputs, leaving))
    assert isinstance(result, Blocked)
    assert (result.txid, result.reason) == ("t1", reason)
    # A block, message included, doesn't depend on the order of the outputs.
    assert trace(tx(inputs, outputs[::-1], leaving)) == result


def test_a_block_names_every_output_involved_in_order() -> None:
    outs = [
        Output(5, 3_000, PAY),
        Output(2, 3_000, DEST),
        Output(7, 3_000, CHANGE, account="w", kind="self_custody"),
    ]
    expected = Blocked("t1", "unclassified_output", "outputs 2, 5 have no recorded event")
    assert trace(tx([coin(frag("L1", 10_000))], outs)) == expected
    assert trace(tx([coin(frag("L1", 10_000))], outs[::-1])) == expected


@pytest.mark.parametrize(
    ("inputs", "outputs", "leaving"),
    [
        ([], [Output(0, 1, PAY)], None),
        (
            [Input("w", 10_000, (frag("L1", 9_000),))],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 11_000, CHANGE, account="w", kind="self_custody")], None),
        (
            [coin(frag("L1", 10_000))],
            [
                Output(0, 5_000, CHANGE, account="w", kind="self_custody"),
                Output(0, 1_000, CHANGE, account="w", kind="self_custody"),
            ],
            None,
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event="s")], None),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            Leaving("s", "sell", 9_000),
        ),
        ([coin(frag("L1", 10_000))], [Output(0, -1, CHANGE, account="w", kind="self_custody")], None),
        (
            [coin(frag("L1", 1), frag("L1", 2, 3))],
            [Output(0, 2, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        (
            [Input(None, 10_000, (frag("L1", 10_000),))],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        (
            [coin(Fragment("L1", 10_000, datetime(2024, 1, 1), "e"))],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, bad(0.0), b"\x6a"), Output(1, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, bad(False), b"\x6a"), Output(1, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        ([coin(frag("L1", 10_000))], [Output(-1, 9_000, CHANGE, account="w", kind="self_custody")], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, bad("chg"), account="w", kind="self_custody")], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event="s")], Leaving("s", bad("Sell"), 9_000)),
        ([bad(object())], [Output(0, 1, PAY)], None),
        ([Input(bad(1), 10_000, ())], [Output(0, 9_000, PAY)], None),
        (
            [coin(Fragment("", 10_000, T0, "e"))],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        (
            [coin(Fragment("L1", 10_000, T0, bad(None)))],
            [Output(0, 9_000, CHANGE, account="w", kind="self_custody")],
            None,
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, CHANGE, account="w")], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, kind="custodial")], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event=bad(7))], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, account=bad(5), kind="custodial")], None),
        ([coin(frag("L1", 10_000))], [bad(object())], None),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event="s")], bad(("s", "sell", 9_000))),
        (
            [coin(frag("L1", 10_000))],
            [Output(0, 0, b"\x6a", event="s"), Output(1, 9_000, PAY, event="s")],
            Leaving("s", "sell", 9_000),
        ),
        ([coin(frag("L1", 10_000))], [Output(0, 9_000, PAY, event="s")], Leaving(bad(3), "sell", 9_000)),
    ],
)
def test_inconsistent_input_raises(
    inputs: list[Input], outputs: list[Output], leaving: Leaving | None
) -> None:
    with pytest.raises(EngineError):
        trace(tx(inputs, outputs, leaving))


def test_a_transaction_that_isnt_a_wallet_tx_raises() -> None:
    with pytest.raises(EngineError):
        trace(bad("t1"))
    with pytest.raises(EngineError):
        trace(WalletTx("t1", "", (coin(frag("L1", 1)),), ()))


def test_an_unknown_fee_treatment_raises() -> None:
    with pytest.raises(EngineError):
        trace(
            tx([coin(frag("L1", 10_000))], [Output(0, 9_000, CHANGE, account="w", kind="self_custody")]),
            bad("keep"),
        )


KINDS: list[LeavingKind] = ["sell", "spend", "gift_out", "deposit", "transfer"]


@st.composite
def wallet_txs(draw: st.DrawFn) -> WalletTx:
    n_lots = draw(st.integers(1, 5))
    fragments = [frag(f"L{i}", draw(st.integers(2, 10_000)), draw(st.integers(0, 3))) for i in range(n_lots)]
    if draw(st.booleans()):  # put part of the first lot on a second coin
        half = fragments[0].sats // 2
        first = fragments[0]
        fragments[0] = Fragment(first.lot, first.sats - half, first.entered, first.event)
        fragments.append(Fragment(first.lot, half, first.entered, first.event))
    split = draw(st.integers(1, len(fragments)))
    inputs = [coin(*fragments[:split]), *([coin(*fragments[split:])] if split < len(fragments) else [])]
    total = sum(f.sats for f in fragments)
    values = draw(st.lists(st.integers(1, total), min_size=1, max_size=4))
    if sum(values) > total:
        values = [total]
    kind = draw(st.sampled_from(KINDS))
    owner, owner_kind = {"deposit": ("x", "custodial"), "transfer": ("w2", "self_custody")}.get(
        kind, (None, None)
    )
    leaves = draw(st.lists(st.booleans(), min_size=len(values), max_size=len(values)))
    outputs = [
        Output(v, value, draw(st.sampled_from([PAY, DEST])), account=owner, event="e", kind=bad(owner_kind))
        if leave
        else Output(v, value, CHANGE, account="w", kind="self_custody")
        for v, (value, leave) in enumerate(zip(values, leaves, strict=True))
    ]
    if draw(st.booleans()):
        outputs.append(Output(len(outputs), 0, b"\x6a"))
    leaving_sats = sum(o.value for o in outputs if o.event)
    leaving = Leaving("e", kind, leaving_sats) if leaving_sats else None
    return tx(inputs, draw(st.permutations(outputs)), leaving)


def _shuffled(t: WalletTx, rnd: Any) -> WalletTx:
    outputs = list(t.outputs)
    rnd.shuffle(outputs)
    inputs = []
    for i in t.inputs:
        parts = list(i.fragments)
        rnd.shuffle(parts)
        inputs.append(Input(i.account, i.value, tuple(parts)))
    rnd.shuffle(inputs)
    return WalletTx(t.txid, t.account, tuple(inputs), tuple(outputs), t.leaving)


@settings(max_examples=300)
@given(wallet_txs(), st.randoms(), st.sampled_from(["carry", "dispose"]))
def test_the_order_of_outputs_inputs_and_lots_never_changes_the_result(
    t: WalletTx, rnd: Any, fee_treatment: FeeTreatment
) -> None:
    assert trace(t, fee_treatment) == trace(_shuffled(t, rnd), fee_treatment)


@settings(max_examples=300)
@given(wallet_txs(), st.randoms(), st.sampled_from([None, "w2"]))
def test_a_foreign_input_blocks_whatever_the_order(t: WalletTx, rnd: Any, owner: str | None) -> None:
    foreign = Input(owner, 1_000, () if owner is None else (frag("F1", 1_000),))
    with_foreign = WalletTx(t.txid, t.account, (*t.inputs, foreign), t.outputs, t.leaving)
    result = trace(with_foreign)
    assert isinstance(result, Blocked)
    assert result.reason == ("shared" if owner is None else "several_accounts")
    if owner is not None:
        assert trace(_shuffled(with_foreign, rnd)) == result


@settings(max_examples=300)
@given(wallet_txs(), st.sampled_from(["carry", "dispose"]))
def test_every_output_holds_its_value_and_nothing_is_lost(t: WalletTx, fee_treatment: FeeTreatment) -> None:
    result = trace(t, fee_treatment)
    paid = [o for o in t.outputs if o.value > 0]
    if isinstance(result, Blocked):
        assert result.reason in {"identical_outputs", "fee_only"}
        if result.reason == "identical_outputs":
            keys = [(o.value, o.script) for o in paid]
            assert len(set(keys)) < len(keys)
        return
    # Lots leave oldest first: the leaving outputs (canonical order), then the fee, then the change.
    order = sorted((o for o in paid if o.event), key=lambda o: (-o.value, o.script))
    order_change = sorted((o for o in paid if not o.event), key=lambda o: (-o.value, o.script))
    held = dict(result.outputs)
    taken = (
        [f for o in order for f in held[o.vout]]
        + list(result.fee)
        + [f for o in order_change for f in held[o.vout]]
    )
    assert [f.lot for f in taken] == sorted((f.lot for f in taken), key=lambda lot: (_entered(t, lot), lot))
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
    carried = (t.leaving is None or t.leaving.kind in ("deposit", "transfer")) and fee_treatment == "carry"
    assert bool(result.carries) == (bool(result.fee) and carried)
    if result.carries:
        assert [(c.lot, c.sats) for c in result.carries] == lots(result.fee)
        for c in result.carries:
            assert any(f.lot == c.onto for f in held[c.vout])


def _entered(t: WalletTx, lot: str) -> tuple[datetime, str]:
    f = next(f for i in t.inputs for f in i.fragments if f.lot == lot)
    return (f.entered, f.event)

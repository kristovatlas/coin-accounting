"""The lot engine in coin-traced wallets (ADR 0041 §1-§3, ADR 0043): links put lots on coins, and a
wallet transaction's lots follow the coins it spends (`tracing.trace`). Hand-worked figures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from coinacct.tax.engine import (
    Acquisition,
    Coin,
    CoinLot,
    Disposal,
    EngineError,
    Event,
    Moved,
    Spent,
    Transfer,
    Untraced,
    WalletTx,
    run,
)
from coinacct.tax.tracing import Output

BTC = 100_000_000
D = Decimal
DAY = date(2025, 3, 1)
AT = datetime(2025, 3, 1, 12, tzinfo=UTC)
PAY = b"\x00\x14pay"
CHANGE = b"\x00\x14chg"
DEST = b"\x00\x14dst"
WALLETS = frozenset({"w", "v"})

# ADR 0041 §2's coin: 1 BTC, holding L1 (0.4, oldest, $10,000) and L2 (0.6, $30,000)
COIN: list[Event] = [
    Acquisition(
        "L1", "w", date(2024, 1, 1), "buy", 40_000_000, D("10000.00"), coins=(Coin("c0:0", 40_000_000),)
    ),
    Acquisition(
        "L2", "w", date(2024, 2, 1), "buy", 60_000_000, D("30000.00"), coins=(Coin("c0:0", 60_000_000),)
    ),
]
FEE = 10_000  # 0.0001 BTC


def wtx(leaving: Disposal | Transfer | None, outputs: list[Output], **kw: Any) -> WalletTx:
    inputs = kw.pop("inputs", (Spent("c0:0", BTC, "w"),))
    return WalletTx(
        kw.pop("id", "tx1"),
        kw.pop("account", "w"),
        DAY,
        AT,
        kw.pop("txid", "t1"),
        inputs,
        tuple(outputs),
        leaving,
    )


def change(vout: int, sats: int, account: str = "w") -> Output:
    return Output(vout, sats, CHANGE, account=account, kind="self_custody")


@pytest.mark.parametrize("change_first", [False, True])
def test_adr0041_first_example_a_spend_takes_the_oldest_lots_and_the_fee(change_first: bool) -> None:
    # 0.5 BTC for goods worth $50,000, 0.4999 change, a 0.0001 fee: the payment gets L1 0.4 and L2 0.1,
    # the fee L2 0.0001 (part of the disposal), the change keeps L2 0.4999. Basis $15,005, whichever
    # output comes first.
    pay_v, chg_v = (1, 0) if change_first else (0, 1)
    spend = Disposal("s", "w", DAY, AT, "spend", BTC // 2, D("50000.00"))
    outs = [Output(pay_v, BTC // 2, PAY, event="s"), change(chg_v, 49_990_000)]
    result = run([*COIN, wtx(spend, outs)], wallets=WALLETS)
    assert [(a.lot, a.sats, a.basis) for a in result.allocations] == [
        ("L1", 40_000_000, D("10000.00")),
        ("L2", 10_010_000, D("5005.00")),
    ]
    assert sum((a.proceeds for a in result.allocations), D("0")) == D("50000.00")
    assert result.coins == (CoinLot(f"t1:{chg_v}", "L2", 49_990_000),)
    assert result.blocking == ()


def test_adr0041_second_example_a_transfer_carries_the_fee_basis_to_the_destination() -> None:
    # 0.5 BTC to wallet v, 0.4999 change, a 0.0001 fee: v gets L1 0.4 and L2 0.1; the fee's L2 ($5)
    # passes to v's L2 fragment, which holds 0.1 with $5,005. No disposal.
    move = Transfer("x", "w", "v", DAY, AT, "self_transfer", BTC // 2 + FEE, FEE)
    outs = [Output(0, BTC // 2, DEST, account="v", kind="self_custody", event="x"), change(1, 49_990_000)]
    result = run([*COIN, wtx(move, outs)], wallets=WALLETS)
    assert result.allocations == ()
    assert result.moves == (
        Moved("x", "L1", "L1@x", "v", 40_000_000, D("10000.00"), date(2024, 1, 1)),
        Moved("x", "L2", "L2@x", "v", 10_000_000, D("5005.00"), date(2024, 2, 1), FEE),
    )
    assert set(result.coins) == {
        CoinLot("t1:0", "L1@x", 40_000_000),
        CoinLot("t1:0", "L2@x", 10_000_000),
        CoinLot("t1:1", "L2", 49_990_000),
    }
    held = {lot.id: (lot.sats, lot.basis) for lot in result.holdings}
    assert held == {
        "L2": (49_990_000, D("24995.00")),  # 30,000 less the 5,005 that moved
        "L1@x": (40_000_000, D("10000.00")),
        "L2@x": (10_000_000, D("5005.00")),
    }


def test_the_moved_coin_is_spent_from_the_destination_with_its_lots() -> None:
    move = Transfer("x", "w", "v", DAY, AT, "self_transfer", BTC // 2 + FEE, FEE)
    outs = [Output(0, BTC // 2, DEST, account="v", kind="self_custody", event="x"), change(1, 49_990_000)]
    sale = Disposal("s", "v", DAY, AT, "sell", BTC // 2 - FEE, D("40000.00"))
    second = wtx(
        sale,
        [Output(0, BTC // 2 - FEE, PAY, event="s")],
        id="tx2",
        account="v",
        txid="t2",
        inputs=(Spent("t1:0", BTC // 2, "v"),),
    )
    result = run([*COIN, wtx(move, outs), second], wallets=WALLETS)
    # the sale and its fee take all of v's coin: L1@x then L2@x, with all their basis
    assert [(a.lot, a.sats, a.basis) for a in result.allocations] == [
        ("L1@x", 40_000_000, D("10000.00")),
        ("L2@x", 10_000_000, D("5005.00")),
    ]
    assert [c.outpoint for c in result.coins] == ["t1:1"]


def test_a_deposit_moves_the_coins_lots_to_the_exchange_account() -> None:
    deposit = Transfer("d", "w", "x", DAY, AT, "deposit", BTC // 2 + FEE, FEE)
    outs = [Output(0, BTC // 2, DEST, account="x", kind="custodial", event="d"), change(1, 49_990_000)]
    result = run([*COIN, wtx(deposit, outs)], wallets=WALLETS)
    assert [(m.into, m.sats, m.basis, m.carried_fee) for m in result.moves] == [
        ("L1@d", 40_000_000, D("10000.00"), 0),
        ("L2@d", 10_000_000, D("5005.00"), FEE),
    ]
    assert result.coins == (CoinLot("t1:1", "L2", 49_990_000),)  # the exchange account has no coins


def test_a_consolidation_keeps_the_fee_basis_in_the_wallet() -> None:
    # two coins, one change output: the fee takes L1's first sats (oldest first, the fee before the
    # change); its basis stays with L1 on the change, and nothing is disposed of
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 1_000, D("10.00"), coins=(Coin("a:0", 1_000),)),
        Acquisition("L2", "w", date(2024, 2, 1), "buy", 1_000, D("30.00"), coins=(Coin("b:0", 1_000),)),
        wtx(None, [change(0, 1_900)], inputs=(Spent("a:0", 1_000, "w"), Spent("b:0", 1_000, "w"))),
    ]
    result = run(events, wallets=WALLETS)
    assert result.allocations == ()
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {
        "L1": (900, D("10.00")),
        "L2": (1_000, D("30.00")),
    }
    assert set(result.coins) == {CoinLot("t1:0", "L1", 900), CoinLot("t1:0", "L2", 1_000)}


def test_a_withdrawal_links_its_moved_lots_to_the_wallets_coin() -> None:
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 1_000, D("10.00")),
        Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 900),)),
    ]
    result = run(events, wallets=WALLETS)
    assert result.coins == (CoinLot("c:0", "e1@wd", 900),)


def test_a_gifts_fee_basis_isnt_recovered() -> None:
    gift = Disposal("g", "w", DAY, AT, "gift_out", BTC - FEE, D("0.00"))
    result = run([*COIN, wtx(gift, [Output(0, BTC - FEE, PAY, event="g")])], wallets=WALLETS)
    assert [(g.lot, g.sats, g.basis) for g in result.gifts] == [
        ("L1", 40_000_000, D("10000.00")),
        ("L2", 59_990_000, D("29995.00")),
    ]
    assert result.allocations == ()
    assert result.holdings == ()


def test_a_coin_without_links_blocks_and_the_rest_of_the_wallet_too() -> None:
    # the coin's lots aren't linked: the spend blocks (ADR 0041 §3), its figures by FIFO meanwhile, and
    # the wallet's later transactions block as well
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", BTC, D("10000.00")),
        wtx(
            Disposal("s", "w", DAY, AT, "spend", BTC // 2, D("1.00")),
            [Output(0, BTC // 2, PAY, event="s"), change(1, BTC // 2)],
        ),
        wtx(None, [change(0, BTC // 2)], id="tx2", txid="t2", inputs=(Spent("t1:1", BTC // 2, "w"),)),
    ]
    result = run(events, wallets=WALLETS)
    assert [(b.event, b.reason) for b in result.blocking if isinstance(b, Untraced)] == [
        ("tx1", "uncovered"),
        ("tx2", "after_untraced"),
    ]
    assert [(a.lot, a.sats) for a in result.allocations] == [("L1", BTC // 2)]


def test_a_shared_transaction_blocks() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", BTC, D("1.00"))
    inputs = (Spent("c0:0", BTC, "w"), Spent("f:0", 1_000, None))
    result = run([*COIN, wtx(spend, [Output(0, BTC, PAY, event="s")], inputs=inputs)], wallets=WALLETS)
    (blocked,) = [b for b in result.blocking if isinstance(b, Untraced)]
    assert (blocked.event, blocked.reason) == ("tx1", "shared")


def test_a_self_transfer_to_an_untraced_wallet_blocks() -> None:
    move = Transfer("x", "w", "z", DAY, AT, "self_transfer", BTC, FEE)
    outs = [Output(0, BTC - FEE, DEST, account="z", kind="self_custody", event="x")]
    result = run([*COIN, wtx(move, outs)], wallets=WALLETS)
    assert [b.reason for b in result.blocking if isinstance(b, Untraced)] == ["mixed_transfer"]


def test_links_over_the_sats_that_arrived_block() -> None:
    bad = Acquisition(
        "L1", "w", date(2024, 1, 1), "buy", 1_000, D("1.00"), coins=(Coin("a:0", 600), Coin("b:0", 600))
    )
    result = run([bad], wallets=WALLETS)
    assert result.blocking == (
        Untraced("L1", "bad_link", "its links hold 1200 sats, more than the 1000 that arrived"),
    )
    assert result.coins == ()


def test_a_coin_holding_more_lots_than_its_value_blocks() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", 900, D("1.00"))
    tx = wtx(spend, [Output(0, 900, PAY, event="s")], inputs=(Spent("c0:0", 1_000, "w"),))
    result = run([*COIN, tx], wallets=WALLETS)
    assert [b.reason for b in result.blocking if isinstance(b, Untraced)] == ["over_value"]


@pytest.mark.parametrize(
    ("events", "match"),
    [
        ([*COIN, Disposal("s", "w", DAY, AT, "sell", 1, D("1.00"))], "come as its WalletTx"),
        ([*COIN, Transfer("d", "w", "x", DAY, AT, "deposit", 1_000, 0)], "come as its WalletTx"),
        (
            [Acquisition("a", "x", DAY, "buy", 1, D("1.00"), coins=(Coin("a:0", 1),))],
            "only a coin-traced wallet",
        ),
        ([*COIN, wtx(None, [change(0, BTC)], account="x")], "isn't a coin-traced wallet"),
        (
            [
                *COIN,
                wtx(
                    Disposal("s", "w", DAY, AT, "sell", BTC - FEE, D("1.00"), picks=()),
                    [Output(0, BTC - FEE, PAY, event="s")],
                ),
            ],
            "identify the lots",
        ),
        (
            [
                *COIN,
                wtx(
                    Transfer("x", "w", "v", DAY, AT, "self_transfer", BTC, 5),
                    [Output(0, BTC - FEE, DEST, account="v", kind="self_custody", event="x")],
                ),
            ],
            "fee_sats is the transaction's fee",
        ),
        (
            [
                *COIN,
                wtx(
                    Transfer("x", "w", "v", DAY, AT, "deposit", BTC, FEE),
                    [Output(0, BTC - FEE, DEST, account="v", kind="self_custody", event="x")],
                ),
            ],
            "not a wallet",
        ),
        (
            [*COIN, wtx(None, [change(0, BTC)], inputs=(Spent("c0:0", BTC, "w"), Spent("c0:0", BTC, "w")))],
            "spent once",
        ),
    ],
)
def test_invalid_wallet_events_raise(events: list[Event], match: str) -> None:
    with pytest.raises(EngineError, match=match):
        run(events, wallets=WALLETS)


def test_wallet_transactions_dont_stop_the_replay() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", BTC // 2, D("50000.00"))
    outs = [Output(0, BTC // 2, PAY, event="s"), change(1, 49_990_000)]
    assert run([*COIN, wtx(spend, outs)], wallets=WALLETS).replay_stopped is None


TWO: list[Event] = [
    Acquisition("L1", "w", date(2024, 1, 1), "buy", 500, D("10.00"), coins=(Coin("c0:0", 500),)),
    Acquisition("L2", "w", date(2024, 2, 1), "buy", 500, D("30.00"), coins=(Coin("c0:0", 500),)),
]


def test_a_fee_lot_not_on_the_destination_passes_its_basis_to_the_oldest_moved_part() -> None:
    # 500 to v, a 100 fee, 400 change: v gets L1 500; the fee takes L2 100 ($6.00), which no
    # destination output holds, so it joins L1@x (ADR 0041 §2.4): $16.00. The change keeps L2 400, $24.00.
    move = Transfer("x", "w", "v", DAY, AT, "self_transfer", 600, 100)
    outs = [Output(0, 500, DEST, account="v", kind="self_custody", event="x"), change(1, 400)]
    result = run([*TWO, wtx(move, outs, inputs=(Spent("c0:0", 1_000, "w"),))], wallets=WALLETS)
    assert result.moves == (Moved("x", "L1", "L1@x", "v", 500, D("16.00"), date(2024, 1, 1), 100, ("L2",)),)
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {
        "L2": (400, D("24.00")),
        "L1@x": (500, D("16.00")),
    }


def test_a_consolidations_fee_lot_off_the_change_passes_its_basis_onto_it() -> None:
    # The fee takes L1 first (all 100 of it); the change holds only L2, whose oldest fragment takes
    # L1's $10.00: L2 1,000 sats, $40.00. No disposal.
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 100, D("10.00"), coins=(Coin("a:0", 100),)),
        Acquisition("L2", "w", date(2024, 2, 1), "buy", 1_000, D("30.00"), coins=(Coin("b:0", 1_000),)),
        wtx(None, [change(0, 1_000)], inputs=(Spent("a:0", 100, "w"), Spent("b:0", 1_000, "w"))),
    ]
    result = run(events, wallets=WALLETS)
    assert result.allocations == ()
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {"L2": (1_000, D("40.00"))}


def test_a_transaction_spending_two_wallets_coins_blocks() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", BTC, D("1.00"))
    inputs = (Spent("c0:0", BTC, "w"), Spent("v:0", 1_000, "v"))
    result = run([*COIN, wtx(spend, [Output(0, BTC, PAY, event="s")], inputs=inputs)], wallets=WALLETS)
    assert [b.reason for b in result.blocking if isinstance(b, Untraced)] == ["several_accounts"]


def bad_wallet_tx(**kw: Any) -> WalletTx:
    fields: dict[str, Any] = {
        "id": "tx1",
        "account": "w",
        "on": DAY,
        "at": AT,
        "txid": "t1",
        "inputs": (Spent("c0:0", BTC, "w"),),
        "outputs": (change(0, BTC),),
        "leaving": None,
    }
    fields.update(kw)
    return WalletTx(**fields)


@pytest.mark.parametrize(
    ("event", "match"),
    [
        (bad_wallet_tx(txid=""), "names its txid"),
        (bad_wallet_tx(txid="a:b"), "names its txid"),
        (bad_wallet_tx(inputs=()), "at least one input"),
        (bad_wallet_tx(outputs=[change(0, BTC)]), "are tuples"),
        (bad_wallet_tx(inputs=("c0:0",)), "a Spent naming its outpoint"),
        (bad_wallet_tx(inputs=(Spent("c0", BTC, "w"),)), "a Spent naming its outpoint"),
        (bad_wallet_tx(inputs=(Spent("c0:0", BTC, ""),)), "a name or None"),
        (bad_wallet_tx(leaving="s"), "a Disposal or a Transfer"),
        (
            bad_wallet_tx(leaving=Disposal("s", "v", DAY, AT, "spend", 1, D("1.00"))),
            "its account, date and moment",
        ),
        (bad_wallet_tx(leaving=Disposal("tx1", "w", DAY, AT, "spend", 1, D("1.00"))), "appears twice"),
        (
            bad_wallet_tx(leaving=Transfer("x", "w", "v", DAY, AT, "withdrawal", BTC, 0)),
            "a deposit or a self-transfer",
        ),
        (
            Transfer("d", "x", "y", DAY, AT, "deposit", 1, 0, coins=(Coin("a:0", 1),)),
            "only a withdrawal links",
        ),
    ],
)
def test_malformed_wallet_events_raise(event: Event, match: str) -> None:
    with pytest.raises(EngineError, match=match):
        run([*COIN, event], wallets=WALLETS)


@pytest.mark.parametrize(
    ("coins", "match"),
    [
        ([Coin("a:0", 1)], "a tuple of Coins"),
        (("a:0",), "a Coin naming an outpoint"),
        ((Coin("a", 1),), "a Coin naming an outpoint"),
        ((Coin("a:0", 1), Coin("a:0", 1)), "linked once per event"),
    ],
)
def test_malformed_links_raise(coins: Any, match: str) -> None:
    with pytest.raises(EngineError, match=match):
        run([Acquisition("L1", "w", DAY, "buy", 10, D("1.00"), coins=coins)], wallets=WALLETS)


def test_wallets_is_a_frozenset_of_names() -> None:
    with pytest.raises(EngineError, match="frozenset"):
        run([], wallets={"w"})  # type: ignore[arg-type]

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
    MissingLots,
    Moved,
    Pick,
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
        "L1", "w", date(2024, 1, 1), "buy", 40_000_000, D("10000.00"), coins=(Coin("c0:0", 40_000_000, BTC),)
    ),
    Acquisition(
        "L2", "w", date(2024, 2, 1), "buy", 60_000_000, D("30000.00"), coins=(Coin("c0:0", 60_000_000, BTC),)
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
    # the fee L2 0.0001, the change keeps L2 0.4999. The fee's $5 joins the L2 row (ADR 0043): basis
    # $15,005, proceeds $50,000 split by the sats received, whichever output comes first.
    pay_v, chg_v = (1, 0) if change_first else (0, 1)
    spend = Disposal("s", "w", DAY, AT, "spend", BTC // 2, D("50000.00"))
    outs = [Output(pay_v, BTC // 2, PAY, event="s"), change(chg_v, 49_990_000)]
    result = run([*COIN, wtx(spend, outs)], wallets=WALLETS)
    assert [(a.lot, a.sats, a.basis, a.proceeds, a.carried_fee) for a in result.allocations] == [
        ("L1", 40_000_000, D("10000.00"), D("40000.00"), 0),
        ("L2", 10_000_000, D("5005.00"), D("10000.00"), FEE),
    ]
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
    # the sale and its fee take all of v's coin: L1@x then L2@x, the fee's L2@x sats joining that row
    assert [(a.lot, a.sats, a.basis, a.carried_fee) for a in result.allocations] == [
        ("L1@x", 40_000_000, D("10000.00"), 0),
        ("L2@x", 9_990_000, D("5005.00"), FEE),
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
        Acquisition(
            "L1", "w", date(2024, 1, 1), "buy", 1_000, D("10.00"), coins=(Coin("a:0", 1_000, 1_000),)
        ),
        Acquisition(
            "L2", "w", date(2024, 2, 1), "buy", 1_000, D("30.00"), coins=(Coin("b:0", 1_000, 1_000),)
        ),
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
        Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 900, 900),)),
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
        "L1",
        "w",
        date(2024, 1, 1),
        "buy",
        1_000,
        D("1.00"),
        coins=(Coin("a:0", 600, 600), Coin("b:0", 600, 600)),
    )
    result = run([bad], wallets=WALLETS)
    assert result.blocking == (
        Untraced("L1", "bad_link", "its links hold 1200 sats, more than the 1000 that arrived"),
    )
    assert result.coins == ()


def test_a_coin_spent_at_another_value_than_its_links_raises() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", 900, D("1.00"))
    tx = wtx(spend, [Output(0, 900, PAY, event="s")], inputs=(Spent("c0:0", 1_000, "w"),))
    with pytest.raises(EngineError, match="another value in its links"):
        run([*COIN, tx], wallets=WALLETS)


@pytest.mark.parametrize(
    ("events", "match"),
    [
        ([*COIN, Disposal("s", "w", DAY, AT, "sell", 1, D("1.00"))], "come as its WalletTx"),
        ([*COIN, Transfer("d", "w", "x", DAY, AT, "deposit", 1_000, 0)], "come as its WalletTx"),
        (
            [Acquisition("a", "x", DAY, "buy", 1, D("1.00"), coins=(Coin("a:0", 1, 1),))],
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
    Acquisition("L1", "w", date(2024, 1, 1), "buy", 500, D("10.00"), coins=(Coin("c0:0", 500, 1_000),)),
    Acquisition("L2", "w", date(2024, 2, 1), "buy", 500, D("30.00"), coins=(Coin("c0:0", 500, 1_000),)),
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
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 100, D("10.00"), coins=(Coin("a:0", 100, 100),)),
        Acquisition(
            "L2", "w", date(2024, 2, 1), "buy", 1_000, D("30.00"), coins=(Coin("b:0", 1_000, 1_000),)
        ),
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
            Transfer("d", "x", "y", DAY, AT, "deposit", 1, 0, coins=(Coin("a:0", 1, 1),)),
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
        ([Coin("a:0", 1, 1)], "a tuple of Coins"),
        (("a:0",), "a Coin naming an outpoint"),
        ((Coin("a", 1, 1),), "a Coin naming an outpoint"),
        ((Coin("a:0", 1, 1), Coin("a:0", 1, 1)), "linked once per event"),
    ],
)
def test_malformed_links_raise(coins: Any, match: str) -> None:
    with pytest.raises(EngineError, match=match):
        run([Acquisition("L1", "w", DAY, "buy", 10, D("1.00"), coins=coins)], wallets=WALLETS)


def test_wallets_is_a_frozenset_of_names() -> None:
    with pytest.raises(EngineError, match="frozenset"):
        run([], wallets={"w"})  # type: ignore[arg-type]


def test_a_fee_lot_with_no_row_joins_the_oldest_row_which_keeps_its_date_adr0043() -> None:
    # The payment takes all of L1 (long-term); the fee takes L2 100 ($6.00, short-term), which has no
    # row: no row of its own, its basis joins L1's row, long-term, and the proceeds stay L1's
    events: list[Event] = [
        Acquisition("L1", "w", date(2023, 1, 1), "buy", 500, D("10.00"), coins=(Coin("c0:0", 500, 1_000),)),
        Acquisition("L2", "w", date(2025, 2, 1), "buy", 500, D("30.00"), coins=(Coin("c0:0", 500, 1_000),)),
    ]
    sale = Disposal("s", "w", DAY, AT, "sell", 500, D("50.00"))
    tx = wtx(sale, [Output(0, 500, PAY, event="s"), change(1, 400)], inputs=(Spent("c0:0", 1_000, "w"),))
    (row,) = run([*events, tx], wallets=WALLETS).allocations
    assert (row.lot, row.sats, row.basis, row.proceeds, row.long_term) == (
        "L1",
        500,
        D("16.00"),
        D("50.00"),
        True,
    )
    assert (row.carried_fee, row.carried_from) == (100, ("L2",))


def test_a_gift_fee_lots_lower_basis_joins_another_lots_row_adr0043() -> None:
    # The fee lot is a gift with donor basis $20 and FMV $8 at the gift: the lower, $8, joins the buy's row
    events: list[Event] = [
        Acquisition("L1", "w", date(2023, 1, 1), "buy", 500, D("10.00"), coins=(Coin("c0:0", 500, 600),)),
        Acquisition(
            "G",
            "w",
            date(2023, 2, 1),
            "gift_in",
            100,
            D("20.00"),
            D("8.00"),
            date(2020, 1, 1),
            coins=(Coin("c0:0", 100, 600),),
        ),
    ]
    sale = Disposal("s", "w", DAY, AT, "sell", 500, D("50.00"))
    tx = wtx(sale, [Output(0, 500, PAY, event="s")], inputs=(Spent("c0:0", 600, "w"),))
    (row,) = run([*events, tx], wallets=WALLETS).allocations
    assert (row.lot, row.basis, row.carried_from) == ("L1", D("18.00"), ("G",))


@pytest.mark.parametrize("dest_first", [False, True])
def test_adr0041_second_example_with_the_outputs_swapped(dest_first: bool) -> None:
    move = Transfer("x", "w", "v", DAY, AT, "self_transfer", BTC // 2 + FEE, FEE)
    d, c = (0, 1) if dest_first else (1, 0)
    outs = [Output(d, BTC // 2, DEST, account="v", kind="self_custody", event="x"), change(c, 49_990_000)]
    result = run([*COIN, wtx(move, outs)], wallets=WALLETS)
    assert [(m.into, m.sats, m.basis) for m in result.moves] == [
        ("L1@x", 40_000_000, D("10000.00")),
        ("L2@x", 10_000_000, D("5005.00")),
    ]


def test_a_withdrawals_linked_coin_is_spent_with_its_moved_lots() -> None:
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 1_000, D("10.00")),
        Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 900, 900),)),
        wtx(
            Disposal("s", "w", DAY, AT, "sell", 800, D("20.00")),
            [Output(0, 800, PAY, event="s")],
            inputs=(Spent("c:0", 900, "w"),),
        ),
    ]
    (row,) = run(events, wallets=WALLETS).allocations
    assert (row.lot, row.sats, row.basis, row.carried_fee) == ("e1@wd", 800, D("10.00"), 100)


def test_an_untraced_spend_leaves_its_fee_too_and_drops_the_wallets_coins() -> None:
    # the second coin isn't linked: the spend blocks, takes 900 by FIFO for the payment and 100 for the
    # fee, and the wallet's coin records go (they no longer match its lots)
    events: list[Event] = [
        Acquisition(
            "L1", "w", date(2024, 1, 1), "buy", 2_000, D("20.00"), coins=(Coin("a:0", 1_000, 1_000),)
        ),
        wtx(
            Disposal("s", "w", DAY, AT, "spend", 900, D("9.00")),
            [Output(0, 900, PAY, event="s")],
            inputs=(Spent("b:0", 1_000, "w"),),
        ),
    ]
    result = run(events, wallets=WALLETS)
    assert [lot.sats for lot in result.holdings] == [1_000]
    assert result.coins == ()


def test_a_self_transfer_from_an_account_that_isnt_coin_traced_blocks() -> None:
    events: list[Event] = [
        Acquisition("b1", "z", date(2024, 1, 1), "buy", 10, D("1.00")),
        Transfer("t1", "z", "v", DAY, AT, "self_transfer", 5, 0),
    ]
    result = run(events, wallets=WALLETS)
    assert [(b.event, b.reason) for b in result.blocking if isinstance(b, Untraced)] == [
        ("t1", "mixed_transfer")
    ]
    assert [m.into for m in result.moves] == ["b1@t1"]  # figured by FIFO, provisionally


def test_a_lot_on_two_destination_outputs_moves_as_one_lot_per_output_and_the_fee_basis_stays_on_one() -> (
    None
):
    # L1 (1,000 sats, $10) pays 400 to each of v's two outputs, 100 change, 100 fee. The fee's $1
    # belongs to the first destination output in the canonical order (same value: by script, d1, at
    # vout 1), so each output is its own moved lot: $4.00 on vout 0, $5.00 on vout 1
    events: list[Event] = [
        Acquisition(
            "L1", "w", date(2024, 1, 1), "buy", 1_000, D("10.00"), coins=(Coin("c0:0", 1_000, 1_000),)
        ),
    ]
    move = Transfer("x", "w", "v", DAY, AT, "self_transfer", 900, 100)
    outs = [
        Output(0, 400, b"\x00\x14d2", account="v", kind="self_custody", event="x"),
        Output(1, 400, b"\x00\x14d1", account="v", kind="self_custody", event="x"),
        change(2, 100),
    ]
    result = run([*events, wtx(move, outs, inputs=(Spent("c0:0", 1_000, "w"),))], wallets=WALLETS)
    assert [(m.into, m.sats, m.basis, m.carried_fee) for m in result.moves] == [
        ("L1@x#1", 400, D("5.00"), 100),  # the canonical order: d1 first, whatever its vout
        ("L1@x#0", 400, D("4.00"), 0),
    ]
    assert set(result.coins) == {
        CoinLot("t1:0", "L1@x#0", 400),
        CoinLot("t1:1", "L1@x#1", 400),
        CoinLot("t1:2", "L1", 100),
    }


def test_a_consolidations_fee_basis_stays_on_the_change_output_tracing_names() -> None:
    # L1 (1,100 sats, $11) consolidates into two change outputs, 500 and 450, with a 150 fee. The fee's
    # $1.50 goes to the 500 output's L1 fragment, which becomes a lot of its own: $5.00 + $1.50
    events: list[Event] = [
        Acquisition(
            "L1", "w", date(2024, 1, 1), "buy", 1_100, D("11.00"), coins=(Coin("a:0", 1_100, 1_100),)
        ),
    ]
    outs = [
        Output(0, 500, b"\x00\x14c1", account="w", kind="self_custody"),
        Output(1, 450, b"\x00\x14c2", account="w", kind="self_custody"),
    ]
    result = run([*events, wtx(None, outs, inputs=(Spent("a:0", 1_100, "w"),))], wallets=WALLETS)
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {
        "L1": (450, D("4.50")),
        "L1@tx1#0": (500, D("6.50")),
    }
    assert set(result.coins) == {CoinLot("t1:0", "L1@tx1#0", 500), CoinLot("t1:1", "L1", 450)}


def test_links_from_several_events_over_the_outputs_value_block_at_once() -> None:
    # two 400-sat links on a 600-sat output: the second blocks when it is made, not when the coin is spent
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 400, D("1.00"), coins=(Coin("a:0", 400, 600),)),
        Acquisition("L2", "w", date(2024, 2, 1), "buy", 400, D("1.00"), coins=(Coin("a:0", 400, 600),)),
    ]
    result = run(events, wallets=WALLETS)
    assert result.blocking == (
        Untraced("L2", "bad_link", "a:0 would hold 800 sats of lots, more than its 600"),
    )
    assert result.coins == (CoinLot("a:0", "L1", 400),)


def test_an_outputs_value_is_the_same_in_every_link() -> None:
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 400, D("1.00"), coins=(Coin("a:0", 400, 600),)),
        Acquisition("L2", "w", date(2024, 2, 1), "buy", 200, D("1.00"), coins=(Coin("a:0", 200, 700),)),
    ]
    with pytest.raises(EngineError, match="another value"):
        run(events, wallets=WALLETS)


def test_a_zero_value_coin_spent_with_the_wallets_coins_carries_no_lots() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", BTC // 2, D("50000.00"))
    outs = [Output(0, BTC // 2, PAY, event="s"), change(1, 49_990_000)]
    inputs = (Spent("c0:0", BTC, "w"), Spent("z:0", 0, "w"))
    result = run([*COIN, wtx(spend, outs, inputs=inputs)], wallets=WALLETS)
    assert result.blocking == ()
    assert [a.lot for a in result.allocations] == ["L1", "L2"]


def test_the_replay_stops_where_only_it_cant_trace_a_wallet_transaction() -> None:
    # A late withdrawal choice puts e2@wd 1,000 on the coin; the replay (FIFO) puts e1@wd 500 and e2@wd
    # 500 there. Splitting the coin into two identical outputs then traces in the real run (both get
    # e2@wd) but blocks in the replay (they would get different lots), so the replay stops there.
    late = datetime(2025, 3, 1, 18, tzinfo=UTC)
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 500, D("1.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 1_000, D("4.00")),
        Transfer(
            "wd",
            "x",
            "w",
            DAY,
            AT,
            "withdrawal",
            1_000,
            picks=(Pick("e2", 1_000),),
            identified_at=late,
            coins=(Coin("c:0", 1_000, 1_000),),
        ),
        wtx(None, [change(0, 500), change(1, 500)], inputs=(Spent("c:0", 1_000, "w"),)),
    ]
    result = run(events, wallets=WALLETS)
    assert result.blocking == ()
    assert result.replay_stopped == "tx1"


def two_change(vouts: tuple[int, int] = (0, 1)) -> list[Output]:
    return [
        Output(vouts[0], 500, b"\x00\x14c1", account="w", kind="self_custody"),
        Output(vouts[1], 450, b"\x00\x14c2", account="w", kind="self_custody"),
    ]


def test_two_fee_carries_onto_one_change_fragment_both_stay_on_that_coin() -> None:
    # L1 (100, $10) on a:0 and L2 (1,000, $30) on b:0 consolidate into 500 and 450 with a 150 fee: the
    # fee takes L1 100 (on no change output: onto the first output's oldest fragment, L2's) and L2 50
    # (onto the same fragment). That fragment splits off: $15.00 + $10.00 + $1.50 = $26.50; L2 keeps
    # 450 sats, $13.50 (ADR 0041 §2.4, ADR 0043)
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 100, D("10.00"), coins=(Coin("a:0", 100, 100),)),
        Acquisition(
            "L2", "w", date(2024, 2, 1), "buy", 1_000, D("30.00"), coins=(Coin("b:0", 1_000, 1_000),)
        ),
        wtx(None, two_change(), inputs=(Spent("a:0", 100, "w"), Spent("b:0", 1_000, "w"))),
    ]
    result = run(events, wallets=WALLETS)
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {
        "L2": (450, D("13.50")),
        "L2@tx1#0": (500, D("26.50")),
    }


def test_two_fee_carries_onto_one_fragment_with_the_lot_on_a_third_coin() -> None:
    # as above, with 200 more of L2 on another coin d:0 that stays unspent: L2's $36 for 1,200 sats
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 100, D("10.00"), coins=(Coin("a:0", 100, 100),)),
        Acquisition(
            "L2",
            "w",
            date(2024, 2, 1),
            "buy",
            1_200,
            D("36.00"),
            coins=(Coin("b:0", 1_000, 1_000), Coin("d:0", 200, 200)),
        ),
        wtx(None, two_change(), inputs=(Spent("a:0", 100, "w"), Spent("b:0", 1_000, "w"))),
    ]
    held = {lot.id: (lot.sats, lot.basis) for lot in run(events, wallets=WALLETS).holdings}
    assert held == {"L2": (650, D("19.50")), "L2@tx1#0": (500, D("26.50"))}


def test_a_mixed_transfer_into_a_wallet_drops_its_coins_and_its_later_transactions_block() -> None:
    events: list[Event] = [
        Acquisition("b1", "z", date(2023, 12, 1), "buy", 10, D("1.00")),
        *COIN,
        Transfer("t1", "z", "w", DAY, AT, "self_transfer", 5, 0),
        wtx(None, [change(0, BTC)], id="tx2", txid="t2"),
    ]
    result = run(events, wallets=WALLETS)
    assert [(b.event, b.reason) for b in result.blocking if isinstance(b, Untraced)] == [
        ("t1", "mixed_transfer"),
        ("tx2", "after_untraced"),
    ]
    assert result.coins == ()


def test_a_transaction_spending_two_wallets_untraces_both() -> None:
    events: list[Event] = [
        Acquisition("V", "v", date(2023, 12, 1), "buy", 1_000, D("1.00"), coins=(Coin("v:0", 1_000, 1_000),)),
        Acquisition(
            "V2", "v", date(2023, 12, 2), "buy", 1_000, D("1.00"), coins=(Coin("v:1", 1_000, 1_000),)
        ),
        *COIN,
        wtx(
            Disposal("s", "w", DAY, AT, "spend", BTC, D("1.00")),
            [Output(0, BTC, PAY, event="s")],
            inputs=(Spent("c0:0", BTC, "w"), Spent("v:0", 1_000, "v")),
        ),
        wtx(
            None,
            [change(0, 1_000, "v")],
            id="tx2",
            account="v",
            txid="t2",
            inputs=(Spent("v:1", 1_000, "v"),),
        ),
    ]
    result = run(events, wallets=WALLETS)
    assert [(b.event, b.reason) for b in result.blocking if isinstance(b, Untraced)] == [
        ("tx1", "several_accounts"),
        ("tx2", "after_untraced"),
    ]
    assert result.coins == ()


def test_a_gift_fee_lot_on_a_transfer_passes_its_lower_basis_adr0043() -> None:
    # 500 to v, a 100 fee from G (a gift: donor basis $20, FMV $8 at the gift), which v's output doesn't
    # hold: the lower, $8, joins L1@x
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 500, D("10.00"), coins=(Coin("c0:0", 500, 600),)),
        Acquisition(
            "G",
            "w",
            date(2024, 2, 1),
            "gift_in",
            100,
            D("20.00"),
            D("8.00"),
            date(2020, 1, 1),
            coins=(Coin("c0:0", 100, 600),),
        ),
    ]
    move = Transfer("x", "w", "v", DAY, AT, "self_transfer", 600, 100)
    tx = wtx(
        move,
        [Output(0, 500, DEST, account="v", kind="self_custody", event="x")],
        inputs=(Spent("c0:0", 600, "w"),),
    )
    assert run([*events, tx], wallets=WALLETS).moves == (
        Moved("x", "L1", "L1@x", "v", 500, D("18.00"), date(2024, 1, 1), 100, ("G",)),
    )


def test_the_oldest_row_ties_by_event_id_adr0043() -> None:
    # "b" and "a" reached the user the same day, b recorded first; the payment takes both, the fee takes
    # c, which has no row: its basis joins "a"'s row, the oldest by event id
    events: list[Event] = [
        Acquisition("b", "w", date(2024, 1, 1), "buy", 100, D("1.00"), coins=(Coin("c0:0", 100, 300),)),
        Acquisition("a", "w", date(2024, 1, 1), "buy", 100, D("1.00"), coins=(Coin("c0:0", 100, 300),)),
        Acquisition("c", "w", date(2024, 2, 1), "buy", 100, D("5.00"), coins=(Coin("c0:0", 100, 300),)),
    ]
    sale = Disposal("s", "w", DAY, AT, "sell", 200, D("10.00"))
    tx = wtx(sale, [Output(0, 200, PAY, event="s")], inputs=(Spent("c0:0", 300, "w"),))
    rows = {a.lot: a.basis for a in run([*events, tx], wallets=WALLETS).allocations}
    assert rows == {"a": D("6.00"), "b": D("1.00")}


def test_a_new_coins_value_is_recorded_for_later_links() -> None:
    spend = Disposal("s", "w", DAY, AT, "spend", BTC // 2, D("50000.00"))
    tx = wtx(spend, [Output(0, BTC // 2, PAY, event="s"), change(1, 49_990_000)])
    late_link = Acquisition("L3", "w", date(2025, 4, 1), "buy", 1, D("1.00"), coins=(Coin("t1:1", 1, 7),))
    with pytest.raises(EngineError, match="another value"):
        run([*COIN, tx, late_link], wallets=WALLETS)


@pytest.mark.parametrize(
    ("outputs", "match"),
    [
        (["x"], "an Output with its vout"),
        ([Output(-1, 1, PAY)], "an Output with its vout"),
        ([Output(0, -1, PAY)], "a whole number of sats"),
        ([Output(0, 1, PAY), Output(0, 1, PAY)], "distinct vouts"),
        ([Output(0, BTC + 1, PAY)], "don't exceed its inputs"),
    ],
)
def test_outputs_are_checked_even_when_the_wallet_is_untraced(outputs: list[Any], match: str) -> None:
    untraced_first = wtx(None, [change(0, 1)], inputs=(Spent("u:0", 1, "w"),))
    bad = wtx(None, outputs, id="tx2", txid="t2")
    with pytest.raises(EngineError, match=match):
        run([*COIN, untraced_first, bad], wallets=WALLETS)


def test_a_plain_deposit_into_a_wallet_raises() -> None:
    events: list[Event] = [
        Acquisition("e", "x", date(2024, 1, 1), "buy", 10, D("1.00")),
        Transfer("d", "x", "w", DAY, AT, "deposit", 10, 0),
    ]
    with pytest.raises(EngineError, match="not a wallet"):
        run(events, wallets=WALLETS)


EXCHANGE_LOT: list[Event] = [Acquisition("e1", "x", date(2024, 1, 1), "buy", 1_000, D("10.00"))]


def test_a_linked_withdrawals_fee_basis_stays_on_the_first_output_holding_its_lot_adr0043() -> None:
    # 1,000 leave the exchange, 100 of them the fee ($1); 900 arrive on two outputs, 500 then 400. The
    # fee's basis goes to the first one, whose fragment becomes a lot of its own: $5 + $1 there, $4 on
    # the second
    wd = Transfer(
        "wd",
        "x",
        "w",
        DAY,
        AT,
        "withdrawal",
        1_000,
        100,
        coins=(Coin("c:0", 500, 500), Coin("c:1", 400, 400)),
    )
    result = run([*EXCHANGE_LOT, wd], wallets=WALLETS)
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {
        "e1@wd": (400, D("4.00")),
        "e1@wd#0": (500, D("6.00")),
    }
    assert set(result.coins) == {CoinLot("c:0", "e1@wd#0", 500), CoinLot("c:1", "e1@wd", 400)}
    assert result.moves == (Moved("wd", "e1", "e1@wd", "w", 900, D("10.00"), date(2024, 1, 1), 100),)


def test_a_partly_linked_withdrawals_fee_basis_stays_on_the_linked_coin_adr0043() -> None:
    # only 500 of the 900 are linked; the rest is in the pool, without the fee's basis
    wd = Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 500, 500),))
    result = run([*EXCHANGE_LOT, wd], wallets=WALLETS)
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {
        "e1@wd": (400, D("4.00")),
        "e1@wd#0": (500, D("6.00")),
    }


def test_a_linked_withdrawals_all_fee_lot_joins_the_first_outputs_oldest_fragment_adr0043() -> None:
    # e1 900 arrives on one output; e2 (100, $5) is all fee: its basis joins e1@wd there
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 900, D("9.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 100, D("5.00")),
        Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 900, 900),)),
    ]
    result = run(events, wallets=WALLETS)
    assert result.moves == (Moved("wd", "e1", "e1@wd", "w", 900, D("14.00"), date(2024, 1, 1), 100, ("e2",)),)
    assert {lot.id: lot.basis for lot in result.holdings} == {"e1@wd": D("14.00")}


def test_a_withdrawal_whose_links_are_refused_keeps_the_fee_basis_on_the_moved_part() -> None:
    wd = Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 950, 950),))
    result = run([*EXCHANGE_LOT, wd], wallets=WALLETS)
    assert [b.reason for b in result.blocking if isinstance(b, Untraced)] == ["bad_link"]
    assert {lot.id: (lot.sats, lot.basis) for lot in result.holdings} == {"e1@wd": (900, D("10.00"))}


def test_an_untraceable_sales_fee_basis_joins_its_row() -> None:
    # the coin isn't linked: by FIFO, the sale takes L1 900 and the fee L1 100, whose basis is in the row
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 2_000, D("20.00")),
        wtx(
            Disposal("s", "w", DAY, AT, "sell", 900, D("50.00")),
            [Output(0, 900, PAY, event="s")],
            inputs=(Spent("b:0", 1_000, "w"),),
        ),
    ]
    (row,) = run(events, wallets=WALLETS).allocations
    assert (row.lot, row.sats, row.basis, row.carried_fee) == ("L1", 900, D("10.00"), 100)


def test_an_untraceable_consolidations_fee_basis_stays_in_the_wallet() -> None:
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 2_000, D("20.00")),
        wtx(None, [change(0, 900)], inputs=(Spent("b:0", 1_000, "w"),)),
    ]
    assert {lot.id: (lot.sats, lot.basis) for lot in run(events, wallets=WALLETS).holdings} == {
        "L1": (1_900, D("20.00"))
    }


def test_a_sales_rows_take_their_basis_before_the_fee_so_no_cent_moves_to_the_change() -> None:
    # a 4-sat lot with $0.01: the payment's 1 sat takes $0.00 of it first, then the fee's 2 sats $0.01
    # of the rest, which joins the row; the change's sat keeps $0.00 (ADR 0041 §2.2's order)
    events: list[Event] = [
        Acquisition("L", "w", date(2024, 1, 1), "buy", 4, D("0.01"), coins=(Coin("c:0", 4, 4),))
    ]
    sale = Disposal("s", "w", DAY, AT, "sell", 1, D("0.01"))
    tx = wtx(sale, [Output(0, 1, PAY, event="s"), change(1, 1)], inputs=(Spent("c:0", 4, "w"),))
    result = run([*events, tx], wallets=WALLETS)
    assert [(a.sats, a.basis, a.carried_fee) for a in result.allocations] == [(1, D("0.01"), 2)]
    assert [(lot.sats, lot.basis) for lot in result.holdings] == [(1, D("0.00"))]


def test_an_untraceable_self_transfers_destination_is_untraced_too() -> None:
    events: list[Event] = [
        Acquisition("V", "v", date(2023, 12, 1), "buy", 1_000, D("1.00"), coins=(Coin("v:0", 1_000, 1_000),)),
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 2_000, D("2.00")),
        wtx(
            Transfer("x", "w", "v", DAY, AT, "self_transfer", 1_000, 100),
            [Output(0, 900, DEST, account="v", kind="self_custody", event="x")],
            inputs=(Spent("b:0", 1_000, "w"),),
        ),
        wtx(
            None,
            [change(0, 1_000, "v")],
            id="tx2",
            account="v",
            txid="t2",
            inputs=(Spent("v:0", 1_000, "v"),),
        ),
    ]
    result = run(events, wallets=WALLETS)
    assert [(b.event, b.reason) for b in result.blocking if isinstance(b, Untraced)] == [
        ("tx1", "uncovered"),
        ("tx2", "after_untraced"),
    ]


def test_a_linked_withdrawals_fee_of_an_unlinked_lot_joins_the_first_outputs_oldest_fragment() -> None:
    # e1 300 and e2 600 arrive, e2's last 100 is the fee ($1); only e1's 300 are linked, so the fee's
    # basis (the lower of e2's, here its one) joins e1@wd on that coin: $3 + $1
    events: list[Event] = [
        Acquisition("e1", "x", date(2024, 1, 1), "buy", 300, D("3.00")),
        Acquisition("e2", "x", date(2024, 2, 1), "buy", 700, D("7.00")),
        Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 300, 300),)),
    ]
    held = {lot.id: (lot.sats, lot.basis) for lot in run(events, wallets=WALLETS).holdings}
    assert held == {"e1@wd": (300, D("4.00")), "e2@wd": (600, D("6.00"))}


def test_links_into_an_untraced_wallet_record_no_coins() -> None:
    events: list[Event] = [
        Acquisition("b1", "z", date(2023, 12, 1), "buy", 10, D("1.00")),
        Transfer(
            "t1", "z", "w", date(2024, 1, 1), datetime(2024, 1, 1, 12, tzinfo=UTC), "self_transfer", 5, 0
        ),
        *EXCHANGE_LOT,
        Transfer("wd", "x", "w", DAY, AT, "withdrawal", 1_000, 100, coins=(Coin("c:0", 900, 900),)),
    ]
    result = run(events, wallets=WALLETS)
    assert result.coins == ()
    assert {lot.id: lot.basis for lot in result.holdings if lot.id.startswith("e1")} == {"e1@wd": D("10.00")}


def test_an_untraceable_spends_unrecorded_output_leaves_the_lots_too() -> None:
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 2_000, D("20.00")),
        wtx(
            Disposal("s", "w", DAY, AT, "spend", 500, D("5.00")),
            [Output(0, 500, PAY, event="s"), Output(1, 300, DEST)],
            inputs=(Spent("b:0", 1_000, "w"),),
        ),
    ]
    assert [lot.sats for lot in run(events, wallets=WALLETS).holdings] == [1_000]


def test_an_untraceable_sale_short_of_lots_is_figured_as_a_disposal_is() -> None:
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 500, D("5.00")),
        wtx(
            Disposal("s", "w", DAY, AT, "sell", 900, D("9.00")),
            [Output(0, 900, PAY, event="s")],
            inputs=(Spent("b:0", 1_000, "w"),),
        ),
    ]
    result = run(events, wallets=WALLETS)
    assert MissingLots("s", 400, D("4.00")) in result.blocking


def test_an_untraceable_consolidations_fee_that_uses_up_a_lot_joins_the_oldest_remaining() -> None:
    # the fee takes L1's 50 ($0.50), all of it, and L2's next 50: L1's basis joins L2, L2 keeps its own
    events: list[Event] = [
        Acquisition("L1", "w", date(2024, 1, 1), "buy", 50, D("0.50")),
        Acquisition("L2", "w", date(2024, 2, 1), "buy", 2_000, D("20.00")),
        wtx(None, [change(0, 900)], inputs=(Spent("b:0", 1_000, "w"),)),
    ]
    held = {lot.id: (lot.sats, lot.basis) for lot in run(events, wallets=WALLETS).holdings}
    assert held == {"L2": (1_950, D("20.50"))}

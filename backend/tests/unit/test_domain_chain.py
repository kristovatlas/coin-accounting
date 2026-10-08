"""Chain domain types: exact satoshi conversion and output classification (PLAN §1; T-205, T-502)."""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest

from coinacct.domain.chain import MAX_SATS, Outpoint, Tx, TxIn, TxOut, btc_to_sats, is_hash

TXID = "aa" * 32


@pytest.mark.parametrize(
    ("btc", "sats"),
    [("0", 0), ("0.00000001", 1), ("1", 100_000_000), ("50.00000000", 5_000_000_000), ("21000000", MAX_SATS)],
)
def test_btc_converts_to_exact_satoshis_t502(btc: str, sats: int) -> None:
    assert btc_to_sats(Decimal(btc)) == sats


@pytest.mark.parametrize(
    "btc",
    [
        "0.000000001",
        "0.123456789",
        "-0.00000001",
        "21000000.00000001",
        "NaN",
        "Infinity",
        "1.000000000000000000000000000001",  # more digits than the default context: no rounding
        "0.0000000100000000000000000000000000001",
        "1E-1000100",  # would underflow to 0 sats
        "1E+999999",  # would overflow
        "21000000.0000000000000000000000000000001",  # just above the cap, not rounded down to it
    ],
)
def test_a_fraction_of_a_satoshi_or_an_out_of_range_amount_is_refused_t502(btc: str) -> None:
    with pytest.raises(ValueError, match="amount"):
        btc_to_sats(Decimal(btc))


@pytest.mark.parametrize(
    "ambient",
    [
        decimal.Context(prec=4),
        decimal.Context(Emax=3),
        decimal.Context(traps=[decimal.Rounded, decimal.Clamped, decimal.Subnormal]),
    ],
)
def test_the_conversion_ignores_the_ambient_decimal_context_t502(ambient: decimal.Context) -> None:
    with decimal.localcontext(ambient):
        assert btc_to_sats(Decimal("1.23456789")) == 123_456_789
        assert btc_to_sats(Decimal("21000000.00000000000000000000000000000000")) == MAX_SATS


def test_a_fraction_of_a_satoshi_is_a_value_error_whatever_the_caller_traps_t502() -> None:
    with (
        decimal.localcontext(decimal.Context(traps=[decimal.Inexact, decimal.Rounded])),
        pytest.raises(ValueError),
    ):
        btc_to_sats(Decimal("1.234567891"))


def test_only_a_decimal_is_converted() -> None:
    with pytest.raises(ValueError, match="Decimal"):
        btc_to_sats(0.1)  # type: ignore[arg-type]  # a float must never reach here


@pytest.mark.parametrize(
    ("script_hex", "script_type", "unspendable"),
    [
        ("6a0b68656c6c6f", "nulldata", True),
        ("6a", "nonstandard", True),  # OP_RETURN, whatever Core calls it
        ("51" * 10_001, "nonstandard", True),  # longer than MAX_SCRIPT_SIZE
        ("51" * 10_000, "nonstandard", False),
        ("0014" + "00" * 20, "witness_v0_keyhash", False),
    ],
)
def test_unspendable_outputs_are_recognised(script_hex: str, script_type: str, unspendable: bool) -> None:
    assert TxOut(0, script_hex, script_type).unspendable is unspendable


def test_an_outpoint_needs_a_txid_and_an_index() -> None:
    assert Outpoint(TXID, 0).vout == 0
    for txid, vout in (("AA" * 32, 0), ("aa" * 31, 0), (TXID, -1)):
        with pytest.raises(ValueError, match="outpoint"):
            Outpoint(txid, vout)


@pytest.mark.parametrize(
    ("value", "ok"), [(TXID, True), ("AA" * 32, False), ("aa" * 33, False), (None, False)]
)
def test_hashes_are_64_lowercase_hex_digits(value: object, ok: bool) -> None:
    assert is_hash(value) is ok


def out(sats: int) -> TxOut:
    return TxOut(sats, "0014" + "00" * 20, "witness_v0_keyhash")


def test_the_fee_is_known_once_every_spent_output_is() -> None:
    known = Tx(
        TXID,
        None,
        0,
        None,
        (TxIn(Outpoint(TXID, 0), 0, out(1000)), TxIn(Outpoint(TXID, 1), 0, out(500))),
        (out(1200),),
    )
    assert known.prevouts_known and known.fee_sats == 300
    unknown = Tx(
        TXID, None, 0, None, (TxIn(Outpoint(TXID, 0), 0, out(1000)), TxIn(Outpoint(TXID, 1), 0)), (out(1200),)
    )
    assert not unknown.prevouts_known and unknown.fee_sats is None


def test_a_coinbase_has_no_fee_and_counts_its_prevouts_as_known() -> None:
    cb = Tx(TXID, "bb" * 32, 1, 0, (TxIn(None, 0xFFFFFFFF),), (out(5_000_000_000),))
    assert cb.coinbase and cb.prevouts_known and cb.fee_sats == 0 and cb.confirmed

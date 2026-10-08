"""Log redaction rules (THREAT_MODEL T-403). Fixtures are public or synthetic: Core's regtest
OP_TRUE address, the genesis block's address and hash, BIP32 test vector 1's keys, a public testnet
private key, and the WIF of private key 1."""

from __future__ import annotations

import pytest

from coinacct.domain.redact import redact

GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
GENESIS_ADDRESS = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
REGTEST_OP_TRUE = "bcrt1qft5p2uhsdcdc3l2ua4ap5qqfg4pjaqlp250x7us7a8qqhrxrxfsqseac85"
TPUB = "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1RhGjxbaTLV3X4FyWuejifB9jusQ46QzG87VKp"  # noqa: E501 - BIP32 test vector 1
# The WIF of private key 1 (a well-known test value, not anyone's funds).
WIF = "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn"
# BIP32 test vector 1's master private key, and a public testnet test key (both also in
# test_domain_keys.py): public test data, not anyone's funds.
XPRV = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiC"
    "hkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)
TPRV = (
    "tprv8ZgxMBicQKsPd7Uf69XL1XwhmjHopUGep8GuEiJDZmbQz6o58LninorQ"
    "AfcKZWARbtRtfnLcJ5MQ2AtHcQJCCRUcMRvmDUjyEmNUWwx8UbK"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"tx {GENESIS_HASH} confirmed", "tx <hex> confirmed"),
        (f"outpoint {GENESIS_HASH}:0", "outpoint <hex>:0"),
        (f"paid {GENESIS_ADDRESS} and {REGTEST_OP_TRUE}", "paid <address> and <address>"),
        ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4 upper", "<address> upper"),
        (f"descriptor wpkh({TPUB}/0/*)", "descriptor wpkh(<key>/0/*)"),
        (f"key {WIF}", "key <key>"),
        (f"descriptor wpkh({XPRV}/84h/0h/0h/0/*)", "descriptor wpkh(<key>/84h/0h/0h/0/*)"),  # T-703
        (f"key {TPRV} refused", "key <key> refused"),  # T-703
        ("sold 0.12345678 BTC for $4,321.00", "sold <amount> BTC for <amount>"),
        (
            "fee 1500 sat, 21000000 sats total, at height 840000",
            "fee <amount> sat, <amount> sats total, at height <number>",
        ),
        ("price €62.10", "price <amount>"),
        # At the end of a sentence, glued to a unit, small, or with a currency code or an exponent.
        ("need 0.50000000.", "need <amount>."),
        ("reached height 840000.", "reached height <number>."),
        ("paid 0.00012345BTC and 1500000sat", "paid <amount>BTC and <amount>sat"),
        ("sent 1 BTC, then 0.5 BTC and 2 sats", "sent <amount> BTC, then <amount> BTC and <amount> sats"),
        ("USD 4321 and 4,321.00 EUR", "USD <amount> and <amount> EUR"),
        ("dust 1E-8 BTC, or 1e-05", "dust <amount> BTC, or <amount>"),
        ("supply 21,000,000", "supply <number>"),
        # Scripts and hashes shorter than a txid: a P2WPKH scriptPubKey (44 hex digits), a HASH160.
        ("spk 0014751e76e8199196d454941c45d1b3a323f1433bd6", "spk <hex>"),
        ("hash160 751e76e8199196d454941c45d1b3a323f1433bd6", "hash160 <hex>"),
        ("raw(0014751e76e8199196d454941c45d1b3a323f1433bd6)", "raw(<hex>)"),
        (f"block 0x{GENESIS_HASH} and {GENESIS_HASH}i0", "block 0x<hex> and <hex>i0"),
        # The key origin in a descriptor: the master fingerprint and the path.
        (f"wpkh([d34db33f/84h/0'/0h]{TPUB}/0/*)", "wpkh([<origin>]<key>/0/*)"),
        # Credentials (synthetic values).
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic <credential>"),
        ("authorization=Bearer abc.def-ghi", "authorization=Bearer <credential>"),
        ("Proxy-Authorization: token123", "Proxy-Authorization: <credential>"),
        ("open http://127.0.0.1:5/#bootstrap=AbC_d-9xyz", "open http://127.0.0.1:5/#bootstrap=<credential>"),
        ("rpc.port must be from 1 to 65535", "rpc.port must be from 1 to <number>"),
        # Values joined by commas, as in compact JSON or a CSV row.
        ("0.12345678,0.98765432", "<amount>,<amount>"),
        ("scan 840000,840100", "scan <number>,<number>"),
        ("[0.1,0.12345678]", "[0.1,<amount>]"),
        ("2024-01-01,0.12345678,BTC", "2024-01-01,<amount>,BTC"),
        # …and when the next value has exactly three digits, which looks like a thousands group.
        ("2024-01-01,0.12345678,144", "2024-01-01,<amount>,144"),
        ("[150000000,250]", "[<number>,250]"),
        ("BTC 0.12345678,144", "BTC <amount>,144"),
        ("1,234,567.89 and 12,345,678", "<amount> and <number>"),
    ],
)
def test_chain_identifiers_and_amounts_are_masked_t403(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Bitcoin Core 31.1 on regtest",
        "scan range 3 of 12 done",
        "HTTP 403 from the node",
        "retry 2/3",
        "job 7 cancelled",
        "Bitcoin Core 31.1.0 started.",
    ],
)
def test_ordinary_operational_text_is_left_readable(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1," * 50_000, "1," * 50_000),
        ("1,234," * 30_000, "<number>," * 30_000),
        ("$1," * 30_000, "<amount>," * 30_000),
        ("USD 1," * 20_000, "USD <amount>," * 20_000),
        ("9" * 200_000, "<hex>"),  # 40+ digits are hex too
    ],
    ids=["commas", "grouped", "signs", "codes", "digits"],
)
def test_long_adversarial_inputs_are_redacted_t403(text: str, expected: str) -> None:
    # No time limit (ENGINEERING §3.5): the linear rules finish in milliseconds, while a quadratic
    # pattern (such as `[\d,]*`) would not finish at all on these, failing the job.
    assert redact(text) == expected

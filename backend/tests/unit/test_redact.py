"""Log redaction rules (THREAT_MODEL T-403). Fixtures are public or synthetic: Core's regtest
OP_TRUE address, the genesis block's address and hash, and a BIP32 test-vector key."""

from __future__ import annotations

import pytest

from coinacct.domain.redact import redact

GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
GENESIS_ADDRESS = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
REGTEST_OP_TRUE = "bcrt1qft5p2uhsdcdc3l2ua4ap5qqfg4pjaqlp250x7us7a8qqhrxrxfsqseac85"
TPUB = "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1RhGjxbaTLV3X4FyWuejifB9jusQ46QzG87VKp"  # noqa: E501 - BIP32 test vector 1
# The WIF of private key 1 (a well-known test value, not anyone's funds).
WIF = "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"tx {GENESIS_HASH} confirmed", "tx <hex> confirmed"),
        (f"outpoint {GENESIS_HASH}:0", "outpoint <hex>:0"),
        (f"paid {GENESIS_ADDRESS} and {REGTEST_OP_TRUE}", "paid <address> and <address>"),
        ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4 upper", "<address> upper"),
        (f"descriptor wpkh({TPUB}/0/*)", "descriptor wpkh(<key>/0/*)"),
        (f"key {WIF}", "key <key>"),
        ("sold 0.12345678 BTC for $4,321.00", "sold <amount> BTC for <amount>"),
        (
            "fee 1500 sat, 21000000 sats total, at height 840000",
            "fee 1500 sat, <number> sats total, at height <number>",
        ),
        ("price €62.10", "price <amount>"),
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
        "rpc.port must be from 1 to 65535",
    ],
)
def test_ordinary_operational_text_is_left_readable(text: str) -> None:
    assert redact(text) == text.replace("65535", "<number>")

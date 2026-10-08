"""Address parsing (THREAT_MODEL T-701): BIP173/BIP350 test vectors and Base58Check."""

from __future__ import annotations

import hashlib

import pytest

from coinacct.chain.node_checks import KNOWN_CHAINS
from coinacct.domain.addresses import NETWORKS, AddressError, parse_address
from coinacct.storage.chain_state import CHAINS

# BIP350 "Test vectors for v0-v16 native segregated witness addresses" (valid), with their scripts.
VALID_SEGWIT = [
    ("main", "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "0014751e76e8199196d454941c45d1b3a323f1433bd6"),
    (
        "test",
        "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7",
        "00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262",
    ),
    (
        "main",
        "bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y",
        "5128751e76e8199196d454941c45d1b3a323f1433bd6751e76e8199196d454941c45d1b3a323f1433bd6",
    ),
    ("main", "BC1SW50QGDZ25J", "6002751e"),
    ("main", "bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs", "5210751e76e8199196d454941c45d1b3a323"),
    (
        "test",
        "tb1qqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesrxh6hy",
        "0020000000c4a5cad46221b2a187905e5266362b99d5e91c6ce24d165dab93e86433",
    ),
    (
        "test",
        "tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c",
        "5120000000c4a5cad46221b2a187905e5266362b99d5e91c6ce24d165dab93e86433",
    ),
    (
        "main",
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
        "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
    ),
]

# BIP350 invalid segwit addresses, each for the chain whose prefix it would otherwise match.
INVALID_SEGWIT = [
    ("main", "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd"),  # bech32, not bech32m
    ("test", "tb1z0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqglt7rf"),  # bech32, not bech32m
    ("main", "BC1S0XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ54WELL"),  # bech32, not bech32m
    ("main", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh"),  # v0 with bech32m
    ("test", "tb1q0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq24jc47"),  # v0 with bech32m
    ("main", "bc1p38j9r5y49hruaue7wxjce0updqjuyyx0kh56v8s25huc6995vvpql3jow4"),  # invalid character
    ("main", "BC130XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ7ZWS8R"),  # witness version 17
    ("main", "bc1pw5dgrnzv"),  # program of 1 byte
    ("main", "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v8n0nx0muaewav253zgeav"),  # 41 bytes
    ("main", "BC1QR508D6QEJXTDG4Y5R3ZARVARYV98GJ9P"),  # v0 program of 16 bytes
    ("test", "tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq47Zagq"),  # mixed case
    ("main", "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v07qwwzcrf"),  # padding over 4 bits
    ("test", "tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vpggkg4j"),  # non-zero padding
    ("main", "bc1gmk9yu"),  # empty data section
]


@pytest.mark.parametrize(("chain", "text", "script"), VALID_SEGWIT)
def test_bip350_valid_segwit_addresses_give_their_scripts_t701(chain: str, text: str, script: str) -> None:
    address = parse_address(text, chain)
    assert address.script_hex == script and address.text == text.lower()


@pytest.mark.parametrize(("chain", "text"), INVALID_SEGWIT)
def test_bip350_invalid_segwit_addresses_are_refused_t701(chain: str, text: str) -> None:
    with pytest.raises(AddressError):
        parse_address(text, chain)


def test_base58_mainnet_p2pkh_and_p2sh_give_their_scripts_t701() -> None:
    # The genesis block's coinbase address, and the P2SH example from the Bitcoin wiki.
    assert parse_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "main").script_hex == (
        "76a91462e907b15cbf27d5425399ebf6f0fb50ebb88f1888ac"
    )
    assert parse_address("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "main").script_hex == (
        "a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87"
    )


def test_a_base58_address_with_a_wrong_checksum_is_refused_t701() -> None:
    with pytest.raises(AddressError, match="checksum"):
        parse_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb", "main")


@pytest.mark.parametrize(
    ("chain", "text"),
    [
        ("regtest", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"),  # a mainnet address on regtest
        ("main", "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"),
        ("regtest", "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"),
        ("main", "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"),
        ("regtest", "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4"),
    ],
)
def test_an_address_of_another_chain_is_refused_t701(chain: str, text: str) -> None:
    with pytest.raises(AddressError, match="another chain"):
        parse_address(text, chain)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x" * 91,
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNá",
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7Div\nfNa",
        "0OIl",
        "11111",
    ],
)
def test_text_that_isnt_an_address_is_refused_without_being_repeated_t701(text: str) -> None:
    with pytest.raises(AddressError) as e:
        parse_address(text, "main")
    assert text not in str(e.value) or not text


def test_an_unknown_chain_is_refused() -> None:
    with pytest.raises(AddressError, match="unknown"):
        parse_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "dogecoin")


def base58check(payload: bytes) -> str:
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n, out = int.from_bytes(raw, "big"), ""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    while n:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


@pytest.mark.parametrize("hash_bytes", [19, 21, 32])
def test_a_base58check_payload_of_the_wrong_length_is_refused_t701(hash_bytes: int) -> None:
    text = base58check(b"\x00" + b"\x11" * hash_bytes)  # a valid checksum, but not a 20-byte hash
    with pytest.raises(AddressError, match="not an address"):
        parse_address(text, "main")
    assert (
        parse_address(base58check(b"\x00" + b"\x11" * 20), "main").script_hex == "76a914" + "11" * 20 + "88ac"
    )


def test_every_chain_the_app_knows_has_its_address_rules() -> None:
    assert set(NETWORKS) == set(KNOWN_CHAINS) == set(CHAINS)
    with pytest.raises(TypeError):
        NETWORKS["main"] = NETWORKS["regtest"]  # type: ignore[index]  # the write must fail at run time


@pytest.mark.parametrize("chain", ["test", "testnet4", "signet"])
def test_the_testnets_share_the_tb_prefix_t701(chain: str) -> None:
    text = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"
    assert parse_address(text, chain).script_hex.startswith("0020")


def test_a_stray_1_inside_a_segwit_address_is_not_another_chain_t701() -> None:
    with pytest.raises(AddressError, match="not an address"):
        parse_address("bc1qw508d6qejxtdg4y5r3zarv1ry0c5xw7kv8f3t4", "main")


def test_a_segwit_address_over_90_characters_is_refused_t701() -> None:
    with pytest.raises(AddressError, match="not an address"):
        parse_address("bc1" + "q" * 88, "main")

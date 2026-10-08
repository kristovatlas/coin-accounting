"""Private key material in imports is refused (ADR 0019; THREAT_MODEL T-703).

The keys below are public test vectors (BIP32 test vector 1, and the well-known WIF examples from
the Bitcoin wiki); none of them holds any value.
"""

from __future__ import annotations

import pytest

from coinacct.domain.keys import PrivateKeyError, has_private_material, refuse_private

XPRV = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiC"
    "hkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)
XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2g"
    "Z29ESFjqJoCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
TPRV = (
    "tprv8ZgxMBicQKsPd7Uf69XL1XwhmjHopUGep8GuEiJDZmbQz6o58LninorQ"
    "AfcKZWARbtRtfnLcJ5MQ2AtHcQJCCRUcMRvmDUjyEmNUWwx8UbK"
)
WIF_COMPRESSED = "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn"
WIF_UNCOMPRESSED = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"
WIF_TESTNET = "cMahea7zqjxrtgAbB7LSGbcQUr1uX1ojuat9jZodMN87JcbXMTcA"


@pytest.mark.parametrize(
    "text",
    [
        XPRV,
        TPRV,
        f"wpkh({XPRV}/84h/0h/0h/0/*)",
        f"wpkh([d34db33f/84h/0h/0h]{XPRV}/0/*)#abcdefgh",
        f"tr({TPRV}/86h/1h/0h/0/*)",
        "y" + XPRV[1:],
        "Z" + XPRV[1:],
        WIF_COMPRESSED,
        WIF_UNCOMPRESSED,
        WIF_TESTNET,
        "9" + WIF_UNCOMPRESSED[1:],  # testnet, uncompressed
        f"pkh({WIF_COMPRESSED})",
        f"wsh(multi(2,{XPUB}/0/*,{WIF_COMPRESSED}))",
        f"one line\n{WIF_TESTNET}\nanother",
        WIF_COMPRESSED[:-1] + ("o" if WIF_COMPRESSED[-1] != "o" else "p"),  # a bad checksum still counts
        WIF_COMPRESSED[:-1],  # a character dropped while copying
        WIF_COMPRESSED + "x",  # a stray character added
        WIF_UNCOMPRESSED[:-2],
        XPRV[:40],  # a truncated extended key
        XPRV + "Q",  # a stray character after an extended key
        f"wpkh({XPRV[:60]}",  # pasted only in part
        XPRV + "0",  # a stray character that isn't Base58, at either end
        XPRV + "l",
        "0" + XPRV,  # a "0x"-style prefix
        WIF_COMPRESSED + "O",
        "0" + WIF_COMPRESSED,
        "l" + WIF_UNCOMPRESSED,
        WIF_COMPRESSED[:20] + "0" + WIF_COMPRESSED[20:],  # a non-Base58 typo inside the key
        WIF_COMPRESSED + "0abc" * 5,  # something glued after the key
        XPRV[:50] + "I" + XPRV[50:],
    ],
)
def test_private_key_material_is_refused_t703(text: str) -> None:
    assert has_private_material(text)
    with pytest.raises(PrivateKeyError) as e:
        refuse_private(text)
    assert text not in str(e.value) and "public descriptor" in str(e.value)


@pytest.mark.parametrize(
    "text",
    [
        XPUB,
        f"wpkh([d34db33f/84h/0h/0h]{XPUB}/0/*)#abcdefgh",
        f"tr({XPUB}/86h/0h/0h/0/*)",
        "addr(1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa)",
        "addr(bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0)",
        "pk(0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798)",
        "raw(6a0b68656c6c6f20776f726c64)",
        "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    ],
)
def test_public_material_passes_unchanged_t703(text: str) -> None:
    assert not has_private_material(text) and refuse_private(text) == text


def test_a_key_shaped_run_inside_an_xpub_is_not_a_separate_key() -> None:
    # Matching is by whole token: an xpub is one token starting with its own prefix, so no 48-character
    # slice of it (one starting with K, say) counts as a WIF key.
    slices = [XPUB[i : i + 52] for i in range(len(XPUB) - 51) if XPUB[i] in "59KLc"]
    assert slices  # the xpub does hold WIF-shaped slices
    assert not has_private_material(XPUB) and not has_private_material(f"wpkh({XPUB}/0/*)")


def test_a_wif_shaped_run_inside_a_bech32_address_is_not_a_key_t703() -> None:
    # In lower-case bech32, "0" and "l" aren't Base58: a 52-character run starting with "c" between
    # them looked like a WIF key to a Base58-only boundary. Tokens are now whole runs of letters and
    # digits, so the address is one token and passes.
    run = "c" + "qpzry9x8gf2tvdw" * 4  # Base58 and bech32 characters only
    address = "bc1p0" + run[:51] + "l" + "qqqq"
    assert len(run[:51]) == 51 and not has_private_material(address)
    assert not has_private_material(address.upper())


def test_hex_scripts_and_keys_are_never_taken_for_wif_t703() -> None:
    assert not has_private_material("raw(c" + "1234567" * 7 + "a)")  # a 51-digit hex run starting with "c"
    assert not has_private_material("pk(5" + "abcdef12" * 6 + "3)")


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def synthetic(first: str, length: int) -> str:
    """A Base58 run that isn't hex (it holds letters past f), `length` characters long."""
    return (first + (B58 * 2))[:length]


@pytest.mark.parametrize("first", ["5", "9", "K", "L", "c"])
def test_a_wif_shaped_token_is_48_characters_or_more_t703(first: str) -> None:
    assert all(has_private_material(synthetic(first, n)) for n in (48, 56, 57, 100))
    assert not has_private_material(synthetic(first, 47))
    assert not has_private_material(synthetic("b", 60))  # any other first character


def test_an_extended_key_prefix_needs_20_characters_after_it_t703() -> None:
    assert has_private_material("xprv" + B58[:20])
    assert not has_private_material("xprv" + B58[:19])


@pytest.mark.parametrize(
    "text",
    [
        "BC1P" + "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" * 2,  # upper-case bech32, 68 characters
        "bcrt1q" + "qpzry9x8gf2tvdw0s3jn54khce6mua7l",
        "c" + "0123456789abcdef" * 3,  # hex only
        "0x5" + "1" * 50,  # hex with a prefix
    ],
)
def test_long_public_tokens_are_not_keys_t703(text: str) -> None:
    assert not has_private_material(text)

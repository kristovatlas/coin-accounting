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


def test_a_key_shaped_run_inside_a_longer_base58_run_is_not_a_separate_key() -> None:
    # An xpub is one long base58 run; no 51- or 52-character slice of it counts as a WIF key.
    assert not has_private_material("K" + XPUB[4:])

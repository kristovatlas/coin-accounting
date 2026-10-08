"""Address parsing for imports: strict, offline, per chain (PLAN §2-3; THREAT_MODEL T-701).

`parse_address` accepts a Base58Check address (P2PKH, P2SH; checksum per Bitcoin's Base58Check) or a
segwit address (BIP173 bech32 for version 0, BIP350 bech32m for versions 1-16), checks that it
belongs to the given chain, and returns its output script. Addresses are keyed by that script
(PLAN §2: "keyed by scripthash"), so the same script imported twice is one address. Anything that
doesn't parse exactly is refused, never guessed at, and the error never repeats the input (T-403).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

_BASE58: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32: Final = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST: Final = 1
_BECH32M_CONST: Final = 0x2BC830A3
MAX_ADDRESS_CHARS: Final = 90  # BIP173's limit; Base58 addresses are far shorter


@dataclass(frozen=True, slots=True)
class Network:
    hrp: str
    p2pkh: int
    p2sh: int


# Keyed by the chain name Core reports (`getblockchaininfo.chain`), as recorded in the user DB.
NETWORKS: Final = {
    "main": Network("bc", 0x00, 0x05),
    "test": Network("tb", 0x6F, 0xC4),
    "testnet4": Network("tb", 0x6F, 0xC4),
    "signet": Network("tb", 0x6F, 0xC4),
    "regtest": Network("bcrt", 0x6F, 0xC4),
}


@dataclass(frozen=True, slots=True)
class Address:
    text: str  # as the user's wallets show it (bech32 in lower case)
    script_hex: str  # the output script it pays to


class AddressError(ValueError):
    """Not an address of this chain. The message names the reason, never the input."""


def parse_address(text: str, chain: str) -> Address:
    network = NETWORKS.get(chain)
    if network is None:
        raise AddressError("the chain is unknown")
    if not text or len(text) > MAX_ADDRESS_CHARS or not text.isascii() or not text.isprintable():
        raise AddressError("not an address")
    if "1" in text and text.lower().startswith(network.hrp + "1"):
        return _segwit(text, network)
    if text.lower().startswith(("bc1", "tb1", "bcrt1")):
        raise AddressError("an address of another chain")
    return _base58(text, network)


def _base58(text: str, network: Network) -> Address:
    n = 0
    for c in text:
        i = _BASE58.find(c)
        if i < 0:
            raise AddressError("not an address")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
    if len(raw) != 25:
        raise AddressError("not an address")
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise AddressError("the address checksum is wrong")
    version, h = payload[0], payload[1:].hex()
    if version == network.p2pkh:
        return Address(text, f"76a914{h}88ac")
    if version == network.p2sh:
        return Address(text, f"a914{h}87")
    raise AddressError("an address of another chain")


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: list[int], frombits: int, tobits: int) -> bytes | None:
    acc, bits, out = 0, 0, bytearray()
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None  # BIP173: padding of more than 4 bits, or non-zero padding, is invalid
    return bytes(out)


def _segwit(text: str, network: Network) -> Address:
    if text != text.lower() and text != text.upper():
        raise AddressError("an address mixes upper and lower case")
    lowered = text.lower()
    sep = lowered.rfind("1")
    hrp, data_part = lowered[:sep], lowered[sep + 1 :]
    if hrp != network.hrp:
        raise AddressError("an address of another chain")
    if len(data_part) < 7 or any(c not in _BECH32 for c in data_part):
        raise AddressError("not an address")
    data = [_BECH32.index(c) for c in data_part]
    const = _polymod(_hrp_expand(hrp) + data)
    version, program5 = data[0], data[1:-6]
    if version > 16:
        raise AddressError("not an address")
    if const != (_BECH32_CONST if version == 0 else _BECH32M_CONST):
        raise AddressError("the address checksum is wrong")  # BIP350: v0 is bech32, v1+ bech32m
    program = _convert_bits(program5, 5, 8)
    if program is None or not 2 <= len(program) <= 40 or (version == 0 and len(program) not in (20, 32)):
        raise AddressError("not an address")
    op = "00" if version == 0 else f"{0x50 + version:02x}"
    return Address(lowered, f"{op}{len(program):02x}{program.hex()}")

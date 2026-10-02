"""Mask chain identifiers and amounts in log text (THREAT_MODEL T-403).

Logs live on the volume, but they are the place data most often leaks from (support requests,
pasted tracebacks), so by default they never hold what would tie the user to coins: keys and
descriptors, addresses, txids and other hashes, amounts and fiat values. The patterns err towards
masking too much; a masked block height or version number is an acceptable loss.
"""

from __future__ import annotations

import re
from typing import Final

_BASE58: Final = "1-9A-HJ-NP-Za-km-z"
_BECH32: Final = "ac-hj-np-z02-9"

# Order matters: the longest, most specific forms first.
_RULES: Final = (
    # Extended keys (xpub/tpub/ypub/zpub/upub/vpub and the private forms), inside descriptors too.
    (re.compile(rf"\b[xtyzuvXTYZUV]p(?:ub|rv)[{_BASE58}]{{100,112}}"), "<key>"),
    # WIF private keys (mainnet 5/K/L, testnet 9/c).
    (re.compile(rf"\b[5KL9c][{_BASE58}]{{50,51}}\b"), "<key>"),
    # Hex of 64+ digits: txids, block hashes, x-only and compressed public keys, scripts.
    (re.compile(r"\b[0-9a-fA-F]{64,}\b"), "<hex>"),
    # Bech32/bech32m addresses on every network (case-insensitive by the spec).
    (re.compile(rf"\b(?:bc|tb|bcrt)1[{_BECH32}]{{6,87}}\b", re.IGNORECASE), "<address>"),
    # Base58 addresses: mainnet 1/3, testnet and regtest m/n/2.
    (re.compile(rf"\b[13mn2][{_BASE58}]{{25,34}}\b"), "<address>"),
    # Fiat with a currency sign, with or without grouping commas.
    (re.compile(r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?"), "<amount>"),
    # Decimal amounts (BTC and fiat): two or more fractional digits.
    (re.compile(r"(?<![\w.])\d[\d,]*\.\d{2,}(?![\w.])"), "<amount>"),
    # Long integers: satoshi amounts, heights, timestamps.
    (re.compile(r"(?<![\w.])\d{5,}(?![\w.])"), "<number>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text

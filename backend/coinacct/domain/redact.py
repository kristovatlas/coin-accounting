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

_UNITS: Final = r"(?:BTC|mBTC|bits?|sats?|satoshis?|USD|EUR|GBP|JPY|CHF|CAD|AUD)"
_CURRENCY_CODES: Final = r"(?:BTC|USD|EUR|GBP|JPY|CHF|CAD|AUD)"
# Digits, or digits grouped by commas in threes: never `[\d,]*`, which backtracks quadratically on a
# long run like `1,2,3,…`.
_INTEGER: Final = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_NUMBER: Final = rf"{_INTEGER}(?:\.\d+)?(?:[eE][-+]?\d+)?"
# Ends a number: no word character and no further digits (a sentence's full stop is fine).
_END: Final = r"(?!\w)(?!\.\d)(?!,\d)"

# Order matters: credentials and keys first, then addresses, hex, and the amount forms from the
# most specific to the most general.
_RULES: Final = (
    # Credentials: HTTP authentication headers and schemes, and the bootstrap token (T-201, T-110).
    (re.compile(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?!(?:basic|bearer)\b)\S+"), r"\1<credential>"),
    (re.compile(r"(?i)\b(basic|bearer)\s+[A-Za-z0-9._~+/-]+=*"), r"\1 <credential>"),
    (re.compile(r"(?i)\b(bootstrap=)[A-Za-z0-9_-]+"), r"\1<credential>"),
    # Extended keys (xpub/tpub/ypub/zpub/upub/vpub and the private forms), inside descriptors too.
    (re.compile(rf"\b[xtyzuvXTYZUV]p(?:ub|rv)[{_BASE58}]{{100,112}}"), "<key>"),
    # WIF private keys (mainnet 5/K/L, testnet 9/c).
    (re.compile(rf"\b[5KL9c][{_BASE58}]{{50,51}}\b"), "<key>"),
    # Descriptor key origins: the master fingerprint and the derivation path.
    (re.compile(r"\[[0-9a-fA-F]{8}(?:/\d+['hH]?)*\]"), "[<origin>]"),
    # Bech32/bech32m addresses on every network (case-insensitive by the spec).
    (re.compile(rf"\b(?:bc|tb|bcrt)1[{_BECH32}]{{6,87}}\b", re.IGNORECASE), "<address>"),
    # Base58 addresses: mainnet 1/3, testnet and regtest m/n/2.
    (re.compile(rf"\b[13mn2][{_BASE58}]{{25,34}}\b"), "<address>"),
    # Hex of 40+ digits: txids, block hashes, public keys, HASH160s and scripts (P2WPKH is 44).
    # Bounded by non-hex characters, so `0x…` and `…i0` forms are covered too.
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])"), "<hex>"),
    # Any number with a unit or a currency code, whatever its size: `1 BTC`, `1500sat`, `USD 4321`.
    (re.compile(rf"(?i)(?<![\w.,]){_NUMBER}(\s?)({_UNITS})\b"), r"<amount>\1\2"),
    (re.compile(rf"(?i)\b({_CURRENCY_CODES})(\s?){_INTEGER}(?:\.\d+)?{_END}"), r"\1\2<amount>"),
    # Fiat with a currency sign, with or without grouping commas.
    (re.compile(rf"[$€£¥]\s?{_INTEGER}(?:\.\d+)?"), "<amount>"),
    # Decimal amounts (BTC and fiat): two or more fractional digits, or an exponent.
    (re.compile(rf"(?<![\w.,]){_INTEGER}\.\d{{2,}}(?:[eE][-+]?\d+)?{_END}"), "<amount>"),
    (re.compile(rf"(?<![\w.,])\d+(?:\.\d+)?[eE][-+]?\d+{_END}"), "<amount>"),
    # Long and digit-grouped integers: satoshi amounts, heights, timestamps.
    (re.compile(rf"(?<![\w.,])(?:\d{{5,}}|\d{{1,3}}(?:,\d{{3}})+){_END}"), "<number>"),
)


# A comma that isn't a thousands separator (one not followed by exactly three digits): values
# joined by one, as in compact JSON or a CSV row, are redacted as separate pieces.
_LIST_COMMA: Final = re.compile(r"(,(?!\d{3}(?!\d)))")


def redact(text: str) -> str:
    return "".join(_redact_piece(piece) for piece in _LIST_COMMA.split(text))


def _redact_piece(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text

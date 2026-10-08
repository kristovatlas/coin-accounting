"""Private key material in imported text (ADR 0019; THREAT_MODEL T-703).

The app imports public material only. **A requirement on every import path:** each address list and
descriptor field passes `refuse_private` in exactly the decoded, normalised form that is then stored,
sent to the node (`getdescriptorinfo`, `deriveaddresses` and `scanblocks` all accept private keys) or
logged, so no later decoding step can turn a checked string into a key.

The check errs towards refusing. A token (a run of letters and digits) is refused when it is
- an extended private key: `xprv`, `tprv` or a SLIP-132 form (`yprv`, `zprv`, `uprv`, `vprv`, `Yprv`,
  `Zprv`, `Uprv`, `Vprv`), followed by 20 or more Base58 characters. A truncated key, or one with a
  stray character added, still holds most of the key, so the length isn't checked;
- shaped like a WIF key: 48 to 56 Base58 characters starting with `5` or `9` (uncompressed) or `K`,
  `L` or `c` (compressed), a margin around WIF's 51 and 52 for a character dropped or doubled while
  copying. The checksum isn't checked either. A token of hex digits only is never a WIF key in
  practice, so scripts and public keys in hex pass.

Tokens are bounded by any character that isn't a letter or digit, so a run inside a longer token (a
bech32 address, an xpub) is never taken for a key on its own. The error never repeats the text, so
the key can't reach a log or a response through it.
"""

from __future__ import annotations

import re
from typing import Final

_BASE58: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_TOKEN: Final = re.compile(r"[A-Za-z0-9]+")
_EXTENDED_PRIVATE: Final = re.compile(rf"[xtyzuvYZUV]prv[{_BASE58}]{{20,}}")
_WIF: Final = re.compile(rf"[59KLc][{_BASE58}]{{47,55}}")
_HEX: Final = re.compile(r"[0-9a-fA-F]+")

HOW_TO_EXPORT: Final = (
    "Only public material can be imported. Export the wallet's public descriptor or xpub instead "
    "(in Bitcoin Core: listdescriptors without the private flag; in a hardware or software wallet: "
    "its 'export xpub' or 'export public descriptor' option)."
)


class PrivateKeyError(ValueError):
    """The text holds private key material. The message never contains the text."""

    def __init__(self) -> None:
        super().__init__(f"the import contains a private key, so nothing was imported. {HOW_TO_EXPORT}")


def has_private_material(text: str) -> bool:
    for token in _TOKEN.findall(text):
        if _EXTENDED_PRIVATE.fullmatch(token):
            return True
        if _WIF.fullmatch(token) and not _HEX.fullmatch(token):
            return True
    return False


def refuse_private(text: str) -> str:
    """`text`, unchanged, if it holds no private key material; `PrivateKeyError` otherwise."""
    if has_private_material(text):
        raise PrivateKeyError
    return text

"""Private key material in imported text (ADR 0019; THREAT_MODEL T-703).

The app imports public material only. Every address list and descriptor passes `refuse_private` before
it is stored, sent to the node (`getdescriptorinfo`, `deriveaddresses` and `scanblocks` all accept
private keys) or logged. The check errs towards refusing: an extended private key (`xprv`, `tprv` and
the SLIP-132 forms) or anything shaped like a WIF key is refused, whether or not its checksum is
valid. The error never repeats the text, so the key can't reach a log or a response through it.
"""

from __future__ import annotations

import re
from typing import Final

_BASE58: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Extended private keys: BIP32 xprv/tprv and the SLIP-132 yprv/zprv/uprv/vprv/Yprv/Zprv/Uprv/Vprv.
_EXTENDED_PRIVATE: Final = re.compile(
    rf"(?<![{_BASE58}])[xtyzuvYZUV]prv[{_BASE58}]{{100,112}}(?![{_BASE58}])"
)
# WIF: 51 characters (uncompressed: 5 on mainnet, 9 on testnet) or 52 (compressed: K/L, c).
_WIF: Final = re.compile(rf"(?<![{_BASE58}])(?:[59][{_BASE58}]{{50}}|[KLc][{_BASE58}]{{51}})(?![{_BASE58}])")

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
    return bool(_EXTENDED_PRIVATE.search(text) or _WIF.search(text))


def refuse_private(text: str) -> str:
    """`text`, unchanged, if it holds no private key material; `PrivateKeyError` otherwise."""
    if has_private_material(text):
        raise PrivateKeyError
    return text

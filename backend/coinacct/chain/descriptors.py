"""Public descriptors through the node: normalise them, and derive their scripts (PLAN §1, §3;
ADR 0019; THREAT_MODEL T-701, T-703).

`describe` checks a descriptor with `getdescriptorinfo` and returns Core's normal form (with its
checksum). `derive` asks `deriveaddresses` for the addresses at a range of indexes; the caller turns
them into scripts with `domain.addresses`. No node wallet is used (ADR 0004).

The text must already have passed `domain.keys.refuse_private`: both calls accept private keys.
`describe` also refuses a descriptor the node says has private keys, as a second check. A node error
never reaches a message or a log: Core quotes the descriptor, and so its keys, back in its errors
(`rpc.RpcCallError.node_message`), so every error here names only what kind of problem it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from coinacct.chain.txs import ChainRpc
from coinacct.rpc import RpcCallError

# The most indexes one `deriveaddresses` call derives (PLAN §1: windows are sized by the perf check).
MAX_DERIVE: Final = 1000
MAX_DESCRIPTOR_CHARS: Final = 4000


class DescriptorError(ValueError):
    """The node refused the descriptor, or it isn't usable for an import. Never the node's text."""


@dataclass(frozen=True, slots=True)
class DescriptorInfo:
    text: str  # Core's normal form, with its checksum
    is_range: bool
    is_solvable: bool


def describe(rpc: ChainRpc, text: str) -> DescriptorInfo:
    if not text or len(text) > MAX_DESCRIPTOR_CHARS or not text.isascii() or not text.isprintable():
        raise DescriptorError("not a descriptor")
    if text.lstrip().startswith("combo("):
        # deriveaddresses returns combo()'s several addresses per index without their indexes.
        raise DescriptorError("a combo() descriptor: import each of its script types as its own descriptor")
    if "<" in text:
        raise DescriptorError(
            "a multipath descriptor (<0;1>): import the receive and change descriptors separately"
        )
    try:
        info = rpc.call("getdescriptorinfo", [text])
    except RpcCallError:
        raise DescriptorError("the node can't read this descriptor") from None
    if not isinstance(info, dict) or not all(
        isinstance(info.get(k), t) for k, t in (("descriptor", str), ("isrange", bool), ("issolvable", bool))
    ):
        raise DescriptorError("the node's reply about the descriptor is malformed")
    if info.get("hasprivatekeys") is not False:
        raise DescriptorError("the node says the descriptor holds private keys")
    normal = info["descriptor"]
    if not normal or len(normal) > MAX_DESCRIPTOR_CHARS or "#" not in normal:
        raise DescriptorError("the node's reply about the descriptor is malformed")
    return DescriptorInfo(normal, info["isrange"], info["issolvable"])


def derive(rpc: ChainRpc, info: DescriptorInfo, start: int, end: int) -> list[str]:
    """The addresses at indexes `start`..`end` (inclusive) of a ranged descriptor, or the one address
    of an unranged one (then `start` and `end` must be 0)."""
    if info.is_range:
        if not 0 <= start <= end or end - start + 1 > MAX_DERIVE:
            raise ValueError("a derive range is 0 <= start <= end, at most MAX_DERIVE indexes")
        params: list[object] = [info.text, [start, end]]
        expected = end - start + 1
    else:
        if start != 0 or end != 0:
            raise ValueError("an unranged descriptor has only index 0")
        params, expected = [info.text], 1
    try:
        addresses = rpc.call("deriveaddresses", params)
    except RpcCallError:
        # Among others, a script with no address form (pk(), raw(), bare multi()).
        raise DescriptorError("the node can't derive addresses from this descriptor") from None
    if (
        not isinstance(addresses, list)
        or len(addresses) != expected
        or not all(isinstance(a, str) for a in addresses)
    ):
        raise DescriptorError("the node's derived addresses are malformed")
    return addresses

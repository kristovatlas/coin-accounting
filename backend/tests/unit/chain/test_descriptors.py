"""Public descriptors through the node (PLAN §1, §3; THREAT_MODEL T-403, T-701, T-703): every reply is
checked, and the node's own error text never reaches a message."""

from __future__ import annotations

from typing import Any

import pytest

from coinacct.chain import descriptors
from coinacct.chain.descriptors import DescriptorError, DescriptorInfo
from coinacct.rpc import RpcCallError

GOOD: dict[str, Any] = {
    "descriptor": "wpkh(tpubx/0/*)#abcdefgh",
    "isrange": True,
    "issolvable": True,
    "hasprivatekeys": False,
}


class Node:
    def __init__(self, reply: Any = None, derived: Any = None) -> None:
        self.reply, self.derived = reply, derived

    def call(self, method: str, params: Any = ()) -> Any:
        if method == "getdescriptorinfo":
            return self.reply
        if method == "deriveaddresses":
            if isinstance(self.derived, Exception):
                raise self.derived
            return self.derived
        raise AssertionError(method)


@pytest.mark.parametrize(
    "text", ["", "x" * (descriptors.MAX_DESCRIPTOR_CHARS + 1), "wpkh(tpubé)", "wpkh(a\nb)"]
)
def test_text_that_cant_be_a_descriptor_is_refused_before_the_node(text: str) -> None:
    with pytest.raises(DescriptorError, match="not a descriptor"):
        descriptors.describe(Node(), text)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        {**GOOD, "descriptor": 5},
        {**GOOD, "isrange": "yes"},
        {k: v for k, v in GOOD.items() if k != "issolvable"},
        {**GOOD, "descriptor": "wpkh(tpubx/0/*)"},  # no checksum
        {**GOOD, "descriptor": ""},
    ],
)
def test_a_malformed_reply_is_refused(reply: Any) -> None:
    with pytest.raises(DescriptorError, match="malformed"):
        descriptors.describe(Node(reply), "wpkh(tpubx/0/*)")


@pytest.mark.parametrize("flag", [True, None, "false"])
def test_only_a_plain_false_private_key_flag_passes_t703(flag: Any) -> None:
    reply = (
        {**GOOD, "hasprivatekeys": flag}
        if flag is not None
        else {k: v for k, v in GOOD.items() if k != "hasprivatekeys"}
    )
    with pytest.raises(DescriptorError, match="private keys"):
        descriptors.describe(Node(reply), "wpkh(tpubx/0/*)")


def test_describe_returns_the_nodes_normal_form() -> None:
    assert descriptors.describe(Node(GOOD), "wpkh(tpubx/0/*)") == DescriptorInfo(
        GOOD["descriptor"], True, True
    )


RANGED = DescriptorInfo("wpkh(tpubx/0/*)#abcdefgh", is_range=True, is_solvable=True)
SINGLE = DescriptorInfo("wpkh(tpubx/0/7)#abcdefgh", is_range=False, is_solvable=True)


@pytest.mark.parametrize(
    ("info", "start", "end"),
    [(RANGED, -1, 3), (RANGED, 4, 3), (RANGED, 0, descriptors.MAX_DERIVE), (SINGLE, 0, 1)],
)
def test_a_derive_range_out_of_bounds_is_a_caller_error(info: DescriptorInfo, start: int, end: int) -> None:
    with pytest.raises(ValueError, match=r"range|index"):
        descriptors.derive(Node(), info, start, end)


@pytest.mark.parametrize("derived", [None, ["bcrt1qa"], ["bcrt1qa", 5], {"a": 1}])
def test_malformed_derived_addresses_are_refused(derived: Any) -> None:
    with pytest.raises(DescriptorError, match="malformed"):
        descriptors.derive(Node(derived=derived), RANGED, 0, 1)


def test_a_node_error_while_deriving_never_reaches_the_message_t403() -> None:
    error = RpcCallError(
        "deriveaddresses", -5, "Descriptor 'wpkh(tpubsecretish/0/*)' does not have an address"
    )
    with pytest.raises(DescriptorError) as e:
        descriptors.derive(Node(derived=error), RANGED, 0, 1)
    assert "tpubsecretish" not in str(e.value) and e.value.__cause__ is None


def test_an_unranged_descriptor_derives_its_one_address() -> None:
    assert descriptors.derive(Node(derived=["bcrt1qa"]), SINGLE, 0, 0) == ["bcrt1qa"]

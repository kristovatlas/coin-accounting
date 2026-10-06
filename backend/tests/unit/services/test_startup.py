"""Start-up node checks and offline mode (architecture §3, §8.1; THREAT_MODEL T-203, T-206, T-401).

A small in-memory node stands in for Bitcoin Core; the node checks themselves are tested in
unit/chain and against regtest. These tests cover what `services/startup` adds: the online/offline
decision, the reasons, and the deferred unencrypted-storage policy.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from coinacct.chain.node_checks import REQUIRED_INDEXES
from coinacct.rpc import RpcAuthError, RpcTransportError
from coinacct.services.startup import StorageRefused, check_at_startup
from coinacct.storage.volume import Encryption, VolumeStatus

ENCRYPTED = VolumeStatus(Encryption.VERACRYPT, "device-mapper veracrypt1")
PLAIN = VolumeStatus(Encryption.NONE, "device 8:1 is not a device-mapper device")


class Node:
    """Answers the start-up calls like a healthy Core 31.1 node unless told otherwise."""

    def __init__(
        self,
        *,
        chain: str = "regtest",
        pruned: bool = False,
        canary: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.chain, self.pruned, self.canary, self.error = chain, pruned, canary, error

    def call(self, method: str, params: Any = ()) -> Any:
        if self.error is not None:
            raise self.error
        return {
            "getnetworkinfo": {"version": 310100, "subversion": "/Satoshi:31.1.0/"},
            "getblockchaininfo": {"chain": self.chain, "pruned": self.pruned},
            "getblockcount": 200,
            "getindexinfo": {name: {"synced": True, "best_block_height": 200} for name in REQUIRED_INDEXES},
        }[method]

    def canary_refused(self) -> bool:
        return self.canary


def start(
    node: Node,
    *,
    expected_chain: str | None = None,
    volume: VolumeStatus = ENCRYPTED,
    allow_unencrypted: bool = False,
) -> Any:
    return check_at_startup(
        node,
        expected_chain=expected_chain,
        volume=volume,
        allow_unencrypted=allow_unencrypted,
        sync_attempts=1,
    )


def test_a_healthy_node_means_online() -> None:
    status = start(Node())
    assert status.online
    assert status.chain == "regtest"
    assert status.reasons == ()


def test_a_node_problem_means_offline_with_the_reason_not_an_error() -> None:
    status = start(Node(pruned=True))
    assert not status.online
    assert status.chain == "regtest"
    assert status.reasons == ("the node is pruned; an unpruned node is required",)


def test_a_missing_whitelist_means_offline_t203() -> None:
    status = start(Node(canary=False))
    assert not status.online
    assert any("rpcwhitelist isn't active" in reason for reason in status.reasons)


def test_an_unreachable_node_means_offline_and_no_chain() -> None:
    status = start(Node(error=RpcTransportError("connection refused")))
    assert not status.online
    assert status.chain is None
    assert status.reasons == ("the node can't be reached: connection refused",)


def test_bad_credentials_mean_offline() -> None:
    status = start(Node(error=RpcAuthError("HTTP 401")))
    assert (status.online, status.reasons) == (False, ("the node refused the RPC credentials",))


def test_a_node_on_another_chain_than_the_data_directory_means_offline_t206() -> None:
    status = start(Node(chain="signet"), expected_chain="regtest")
    assert not status.online
    assert status.chain == "signet"
    assert any("different chain" in reason for reason in status.reasons)


def test_each_reason_is_logged_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="coinacct.services.startup"):
        start(Node(pruned=True))
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "offline mode: the node is pruned" in caplog.records[0].getMessage()


def test_encrypted_storage_is_fine_on_any_chain() -> None:
    assert start(Node(chain="main"), volume=ENCRYPTED).online


def test_unencrypted_storage_without_permission_is_refused_t401() -> None:
    with pytest.raises(StorageRefused, match="VeraCrypt"):
        start(Node(), volume=PLAIN, allow_unencrypted=False)


@pytest.mark.parametrize("chain", ["regtest", "signet", "test", "testnet4"])
def test_allowed_unencrypted_storage_runs_on_a_test_chain_t401(chain: str) -> None:
    assert start(Node(chain=chain), volume=PLAIN, allow_unencrypted=True).online


def test_allowed_unencrypted_storage_is_refused_on_mainnet_t401() -> None:
    with pytest.raises(StorageRefused, match="never accepted on mainnet"):
        start(Node(chain="main"), volume=PLAIN, allow_unencrypted=True)


def test_allowed_unencrypted_storage_is_refused_when_the_chain_is_unknown_t401() -> None:
    # The node didn't answer, so nothing shows this is a test chain: refuse rather than guess.
    with pytest.raises(StorageRefused, match="never accepted on mainnet"):
        start(Node(error=RpcTransportError("down")), volume=PLAIN, allow_unencrypted=True)


def test_the_data_directorys_chain_decides_over_the_nodes_t401() -> None:
    # A mainnet data directory stays refused even if a regtest node is attached to it.
    with pytest.raises(StorageRefused):
        start(Node(chain="regtest"), expected_chain="main", volume=PLAIN, allow_unencrypted=True)


def test_a_test_chain_data_directory_runs_offline_while_the_node_is_down_t401() -> None:
    status = start(
        Node(error=RpcTransportError("down")), expected_chain="regtest", volume=PLAIN, allow_unencrypted=True
    )
    assert not status.online

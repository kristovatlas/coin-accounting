"""Integration fixtures: a regtest node set up the way the app requires (ADR 0004)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from coinacct.domain.secret import Secret
from coinacct.rpc import RpcClient
from harness.regtest import RegtestNode, regtest_node


@pytest.fixture(scope="module")
def node(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    """A correctly configured node with 101 blocks (so the first coinbase is mature)."""
    with regtest_node(tmp_path_factory.mktemp("regtest")) as n:
        n.mine(101)
        yield n


def app_client(node: RegtestNode, password: str | None = None) -> RpcClient:
    return RpcClient("127.0.0.1", node.port, node.app_user, Secret(password or node.app_password), timeout=30)

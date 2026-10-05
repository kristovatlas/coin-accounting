"""Node requirement checks (ADR 0004; architecture §8.1; THREAT_MODEL T-203, T-206, T-210).

`evaluate` is pure and is tested with hand-built facts, including nodes regtest can't be (an old
version, a pruned node). `gather`'s retry and error mapping use a small in-memory client; the
same checks run against a real regtest node in tests/integration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from coinacct.chain.node_checks import (
    REQUIRED_INDEXES,
    IndexState,
    NodeFacts,
    Problem,
    check_node,
    evaluate,
)
from coinacct.rpc import RpcAuthError, RpcForbiddenError, RpcTransportError


def facts(**overrides: Any) -> NodeFacts:
    values: dict[str, Any] = {
        "version": 310100,
        "subversion": "/Satoshi:31.1.0/",
        "chain": "regtest",
        "pruned": False,
        "canary_refused": True,
        "block_count": 200,
        "indexes": {name: IndexState(synced=True, best_block_height=200) for name in REQUIRED_INDEXES},
    }
    values.update(overrides)
    return NodeFacts(**values)


def problems(f: NodeFacts, expected_chain: str | None = "regtest") -> list[Problem]:
    return [finding.problem for finding in evaluate(f, expected_chain)]


def test_a_node_meeting_every_requirement_has_no_findings() -> None:
    assert evaluate(facts(), "regtest") == []


def test_a_new_data_directory_accepts_any_known_chain() -> None:
    assert problems(facts(chain="main"), expected_chain=None) == []


@pytest.mark.parametrize(
    ("version", "too_old"), [(309900, True), (300000, True), (310000, False), (320000, False)]
)
def test_core_31_0_is_the_minimum_version(version: int, too_old: bool) -> None:
    assert (Problem.VERSION_TOO_OLD in problems(facts(version=version))) is too_old


def test_a_chain_other_than_the_data_directorys_is_refused_t206() -> None:
    findings = evaluate(facts(chain="main"), "regtest")
    assert [f.problem for f in findings] == [Problem.CHAIN_MISMATCH]
    assert "node: main, data directory: regtest" in str(findings[0])


def test_an_unknown_chain_is_refused() -> None:
    assert problems(facts(chain="liquidv1")) == [Problem.UNKNOWN_CHAIN]


def test_a_pruned_node_is_refused_t210() -> None:
    assert problems(facts(pruned=True)) == [Problem.PRUNED]


def test_an_answered_canary_means_the_whitelist_is_missing_t203() -> None:
    assert problems(facts(canary_refused=False)) == [Problem.WHITELIST_MISSING]


@pytest.mark.parametrize("missing", REQUIRED_INDEXES)
def test_each_index_is_required_t210(missing: str) -> None:
    indexes = {n: IndexState(True, 200) for n in REQUIRED_INDEXES if n != missing}
    findings = evaluate(facts(indexes=indexes), "regtest")
    assert [(f.problem, f.detail) for f in findings] == [(Problem.INDEX_MISSING, missing)]


@pytest.mark.parametrize(("synced", "height"), [(False, 200), (True, 199), (True, 201), (False, 150)])
def test_an_index_must_be_synced_and_at_the_tip_t210(synced: bool, height: int) -> None:
    indexes = {n: IndexState(True, 200) for n in REQUIRED_INDEXES}
    indexes["txospenderindex"] = IndexState(synced, height)
    assert problems(facts(indexes=indexes)) == [Problem.INDEX_NOT_SYNCED]


def test_every_problem_is_reported_not_just_the_first() -> None:
    f = facts(version=290000, chain="main", pruned=True, canary_refused=False, indexes={})
    assert (
        problems(f)
        == [Problem.VERSION_TOO_OLD, Problem.CHAIN_MISMATCH, Problem.PRUNED, Problem.WHITELIST_MISSING]
        + [Problem.INDEX_MISSING] * 3
    )


class FakeNode:
    """In-memory replies for the gather logic. The real node is exercised in tests/integration."""

    def __init__(
        self,
        index_readings: list[dict[str, Any]],
        *,
        canary: bool = True,
        network: Any = None,
        fail: Callable[[str], None] | None = None,
    ) -> None:
        self.index_readings = index_readings
        self.canary = canary
        self.network = (
            network if network is not None else {"version": 310100, "subversion": "/Satoshi:31.1.0/"}
        )
        self.fail = fail
        self.calls: list[str] = []

    def call(self, method: str, params: Any = ()) -> Any:
        self.calls.append(method)
        if self.fail:
            self.fail(method)
        if method == "getnetworkinfo":
            return self.network
        if method == "getblockchaininfo":
            return {"chain": "regtest", "pruned": False}
        if method == "getblockcount":
            return 200
        if method == "getindexinfo":
            return self.index_readings.pop(0) if len(self.index_readings) > 1 else self.index_readings[0]
        raise AssertionError(f"unexpected call {method}")

    def canary_refused(self) -> bool:
        self.calls.append("uptime")
        return self.canary


def reading(height: int, names: tuple[str, ...] = REQUIRED_INDEXES) -> dict[str, Any]:
    return {n: {"synced": height == 200, "best_block_height": height} for n in names}


def test_indexes_that_catch_up_are_retried_until_synced() -> None:
    node, delays = FakeNode([reading(198), reading(199), reading(200)]), list[float]()
    result = check_node(node, "regtest", sync_attempts=5, sync_delay=0.25, sleep=delays.append)
    assert result.ok
    assert delays == [0.25, 0.25]
    assert node.calls.count("getindexinfo") == 3


def test_indexes_that_never_catch_up_fail_after_the_last_attempt_t210() -> None:
    node, delays = FakeNode([reading(150)]), list[float]()
    result = check_node(node, "regtest", sync_attempts=3, sync_delay=1, sleep=delays.append)
    assert [f.problem for f in result.findings] == [Problem.INDEX_NOT_SYNCED] * 3
    assert len(delays) == 2


def test_a_missing_index_isnt_waited_for() -> None:
    node, delays = FakeNode([reading(200, names=("txindex",))]), list[float]()
    result = check_node(node, "regtest", sync_attempts=5, sleep=delays.append)
    assert delays == []
    assert [f.problem for f in result.findings] == [Problem.INDEX_MISSING, Problem.INDEX_MISSING]


def test_the_checks_run_in_the_documented_order() -> None:
    # Architecture §8.1: version, chain and pruning; the canary; then the indexes.
    node = FakeNode([reading(200)])
    check_node(node, "regtest")
    assert node.calls == ["getnetworkinfo", "getblockchaininfo", "uptime", "getblockcount", "getindexinfo"]


def raising(error: Exception) -> Callable[[str], None]:
    def fail(method: str) -> None:
        raise error

    return fail


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (RpcAuthError("x"), Problem.AUTH_FAILED),
        (RpcForbiddenError("getindexinfo refused"), Problem.METHOD_REFUSED),
        (RpcTransportError("down"), Problem.UNREACHABLE),
    ],
)
def test_rpc_errors_become_findings_not_exceptions(error: Exception, problem: Problem) -> None:
    result = check_node(FakeNode([reading(200)], fail=raising(error)), "regtest")
    assert [f.problem for f in result.findings] == [problem]
    assert result.facts is None and not result.ok


@pytest.mark.parametrize(
    "network",
    [{"version": "310100"}, {"version": True}, {"version": -1}, {"subversion": "x"}, [310100]],
)
def test_malformed_replies_fail_closed(network: Any) -> None:
    result = check_node(FakeNode([reading(200)], network=network), "regtest")
    assert [f.problem for f in result.findings] == [Problem.MALFORMED_REPLY]


def test_a_malformed_index_entry_fails_closed() -> None:
    bad = reading(200)
    bad["txindex"] = {"synced": "yes", "best_block_height": 200}
    result = check_node(FakeNode([bad]), "regtest")
    assert [f.problem for f in result.findings] == [Problem.MALFORMED_REPLY]

"""Startup node requirement checks (ADR 0004, architecture §8.1, THREAT_MODEL T-203, T-206, T-210).

`gather` makes the RPC calls; `evaluate` is pure and turns what the node reported into problems.
Any problem means the app disables chain RPC and runs in offline mode (architecture §3), so the
checks fail closed: a reply that can't be read counts as a problem, never as a pass.

The order follows §8.1: version, chain and pruning; the chain against the DB; the canary; then the
three indexes, retried briefly because they follow the tip asynchronously.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from coinacct.domain.secret import Secret
from coinacct.rpc import RpcAuthError, RpcClient, RpcError, RpcForbiddenError

# Bitcoin Core 31.1 (ADR 0029; 31.0 was the first release with txospenderindex, ADR 0004).
# getnetworkinfo reports the version as MMmmpp: 31.1.0 is 310100.
MIN_VERSION: Final = 310100
# Names as getindexinfo reports them (checked against Core 31.1 on regtest).
REQUIRED_INDEXES: Final = ("txindex", "basic block filter index", "txospenderindex")
KNOWN_CHAINS: Final = frozenset({"main", "test", "testnet4", "signet", "regtest"})

SYNC_ATTEMPTS: Final = 5
SYNC_DELAY_SECONDS: Final = 1.0


class Problem(enum.Enum):
    UNREACHABLE = "the node can't be reached"
    AUTH_FAILED = "the node refused the RPC credentials"
    METHOD_REFUSED = "the node's rpcwhitelist refuses a method the app needs"
    MALFORMED_REPLY = "the node sent a reply the app can't read"
    VERSION_TOO_OLD = "Bitcoin Core 31.1 or newer is required"
    UNKNOWN_CHAIN = "the node reports an unknown chain"
    CHAIN_MISMATCH = "the node is on a different chain than this data directory (T-206)"
    PRUNED = "the node is pruned; an unpruned node is required"
    WHITELIST_MISSING = "the node answered the canary call, so its rpcwhitelist isn't active (T-203)"
    INDEX_MISSING = "a required index is not enabled"
    INDEX_NOT_SYNCED = "a required index isn't synced to the tip"


@dataclass(frozen=True, slots=True)
class Finding:
    problem: Problem
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.problem.value}{': ' + self.detail if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class IndexState:
    synced: bool
    best_block_height: int


@dataclass(frozen=True, slots=True)
class NodeFacts:
    """What the node reported, already type-checked."""

    version: int
    subversion: str
    chain: str
    pruned: bool
    canary_refused: bool
    block_count: int
    indexes: Mapping[str, IndexState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NodeCheckResult:
    findings: tuple[Finding, ...]
    facts: NodeFacts | None

    @property
    def ok(self) -> bool:
        return not self.findings


class NodeRpc(Protocol):
    """The part of `coinacct.rpc.RpcClient` the checks use."""

    def call(self, method: str, params: Any = ()) -> Any: ...

    def canary_refused(self) -> bool: ...


class MalformedReplyError(ValueError):
    """A reply didn't have the expected shape."""


def connect(host: str, port: int, user: str, password: Secret, *, timeout: float | None = None) -> NodeRpc:
    """The node's RPC client. Only `chain/` reaches `rpc` (architecture §2), so the services that run
    the checks get their client here; the endpoint was checked to be loopback by `config`. `timeout`
    replaces the default per-call timeout (the shutdown abort's must be short)."""
    if timeout is None:
        return RpcClient(host, port, user, password)
    return RpcClient(host, port, user, password, timeout=timeout)


def evaluate(facts: NodeFacts, expected_chain: str | None) -> list[Finding]:
    """Pure: the problems with what the node reported. `expected_chain` is the chain recorded
    in the user DB, or None for a new data directory."""
    findings: list[Finding] = []
    if facts.version < MIN_VERSION:
        findings.append(
            Finding(Problem.VERSION_TOO_OLD, f"the node reports {facts.subversion or facts.version}")
        )
    if facts.chain not in KNOWN_CHAINS:
        findings.append(Finding(Problem.UNKNOWN_CHAIN, facts.chain))
    elif expected_chain is not None and facts.chain != expected_chain:
        findings.append(
            Finding(Problem.CHAIN_MISMATCH, f"node: {facts.chain}, data directory: {expected_chain}")
        )
    if facts.pruned:
        findings.append(Finding(Problem.PRUNED))
    if not facts.canary_refused:
        findings.append(Finding(Problem.WHITELIST_MISSING))
    for name in REQUIRED_INDEXES:
        state = facts.indexes.get(name)
        if state is None:
            findings.append(Finding(Problem.INDEX_MISSING, name))
        elif not index_synced(state, facts.block_count):
            findings.append(
                Finding(
                    Problem.INDEX_NOT_SYNCED,
                    f"{name} at {state.best_block_height}, tip at {facts.block_count}",
                )
            )
    return findings


def index_synced(state: IndexState, block_count: int) -> bool:
    # T-210: `synced` alone isn't enough; the index must also have reached the tip.
    return state.synced and state.best_block_height == block_count


def gather(
    client: NodeRpc,
    *,
    sync_attempts: int = SYNC_ATTEMPTS,
    sync_delay: float = SYNC_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> NodeFacts:
    """Ask the node. Raises RpcError for transport, auth and refusal errors and
    MalformedReplyError for a reply of the wrong shape."""
    network = _object(client.call("getnetworkinfo"), "getnetworkinfo")
    chain_info = _object(client.call("getblockchaininfo"), "getblockchaininfo")
    canary_refused = client.canary_refused()
    block_count, indexes = 0, {}
    for attempt in range(max(1, sync_attempts)):
        block_count = _int(client.call("getblockcount"), "getblockcount")
        indexes = _indexes(client.call("getindexinfo"))
        if all(name in indexes and index_synced(indexes[name], block_count) for name in REQUIRED_INDEXES):
            break
        if any(name not in indexes for name in REQUIRED_INDEXES):
            break  # a missing index won't appear by waiting
        if attempt + 1 < sync_attempts:
            sleep(sync_delay)
    return NodeFacts(
        version=_int(network.get("version"), "getnetworkinfo.version"),
        subversion=_str(network.get("subversion", ""), "getnetworkinfo.subversion"),
        chain=_str(chain_info.get("chain"), "getblockchaininfo.chain"),
        pruned=_bool(chain_info.get("pruned"), "getblockchaininfo.pruned"),
        canary_refused=canary_refused,
        block_count=block_count,
        indexes=indexes,
    )


def check_node(client: NodeRpc, expected_chain: str | None, **gather_options: Any) -> NodeCheckResult:
    """Run every check. Never raises for a node problem: an error becomes a finding."""
    try:
        facts = gather(client, **gather_options)
    except RpcAuthError:
        return NodeCheckResult((Finding(Problem.AUTH_FAILED),), None)
    except RpcForbiddenError as e:
        return NodeCheckResult((Finding(Problem.METHOD_REFUSED, str(e)),), None)
    except MalformedReplyError as e:
        return NodeCheckResult((Finding(Problem.MALFORMED_REPLY, str(e)),), None)
    except RpcError as e:
        return NodeCheckResult((Finding(Problem.UNREACHABLE, str(e)),), None)
    return NodeCheckResult(tuple(evaluate(facts, expected_chain)), facts)


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MalformedReplyError(f"{what} didn't return an object")
    return value


def _int(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MalformedReplyError(f"{what} isn't a non-negative integer")
    return value


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise MalformedReplyError(f"{what} isn't a string")
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise MalformedReplyError(f"{what} isn't true or false")
    return value


def _indexes(value: object) -> dict[str, IndexState]:
    out = {}
    for name, raw in _object(value, "getindexinfo").items():
        state = _object(raw, f"getindexinfo[{name!r}]")
        out[name] = IndexState(
            synced=_bool(state.get("synced"), f"getindexinfo[{name!r}].synced"),
            best_block_height=_int(
                state.get("best_block_height"), f"getindexinfo[{name!r}].best_block_height"
            ),
        )
    return out

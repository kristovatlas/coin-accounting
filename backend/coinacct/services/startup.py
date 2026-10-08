"""Start-up node checks and offline mode (architecture §3, §8.1; THREAT_MODEL T-203, T-206, T-210, T-401).

`check_at_startup` runs the node checks and decides whether the app runs **online** (chain RPC on) or
in **offline mode** (chain RPC off, everything else working from the cache). A node problem never
stops the app: it becomes a reason shown to the user.

It also settles the storage decision the launcher had to defer (T-401): unencrypted storage is only
ever allowed on a test chain, and the chain isn't known until the node has answered. If the chain
can't be shown to be a test chain, `StorageRefused` is raised and the app must not start.

`check_configured_node` checks the node's chain against the one recorded in the user DB (T-206): a
mismatch means offline mode. A new data directory records the node's chain on its first start that
passes every check, and from then on it never changes. Once online, the rest of §8.1 runs
(`chain_sync.at_startup`): a scan the app left running is aborted, then the fork-point check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from coinacct.chain import node_checks
from coinacct.domain.secret import Secret
from coinacct.services import chain_sync
from coinacct.storage import chain_state, datadir
from coinacct.storage.db import Connection
from coinacct.storage.volume import VolumeStatus

log = logging.getLogger(__name__)


class StorageRefused(Exception):
    """Unencrypted storage was allowed provisionally, but the chain isn't a test chain (T-401)."""


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """What the rest of the app needs to know about the node after start-up."""

    online: bool
    # The node's chain, if it answered (even when other checks failed).
    chain: str | None
    # Why the app is offline, in words for the user; empty when online.
    reasons: tuple[str, ...]


def check_at_startup(
    client: node_checks.NodeRpc,
    *,
    expected_chain: str | None,
    volume: VolumeStatus,
    allow_unencrypted: bool,
    **gather_options: Any,
) -> NodeStatus:
    """Run the node checks, then the deferred storage policy.

    `expected_chain` is the chain recorded for this data directory, or None for a new one. Raises
    `StorageRefused` when the storage policy forbids running; never raises for a node problem.
    """
    result = node_checks.check_node(client, expected_chain, **gather_options)
    node_chain = result.facts.chain if result.facts is not None else None
    # The data directory's own chain decides when it's known: a node on the wrong chain mustn't make
    # unencrypted mainnet data acceptable. An unknown chain counts as mainnet (storage_refusal).
    refusal = datadir.storage_refusal(
        volume, expected_chain if expected_chain is not None else node_chain, allow_unencrypted
    )
    if refusal is not None:
        raise StorageRefused(refusal)
    reasons = tuple(str(finding) for finding in result.findings)
    for reason in reasons:
        log.warning("offline mode: %s", reason)
    if not reasons:
        log.info("node checks passed (chain %s); chain access is on", node_chain)
    return NodeStatus(online=not reasons, chain=node_chain, reasons=reasons)


def check_configured_node(  # noqa: PLR0913 - the endpoint, its credentials and the storage policy inputs
    host: str,
    port: int,
    user: str,
    password: Secret,
    *,
    db: Connection,
    volume: VolumeStatus,
    allow_unencrypted: bool,
) -> NodeStatus:
    """`check_recorded` against the configured node (the launcher passes the config as values)."""
    client = node_checks.connect(host, port, user, password)
    return check_recorded(client, db=db, volume=volume, allow_unencrypted=allow_unencrypted)


def check_recorded(
    client: node_checks.NodeRpc,
    *,
    db: Connection,
    volume: VolumeStatus,
    allow_unencrypted: bool,
    **gather_options: Any,
) -> NodeStatus:
    """`check_at_startup` against the chain recorded in the user DB (T-206). A data directory with
    no recorded chain records the node's, but only once every check has passed: a node that fails a
    check never decides it."""
    expected = chain_state.recorded_chain(db)
    status = check_at_startup(
        client, expected_chain=expected, volume=volume, allow_unencrypted=allow_unencrypted, **gather_options
    )
    if expected is None and status.online and status.chain is not None:
        chain_state.record_chain(db, status.chain)
        log.info("recorded the data directory's chain: %s", status.chain)
    if status.online:
        reason = chain_sync.at_startup(client, db)
        if reason is not None:
            log.warning("offline mode: %s", reason)
            return NodeStatus(online=False, chain=status.chain, reasons=(reason,))
    return status

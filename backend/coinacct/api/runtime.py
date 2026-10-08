"""What the launcher starts the app with (architecture §2, §3, §4, §8.1).

The launcher may import only `config`, `storage` and `api`, so this is its one entry point into the
rest of the app: `build` runs the start-up node checks through `services/`, then creates the session
store, the shutdown coordinator and the ASGI app. The launcher keeps everything that touches the
network or the filesystem: the listening socket, uvicorn, the bootstrap file and the watchdog.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from coinacct.api.app import create_app
from coinacct.api.security import ASGIApp
from coinacct.api.session import BOOTSTRAP_TTL_SECONDS, Sessions
from coinacct.services import discovery, imports, jobs, startup
from coinacct.services.lifecycle import Shutdown
from coinacct.services.startup import NodeStatus, StorageRefused

__all__ = ["Runtime", "StorageRefused", "build", "new_shutdown"]

QUIT_REASON = "quit from the UI"


def new_shutdown() -> Shutdown:
    """The coordinator, for a launcher that needs it before the node checks run (§8.1): it passes
    the same one to `build`."""
    return Shutdown()


@dataclass(frozen=True)
class Runtime:
    app: ASGIApp
    sessions: Sessions
    shutdown: Shutdown
    status: NodeStatus
    # Starts the job worker and tip poller (§3), online only. The launcher calls it after its last
    # start-up checks, and stops what it returns at shutdown.
    start_chain_jobs: Callable[[], Any] | None = None


def build(  # noqa: PLR0913 - each value comes from a different part of start-up
    *,
    port: int,
    bootstrap_token: str,
    rpc: Any,
    volume: Any,
    db: Any,
    allow_unencrypted: bool,
    on_claimed: Callable[[], None],
    check: Callable[..., NodeStatus] = startup.check_configured_node,
    start_jobs: Callable[..., Any] = jobs.start_chain_jobs,
    ttl: float = BOOTSTRAP_TTL_SECONDS,
    shutdown: Shutdown | None = None,
    bundle: Mapping[str, bytes] | None = None,
) -> Runtime:
    """`rpc` is `config.RpcConfig`, `volume` is `storage.volume.VolumeStatus` and `db` the open user
    DB, passed as values (architecture §2: `api/` imports none of them). Raises `StorageRefused` when
    the storage policy forbids running (T-401); a node problem, including a chain other than the
    one recorded in the DB (T-206), only means offline mode.
    """
    status = check(
        rpc.host,
        rpc.port,
        rpc.user,
        rpc.password,
        db=db,
        volume=volume,
        allow_unencrypted=allow_unencrypted,
    )
    if shutdown is None:
        shutdown = Shutdown()
    # Created after the node checks, which can take seconds, so the token's 60 s start just before
    # the bootstrap file is written and the browser opened.
    sessions = Sessions(bootstrap_token, ttl=ttl, on_claimed=on_claimed)
    app = create_app(
        port=port,
        sessions=sessions,
        status=lambda: status,
        on_quit=lambda: shutdown.request(QUIT_REASON),
        bundle=bundle,
    )
    starter = None
    if status.online:
        # The chain jobs scan what has been imported (services.imports.subjects), read afresh each sync.
        starter = functools.partial(
            start_jobs,
            rpc.host,
            rpc.port,
            rpc.user,
            rpc.password,
            db=db,
            subjects=functools.partial(imports.subjects, db),
            discover=discovery.extend_windows,
        )
    return Runtime(app=app, sessions=sessions, shutdown=shutdown, status=status, start_chain_jobs=starter)

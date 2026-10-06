"""What the launcher starts the app with (architecture §2, §3, §4, §8.1).

The launcher may import only `config`, `storage` and `api`, so this is its one entry point into the
rest of the app: `build` runs the start-up node checks through `services/`, then creates the session
store, the shutdown coordinator and the ASGI app. The launcher keeps everything that touches the
network or the filesystem: the listening socket, uvicorn, the bootstrap file and the watchdog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from coinacct.api.app import create_app
from coinacct.api.security import ASGIApp
from coinacct.api.session import BOOTSTRAP_TTL_SECONDS, Sessions
from coinacct.services import startup
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


def build(  # noqa: PLR0913 - each value comes from a different part of start-up
    *,
    port: int,
    bootstrap_token: str,
    rpc: Any,
    volume: Any,
    allow_unencrypted: bool,
    on_claimed: Callable[[], None],
    check: Callable[..., NodeStatus] = startup.check_configured_node,
    ttl: float = BOOTSTRAP_TTL_SECONDS,
    shutdown: Shutdown | None = None,
) -> Runtime:
    """`rpc` is `config.RpcConfig` and `volume` is `storage.volume.VolumeStatus`, passed as values
    (architecture §2: `api/` imports neither). Raises `StorageRefused` when the storage policy
    forbids running (T-401); a node problem only means offline mode.

    There is no user DB yet (M2), so no chain is recorded for the data directory: the node's chain
    decides the storage policy, and a mismatch check (T-206) comes with the DB.
    """
    status = check(
        rpc.host,
        rpc.port,
        rpc.user,
        rpc.password,
        expected_chain=None,
        volume=volume,
        allow_unencrypted=allow_unencrypted,
    )
    if shutdown is None:
        shutdown = Shutdown()
    # Created after the node checks, which can take seconds, so the token's 60 s start just before
    # the bootstrap file is written and the browser opened.
    sessions = Sessions(bootstrap_token, ttl=ttl, on_claimed=on_claimed)
    app = create_app(
        port=port, sessions=sessions, status=lambda: status, on_quit=lambda: shutdown.request(QUIT_REASON)
    )
    return Runtime(app=app, sessions=sessions, shutdown=shutdown, status=status)

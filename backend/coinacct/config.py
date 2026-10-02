"""Parse `config.toml` text into values (architecture §2: pure, no file access).

The file holds **only** the RPC endpoint and the app's `rpcauth` credentials (architecture §6):

    [rpc]
    host = "127.0.0.1"
    port = 8332
    user = "ro-client"
    password = "..."

Everything is checked here and anything unexpected is an error, so a typo can't silently fall
back to a default (fail closed, ENGINEERING §5.3). Error messages never contain the password.
"""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from typing import Any

from coinacct.domain.secret import Secret

RPC_KEYS = frozenset({"host", "port", "user", "password"})


class ConfigError(ValueError):
    """The configuration is invalid. The message is safe to show and log (no secret values)."""


@dataclass(frozen=True, slots=True)
class RpcConfig:
    host: str
    port: int
    user: str
    password: Secret


@dataclass(frozen=True, slots=True)
class Config:
    rpc: RpcConfig


def loopback_literal(host: str) -> str:
    """Return `host` if it is a literal loopback IP address, else raise ConfigError.

    Names, including `localhost`, are refused: a name is resolved through the system resolver
    and the hosts file, which could point elsewhere. There is no override (THREAT_MODEL T-202);
    a remote node is reached through an SSH tunnel that ends on loopback.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            "rpc.host must be a literal loopback address such as 127.0.0.1 or ::1, not a name. "
            "For a node on another machine, use an SSH tunnel that ends on loopback (T-202)"
        ) from None
    if not address.is_loopback:
        raise ConfigError(
            f"rpc.host {host} is not a loopback address. The RPC connection has no encryption, so only "
            "loopback is allowed; use an SSH tunnel for a node elsewhere (T-202)"
        )
    return host


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # The decoder's message quotes no values, only the position.
        raise ConfigError(f"config.toml is not valid TOML: {e}") from None
    unknown = set(data) - {"rpc"}
    if unknown:
        raise ConfigError(f"config.toml has unknown sections or keys: {', '.join(sorted(unknown))}")
    rpc = data.get("rpc")
    if not isinstance(rpc, dict):
        raise ConfigError("config.toml needs an [rpc] section")
    return Config(rpc=_parse_rpc(rpc))


def _parse_rpc(rpc: dict[str, Any]) -> RpcConfig:
    unknown = set(rpc) - RPC_KEYS
    if unknown:
        raise ConfigError(f"[rpc] has unknown keys: {', '.join(sorted(unknown))}")
    missing = RPC_KEYS - set(rpc)
    if missing:
        raise ConfigError(f"[rpc] is missing: {', '.join(sorted(missing))}")
    host, port, user, password = rpc["host"], rpc["port"], rpc["user"], rpc["password"]
    if not isinstance(host, str):
        raise ConfigError("rpc.host must be a string")
    # bool is an int subclass; `port = true` must not become port 1.
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("rpc.port must be a whole number from 1 to 65535")
    if not isinstance(user, str) or not user or ":" in user or not user.isprintable():
        raise ConfigError("rpc.user must be a non-empty string without ':' or control characters")
    if not isinstance(password, str) or not password or not password.isprintable():
        raise ConfigError("rpc.password must be a non-empty string without control characters")
    return RpcConfig(host=loopback_literal(host), port=port, user=user, password=Secret(password))

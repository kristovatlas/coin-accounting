"""The only JSON-RPC client for Bitcoin Core (architecture §1, flow F2; ADR 0004).

- **Loopback only**, with no override (T-202): the endpoint must be a literal loopback IP.
- **Client-side method allowlist** (T-203), mirroring the node's `rpcwhitelist`. Changing it is a
  security change and needs an ADR (ADR 0004). The one call outside it is the startup canary,
  which has its own method and expects the node to refuse it.
- **Counter request ids** (T-209): `debug=rpc` logs the id, so it carries nothing but a number.
- **Concurrency cap** below Core's `rpcthreads`/`rpcworkqueue` defaults (16/64).
- **No retries:** a failed call is reported, never repeated blindly.
- **Response size cap** and exact decoding: JSON numbers with a fraction (BTC amounts) become
  `Decimal`, never `float` (T-205, T-502).
- Credentials appear only in the Authorization header. No error message includes them, or the
  call's parameters, which can hold descriptors or txids (T-201, T-403).
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import itertools
import json
import threading
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

from coinacct.domain.secret import Secret

# THREAT_MODEL T-203: the read-only methods the app's rpcauth user may call.
ALLOWED_METHODS: Final = frozenset(
    {
        "getblockchaininfo",
        "getnetworkinfo",
        "getindexinfo",
        "getblockcount",
        "getbestblockhash",
        "getblockhash",
        "getblockheader",
        "getblock",
        "getrawtransaction",
        "gettxout",
        "gettxspendingprevout",
        "scanblocks",
        "getdescriptoractivity",
        "getchaintips",
        "deriveaddresses",
        "getdescriptorinfo",
    }
)
# ADR 0004: a harmless method that must *not* be whitelisted. If the node answers it, the
# server-side whitelist is missing.
CANARY_METHOD: Final = "uptime"

DEFAULT_TIMEOUT: Final = 120.0
DEFAULT_MAX_CONCURRENCY: Final = 4
DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024 * 1024


class RpcError(Exception):
    """Base class. Messages name the method, never its parameters or the credentials."""


class RpcTransportError(RpcError):
    """The node couldn't be reached, or answered with something that isn't a JSON-RPC reply."""


class RpcAuthError(RpcError):
    """HTTP 401: the rpcauth user or password is wrong."""


class RpcForbiddenError(RpcError):
    """HTTP 403: the node's rpcwhitelist refused the method."""


class RpcResponseTooLargeError(RpcError):
    """The reply exceeded the size cap (T-205)."""


class RpcMethodNotAllowedError(RpcError):
    """The method isn't on the client-side allowlist (T-203); nothing was sent."""


class RpcCallError(RpcError):
    """The node returned a JSON-RPC error object.

    `str()` and `repr()` carry only the method and the error code. Core often quotes the offending
    parameter in its message (e.g. "key '<xpub>' is not valid", "... for '<txid>'"), so the node's
    text is kept apart in `node_message` for code that must branch on it, and must never be
    logged, shown or put in another exception (T-201, T-403).
    """

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method}: node error {code}")
        self.method = method
        self.code = code
        self.node_message = message


def require_loopback(host: str) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("the RPC endpoint must be a literal loopback address (T-202)")


class RpcClient:
    def __init__(  # noqa: PLR0913 - endpoint, credentials and three keyword-only limits
        self,
        host: str,
        port: int,
        user: str,
        password: Secret,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        require_loopback(host)
        if not 1 <= port <= 65535:
            raise ValueError("the RPC port must be from 1 to 65535")
        if max_concurrency < 1 or max_response_bytes < 1 or timeout <= 0:
            raise ValueError("limits must be positive")
        self._host, self._port, self._timeout = host, port, timeout
        self._max_response_bytes = max_response_bytes
        token = base64.b64encode(f"{user}:{password.reveal()}".encode()).decode("ascii")
        self._auth = Secret(f"Basic {token}")
        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def __repr__(self) -> str:
        return f"RpcClient({self._host}:{self._port})"

    def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        """Call an allowlisted method and return its `result`."""
        if method not in ALLOWED_METHODS:
            raise RpcMethodNotAllowedError(f"{method} is not on the client allowlist (T-203)")
        status, body = self._post(method, list(params))
        return self._decode(method, status, body)

    def canary_refused(self) -> bool:
        """Call the canary method. True if the node refused it (HTTP 403), as it must.

        Any other answer (a result, or a JSON-RPC error such as "method not found") means the
        whitelist isn't active for this user. Transport and auth failures raise, so the caller
        can't mistake an unreachable node for a refused canary.
        """
        status, body = self._post(CANARY_METHOD, [])
        if status == http.client.FORBIDDEN:
            return True
        try:
            self._decode(CANARY_METHOD, status, body)  # raises on 401 and malformed replies
        except RpcCallError:
            pass  # a JSON-RPC error still means the node processed the call: no whitelist
        return False

    def _next_id(self) -> int:
        with self._id_lock:
            return next(self._ids)

    def _post(self, method: str, params: list[Any]) -> tuple[int, bytes]:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params},
            default=_refuse_non_json,
        ).encode()
        headers = {
            "Authorization": self._auth.reveal(),
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        }
        with self._slots:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
            try:
                conn.request("POST", "/", body=payload, headers=headers)
                response = conn.getresponse()
                body = response.read(self._max_response_bytes + 1)
                status = response.status
            except (OSError, http.client.HTTPException) as e:
                raise RpcTransportError(f"{method}: can't reach the node ({type(e).__name__})") from None
            finally:
                conn.close()
        if len(body) > self._max_response_bytes:
            raise RpcResponseTooLargeError(f"{method}: reply larger than {self._max_response_bytes} bytes")
        return status, body

    def _decode(self, method: str, status: int, body: bytes) -> Any:
        if status == http.client.UNAUTHORIZED:
            raise RpcAuthError("the node refused the RPC credentials (HTTP 401)")
        if status == http.client.FORBIDDEN:
            raise RpcForbiddenError(f"{method}: refused by the node's rpcwhitelist (HTTP 403)")
        try:
            reply = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise RpcTransportError(f"{method}: HTTP {status} with a reply that isn't JSON") from None
        if not isinstance(reply, dict) or ("result" not in reply and "error" not in reply):
            raise RpcTransportError(f"{method}: HTTP {status} with a reply that isn't JSON-RPC")
        error = reply.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise RpcTransportError(f"{method}: malformed error object")
            code, message = error.get("code"), error.get("message")
            raise RpcCallError(
                method, code if isinstance(code, int) else 0, message if isinstance(message, str) else ""
            )
        if status != http.client.OK:
            raise RpcTransportError(f"{method}: HTTP {status}")
        return reply["result"]


def _refuse_non_json(value: object) -> object:
    # Parameters are strings, integers, booleans, lists and objects. No allowlisted method takes
    # an amount, so there is no Decimal encoding (and no float path, T-502).
    raise TypeError(f"can't send a {type(value).__name__} as an RPC parameter")


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")

"""The HTTP security layer, outermost around the app (architecture §4; THREAT_MODEL T-101 to T-107).

Pure ASGI, so it runs before routing and for every response, errors included:

- **Host allowlist (T-101):** exactly `127.0.0.1:<port>`. Anything else, `localhost` included, is
  refused with 421 before routing, which stops DNS rebinding.
- **JSON only (T-102):** a request with a body under `/api/` must be `application/json`. A plain
  HTML form can't send that, so it can't reach a state-changing route. The bearer token is the main
  CSRF control; this is a second one. There are never any CORS headers.
- **Response headers:** the strict CSP (T-104, T-106), `frame-ancestors 'none'` and
  `X-Frame-Options: DENY` (T-107), `Referrer-Policy: no-referrer` (T-110), `Cache-Control: no-store`
  (T-105), `nosniff`, and same-origin opener and resource policies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS: Final = (
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)
_REPLACED: Final = frozenset(name for name, _ in SECURITY_HEADERS)
METHODS_WITH_BODY: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_host(port: int) -> bytes:
    return f"127.0.0.1:{port}".encode()


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.host = allowed_host(port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)  # lifespan
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _REPLACED]
                message["headers"] = headers + list(SECURITY_HEADERS)
            await send(message)

        headers = scope.get("headers", [])
        hosts = [v for k, v in headers if k.lower() == b"host"]
        if hosts != [self.host]:
            await _plain(send_with_headers, 421, "Misdirected request: wrong Host (T-101)")
            return
        if scope["method"] in METHODS_WITH_BODY and scope["path"].startswith("/api/"):
            types = [v for k, v in headers if k.lower() == b"content-type"]
            if len(types) != 1 or types[0].split(b";")[0].strip().lower() != b"application/json":
                await _plain(send_with_headers, 415, "Only application/json is accepted")
                return
        await self.app(scope, receive, send_with_headers)


async def _plain(send: Send, status: int, text: str) -> None:
    body = text.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

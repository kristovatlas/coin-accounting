"""A minimal in-process ASGI client for the API tests. No sockets, no httpx: it calls the app with
a scope and collects what it sends."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from coinacct.api.security import ASGIApp, Message

PORT = 54321
HOST = f"127.0.0.1:{PORT}"


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def call(  # noqa: PLR0913 - mirrors the parts of an HTTP request
    app: ASGIApp,
    method: str,
    path: str,
    *,
    host: str | None = HOST,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    json_body: Any = None,
    raw_headers: list[tuple[bytes, bytes]] | None = None,
    chunks: list[bytes] | None = None,
) -> Reply:
    hdrs = [] if host is None else [(b"host", host.encode())]
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs.append((b"content-type", b"application/json"))
    hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    hdrs += raw_headers or []
    path, _, query = path.partition("?")  # as a server splits the request target
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": hdrs,
        "client": ("127.0.0.1", 40000),
        "server": ("127.0.0.1", PORT),
    }
    sent: list[Message] = []
    pending = list(chunks) if chunks is not None else [body]

    async def receive() -> Message:
        if pending:
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    async def run() -> None:
        await app(scope, receive, send)

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    names = [k.decode().lower() for k, _ in start.get("headers", [])]
    assert len(names) == len(set(names)), f"duplicate response headers: {names}"
    return Reply(start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, data)

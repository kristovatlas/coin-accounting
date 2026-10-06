"""The API and its security layer (architecture §4; THREAT_MODEL T-101 to T-107, T-110).

Requests go straight into the ASGI app (tests/unit/api/asgi.py), so these run without sockets.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest

from coinacct.api.app import create_app
from coinacct.api.security import CSP, ASGIApp
from coinacct.api.session import ALREADY_CLAIMED, Sessions
from coinacct.services.startup import NodeStatus

from .asgi import HOST, PORT, Reply, call

TOKEN = "launch-token-for-tests"
ONLINE = NodeStatus(online=True, chain="regtest", reasons=())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, status: NodeStatus = ONLINE) -> None:
        self.clock = Clock()
        self.claimed = 0
        self.quits = 0
        self.sessions = Sessions(TOKEN, clock=self.clock, on_claimed=self.on_claimed)
        self.status = status
        self.app: ASGIApp = create_app(
            port=PORT, sessions=self.sessions, status=lambda: self.status, on_quit=self.on_quit
        )

    def on_claimed(self) -> None:
        self.claimed += 1

    def on_quit(self) -> None:
        self.quits += 1

    def claim(self, token: str = TOKEN) -> Reply:
        return call(self.app, "POST", "/api/session", json_body={"bootstrap": token})

    def session(self) -> str:
        reply = self.claim()
        assert reply.status == 200
        return str(reply.json()["session"])


@pytest.fixture
def world() -> World:
    return World()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- T-101: Host allowlist ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost:54321",  # one canonical origin only, never localhost
        "127.0.0.1",  # no port
        "127.0.0.1:1",  # another port
        "evil.example:54321",  # DNS rebinding
        "127.0.0.1:54321.evil.example",
        "[::1]:54321",
        "127.0.0.1:54321 ",
    ],
)
def test_any_other_host_is_refused_before_routing_t101(world: World, host: str) -> None:
    reply = call(world.app, "GET", "/api/status", host=host, headers=bearer("x"))
    assert reply.status == 421
    reply = call(world.app, "POST", "/api/session", host=host, json_body={"bootstrap": TOKEN})
    assert reply.status == 421
    assert world.claimed == 0  # the session route never ran


def test_a_missing_or_doubled_host_is_refused_t101(world: World) -> None:
    assert call(world.app, "GET", "/", host=None).status == 421
    doubled = [(b"host", b"evil.example")]
    assert call(world.app, "GET", "/", raw_headers=doubled).status == 421


def test_the_canonical_host_is_served(world: World) -> None:
    reply = call(world.app, "GET", "/", host=HOST)
    assert reply.status == 200
    assert reply.headers["content-type"].startswith("text/html")


# --- Response headers (T-104 to T-107, T-110) ----------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "host"),
    [
        ("GET", "/", HOST),
        ("GET", "/app.js", HOST),
        ("GET", "/api/status", HOST),
        ("GET", "/nowhere", HOST),
        ("GET", "/", "evil.example"),
    ],
)
def test_every_response_has_the_security_headers(world: World, method: str, path: str, host: str) -> None:
    headers = call(world.app, method, path, host=host).headers
    assert headers["content-security-policy"] == CSP
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert not any(name.startswith("access-control-") for name in headers)  # never CORS (T-102)


def test_the_csp_is_the_strict_one_t104() -> None:
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in CSP
    assert "unsafe-inline" not in CSP
    assert "unsafe-eval" not in CSP


def test_there_are_no_docs_or_schema_pages_t106(world: World) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert call(world.app, "GET", path).status == 404


def test_a_cors_preflight_gets_no_cors_headers_t102(world: World) -> None:
    reply = call(
        world.app,
        "OPTIONS",
        "/api/status",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert reply.status == 405
    assert not any(name.startswith("access-control-") for name in reply.headers)


# --- T-102: JSON only -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_api_bodies_must_be_json_t102(world: World, content_type: str | None) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    reply = call(world.app, "POST", "/api/session", headers=headers, body=b'{"bootstrap": "x"}')
    assert reply.status == 415
    assert world.claimed == 0


def test_json_with_a_charset_is_accepted(world: World) -> None:
    reply = call(
        world.app,
        "POST",
        "/api/session",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=b'{"bootstrap": "launch-token-for-tests"}',
    )
    assert reply.status == 200


# --- §4 and T-110: the launch token exchange ----------------------------------------------------


def test_the_bootstrap_token_is_exchanged_once_for_a_session(world: World) -> None:
    reply = world.claim()
    assert reply.status == 200
    session = reply.json()["session"]
    assert len(session) >= 40 and session != TOKEN
    assert world.claimed == 1  # the launcher deletes the bootstrap file now


def test_a_second_claim_is_refused_and_logged_t110(world: World, caplog: pytest.LogCaptureFixture) -> None:
    world.session()
    with caplog.at_level(logging.WARNING, logger="coinacct.api.session"):
        reply = world.claim()
    assert reply.status == 409
    assert reply.json() == {"error": ALREADY_CLAIMED}
    assert "second claim" in caplog.text
    assert world.claimed == 1


def test_a_wrong_token_is_refused(world: World) -> None:
    reply = world.claim("not-the-token")
    assert reply.status == 401
    assert "session" not in reply.json()
    assert world.claim().status == 200  # the real token still works


def test_an_expired_token_is_refused_t110(world: World) -> None:
    world.clock.now += 60.0
    reply = world.claim()
    assert reply.status == 410
    assert "Restart" in reply.json()["error"]
    assert world.claimed == 0


def test_a_token_just_inside_the_ttl_works(world: World) -> None:
    world.clock.now += 59.9
    assert world.claim().status == 200


@pytest.mark.parametrize(
    "body",
    [{}, {"bootstrap": ""}, {"bootstrap": "x" * 201}, {"bootstrap": TOKEN, "extra": 1}, {"bootstrap": 5}],
)
def test_a_malformed_claim_is_refused_without_echoing_input(world: World, body: dict[str, object]) -> None:
    reply = call(world.app, "POST", "/api/session", json_body=body)
    assert reply.status == 422
    assert reply.json() == {"error": "invalid request"}
    assert world.claimed == 0


def test_a_failing_claim_callback_doesnt_lose_the_session(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> None:
        raise OSError("bootstrap file already gone")

    sessions = Sessions(TOKEN, on_claimed=broken)
    with caplog.at_level(logging.ERROR, logger="coinacct.api.session"):
        session = sessions.claim(TOKEN)
    assert isinstance(session, str)
    assert sessions.is_session(session)
    assert "bootstrap file" in caplog.text


def test_an_empty_bootstrap_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        Sessions("")


# --- Bearer authentication (T-102, T-103) -------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Bearer wrong", "Basic abc", "Token {s}", "{s}", "Bearer {s}x"],
)
def test_the_api_needs_the_session_as_a_bearer_token(world: World, authorization: str | None) -> None:
    session = world.session()
    headers = {} if authorization is None else {"Authorization": authorization.format(s=session)}
    for method, path in (("GET", "/api/status"), ("POST", "/api/quit")):
        hdrs = dict(headers, **({"Content-Type": "application/json"} if method == "POST" else {}))
        reply = call(world.app, method, path, headers=hdrs, body=b"{}" if method == "POST" else b"")
        assert reply.status == 401
        assert reply.headers["www-authenticate"] == "Bearer"
    assert world.quits == 0


def test_no_session_works_before_the_claim(world: World) -> None:
    assert call(world.app, "GET", "/api/status", headers=bearer(TOKEN)).status == 401


def test_the_scheme_is_case_insensitive(world: World) -> None:
    session = world.session()
    assert call(world.app, "GET", "/api/status", headers={"Authorization": f"bearer {session}"}).status == 200


# --- Routes ---------------------------------------------------------------------------------------


def test_status_reports_the_node(world: World) -> None:
    session = world.session()
    assert call(world.app, "GET", "/api/status", headers=bearer(session)).json() == {
        "online": True,
        "chain": "regtest",
        "reasons": [],
    }
    world.status = NodeStatus(online=False, chain=None, reasons=("the node can't be reached: refused",))
    assert call(world.app, "GET", "/api/status", headers=bearer(session)).json() == {
        "online": False,
        "chain": None,
        "reasons": ["the node can't be reached: refused"],
    }


def test_quit_starts_shutdown(world: World) -> None:
    session = world.session()
    reply = call(world.app, "POST", "/api/quit", headers=bearer(session), json_body={})
    assert reply.status == 202
    assert world.quits == 1


def test_the_page_and_script_follow_the_csp_t104(world: World) -> None:
    page = call(world.app, "GET", "/").body.decode()
    script = call(world.app, "GET", "/app.js")
    assert script.headers["content-type"].startswith("text/javascript")
    assert '<script src="/app.js" defer></script>' in page
    assert "<script>" not in page and "style=" not in page and "<style" not in page  # nothing inline
    js = script.body.decode()
    assert "innerHTML" not in js and "eval(" not in js
    assert "history.replaceState" in js  # T-110: the token leaves the address bar first
    assert "sessionStorage" in js and "localStorage" not in js and "cookie" not in js


def test_websockets_are_closed_without_reaching_the_app(world: World) -> None:
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "websocket", "path": "/api/status", "headers": [(b"host", HOST.encode())]}
    asyncio.run(world.app(scope, receive, send))  # type: ignore[arg-type]
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_lifespan_events_pass_through(world: World) -> None:
    sent: list[dict[str, object]] = []
    messages: Iterator[dict[str, object]] = iter(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )

    async def receive() -> dict[str, object]:
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    asyncio.run(world.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))  # type: ignore[arg-type]
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]

"""The price download layer (F3; ADR 0007; THREAT_MODEL T-301, T-302, T-303, T-305).

No test reaches the internet (the socket guard would fail it). A fake server on loopback stands in for
the price host, and for the SOCKS5 proxy in front of it. TLS itself is the standard library's; the tests
check the context it is given and where the TLS layer is applied.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from coinacct.prices import fetch
from coinacct.prices.fetch import USER_AGENT, FetchError, Proxy, open_url, socks5_connect

URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000"


class FakeServer:
    """One connection on loopback: an optional SOCKS5 handshake, then one HTTP exchange."""

    def __init__(self, response: bytes, *, socks: bytes | None = None, http: bool = True) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.response, self.socks_reply, self.http = response, socks, http
        self.socks_request = b""
        self.request = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn:
            conn.settimeout(5)
            if self.socks_reply is not None:
                self.socks_request = conn.recv(3)
                conn.sendall(b"\x05\x00")
                head = conn.recv(5)
                rest = conn.recv(head[4] + 2)
                self.socks_request += head + rest
                conn.sendall(self.socks_reply)
                if not self.http:
                    return
            while b"\r\n\r\n" not in self.request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                self.request += chunk
            conn.sendall(self.response)

    def close(self) -> None:
        self.thread.join(5)
        self.listener.close()


def ok(body: bytes, length: bool = True) -> bytes:
    head = b"HTTP/1.1 200 OK\r\n" + (b"Content-Length: %d\r\n" % len(body) if length else b"")
    return head + b"Connection: close\r\n\r\n" + body


@pytest.fixture
def direct(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeServer]]:
    """Point the direct HTTPS connection at a fake loopback server, without TLS."""
    servers: list[FakeServer] = []

    class Plain(http.client.HTTPConnection):
        def __init__(self, host: str, port: int, *, timeout: float, context: ssl.SSLContext) -> None:
            assert (port, context.verify_mode, context.check_hostname) == (443, ssl.CERT_REQUIRED, True)
            super().__init__("127.0.0.1", servers[-1].port, timeout=timeout)
            self.real_host = host

        def putheader(self, header: str | bytes, *values: Any) -> None:
            # http.client sends Host from the connection; the real class would send the source's name
            if header == "Host":
                values = (self.real_host,)
            super().putheader(header, *values)

    monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
    yield servers
    for s in servers:
        s.close()


def test_a_download_sends_one_fixed_request_and_streams_the_body(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"a" * 100_000)))
    with open_url(URL, max_bytes=100_000) as body:
        data = b"".join(iter(lambda: body.read(30_000), b""))
    assert data == b"a" * 100_000
    head = direct[0].request.decode()
    lines = head.split("\r\n")
    assert lines[0] == "GET /api/v2/ohlc/btcusd/?step=86400&limit=1000 HTTP/1.1"
    headers = sorted(line.split(": ", 1)[0] for line in lines[1:] if line)
    assert headers == ["Accept", "Accept-Encoding", "Host", "User-Agent"]  # nothing else about the app
    assert f"User-Agent: {USER_AGENT}" in lines and "Host: www.bitstamp.net" in lines


def test_a_body_past_the_limit_is_refused_as_it_streams(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"a" * 1001, length=False)))
    with pytest.raises(FetchError, match=r"www\.bitstamp\.net: the response is larger than allowed"):
        with open_url(URL, max_bytes=1000) as body:
            while body.read():
                pass


def test_a_declared_length_past_the_limit_is_refused_before_reading(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"a" * 1001)))
    with pytest.raises(FetchError, match="larger than allowed"):
        with open_url(URL, max_bytes=1000):
            pass


@pytest.mark.parametrize(
    "status",
    [b"301 Moved Permanently\r\nLocation: https://elsewhere.example/", b"404 Not Found", b"500 Oops"],
)
def test_a_redirect_or_error_is_refused_not_followed(direct: list[FakeServer], status: bytes) -> None:
    direct.append(FakeServer(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n"))
    with pytest.raises(FetchError, match=r"www\.bitstamp\.net: HTTP (301|404|500) "):
        with open_url(URL, max_bytes=1000):
            pass


def test_a_broken_response_is_a_fetch_error(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(b"garbage\r\n\r\n"))
    with pytest.raises(FetchError, match="the download failed"):
        with open_url(URL, max_bytes=1000):
            pass


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bitstamp.net/",  # not TLS
        "https://example.com/",  # not a price source
        "https://www.bitstamp.net.example.com/",
        "https://www.bitstamp.net:8443/",  # another port
        "https://www.bitstamp.net:443/",  # even the default port, written out
        "https://user@www.bitstamp.net/",
        "https://www.bitstamp.net/#x",
        "https://WWW.BITSTAMP.NET/",  # only the exact names
        "https://www.bitstamp.net:99999/",
        "https://www.bitstamp.net/a b",
        "https://www.bitstamp.net/café",
        "ftp://www.bitstamp.net/",
    ],
)
def test_only_https_to_a_price_source_is_fetched(url: str) -> None:
    with pytest.raises(FetchError):
        with open_url(url, max_bytes=1):
            pass


def test_the_tls_context_verifies_certificates_and_names() -> None:
    ctx = fetch._context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


# --- SOCKS5 with remote DNS


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.8.9.10"])
def test_a_loopback_proxy_is_accepted(host: str) -> None:
    assert Proxy(host, 9050).host == host


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("10.0.0.1", 9050),
        ("localhost", 9050),
        ("tor.example", 9050),
        ("127.0.0.1", 0),
        ("127.0.0.1", 65536),
        ("127.0.0.1", True),
    ],
)
def test_only_a_loopback_proxy_with_a_port_is_accepted(host: str, port: int) -> None:
    with pytest.raises(FetchError):
        Proxy(host, port)


def socks_ok(kind: int = 1) -> bytes:
    bound = {1: b"\x7f\x00\x00\x01", 4: b"\x00" * 15 + b"\x01", 3: b"\x09localhost"}[kind]
    return b"\x05\x00\x00" + bytes([kind]) + bound + b"\x00\x50"


@pytest.mark.parametrize("kind", [1, 3, 4])
def test_the_proxy_gets_the_host_name_not_an_address(kind: int) -> None:
    server = FakeServer(b"", socks=socks_ok(kind), http=False)
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        socks5_connect(sock, "www.ecb.europa.eu", 443)
    server.close()
    name = b"www.ecb.europa.eu"
    # no authentication; CONNECT by domain name (type 3), so the proxy resolves it (remote DNS)
    assert (
        server.socks_request
        == b"\x05\x01\x00" + b"\x05\x01\x00\x03" + bytes([len(name)]) + name + b"\x01\xbb"
    )


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (b"\x05\x05\x00\x01" + b"\x00" * 6, "couldn't connect: connection refused"),
        (b"\x05\x09\x00\x01" + b"\x00" * 6, "couldn't connect: unknown error"),
        (b"\x04\x00\x00\x01" + b"\x00" * 6, "answered oddly"),
        (b"\x05\x00\x00\x07" + b"\x00" * 6, "answered oddly"),
        (b"\x05\x00", "closed the connection"),
    ],
)
def test_a_refusing_or_odd_proxy_is_a_fetch_error(reply: bytes, message: str) -> None:
    server = FakeServer(b"", socks=reply, http=False)
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        with pytest.raises(FetchError, match=message):
            socks5_connect(sock, "www.ecb.europa.eu", 443)
    server.close()


def test_a_proxy_that_wants_a_password_is_refused() -> None:
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b"\x05\xff")
        with pytest.raises(FetchError, match="refused"):
            socks5_connect(a, "www.ecb.europa.eu", 443)


def test_a_host_name_too_long_for_socks5_is_refused() -> None:
    a, b = socket.socketpair()
    with a, b, pytest.raises(FetchError, match="too long"):
        socks5_connect(a, "x" * 256, 443)


def test_a_download_through_the_proxy_wraps_tls_around_the_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(ok(b"rates"), socks=socks_ok())
    wrapped: list[str] = []

    class NoTLS:
        def wrap_socket(self, sock: socket.socket, server_hostname: str) -> socket.socket:
            wrapped.append(server_hostname)  # TLS starts after the tunnel, for the source's own name
            return sock

    real = fetch._SocksHTTPSConnection.__init__

    def init(self: Any, host: str, proxy: Proxy, timeout: float) -> None:
        real(self, host, proxy, timeout)
        self._tls = NoTLS()

    monkeypatch.setattr(fetch._SocksHTTPSConnection, "__init__", init)
    with open_url(
        "https://www.ecb.europa.eu/stats/x.zip", Proxy("127.0.0.1", server.port), max_bytes=10
    ) as body:
        assert body.read() == b"rates"
    server.close()
    assert wrapped == ["www.ecb.europa.eu"]
    assert b"\x03\x11www.ecb.europa.eu\x01\xbb" in server.socks_request
    assert server.request.startswith(b"GET /stats/x.zip HTTP/1.1\r\n")


def test_a_proxy_that_is_not_listening_is_a_fetch_error() -> None:
    port = socket.create_server(("127.0.0.1", 0))
    free = port.getsockname()[1]
    port.close()
    with pytest.raises(FetchError, match=r"www\.ecb\.europa\.eu: the download failed"):
        with open_url("https://www.ecb.europa.eu/", Proxy("127.0.0.1", free), max_bytes=1):
            pass


def test_a_proxy_that_refuses_the_connection_fails_the_download_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(b"", socks=b"\x05\x04\x00\x01" + b"\x00" * 6, http=False)
    with pytest.raises(FetchError, match="couldn't connect: host unreachable"):
        with open_url("https://www.ecb.europa.eu/", Proxy("127.0.0.1", server.port), max_bytes=1):
            pass
    server.close()


def test_a_silent_proxy_times_out_as_a_fetch_error() -> None:
    a, b = socket.socketpair()
    with a, b:
        a.settimeout(0.05)
        with pytest.raises(FetchError, match=r"the SOCKS5 proxy failed \(TimeoutError\)"):
            socks5_connect(a, "www.ecb.europa.eu", 443)


def test_the_body_is_a_readable_stream(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"x")))
    with open_url(URL, max_bytes=1) as body:
        assert body.readable() and body.read() == b"x" and body.read() == b""

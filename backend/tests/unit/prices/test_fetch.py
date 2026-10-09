"""The price download layer (F3; ADR 0007; THREAT_MODEL T-301, T-302, T-303, T-305).

No test reaches the internet (the socket guard would fail it). A fake server on loopback stands in for
the price host, and for the SOCKS5 proxy in front of it. TLS itself is the standard library's; the tests
check the context it is given and where the TLS layer is applied.
"""

from __future__ import annotations

import gzip
import io
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from coinacct.prices import fetch
from coinacct.prices.fetch import HEADERS, USER_AGENT, FetchError, Proxy, open_url, socks5_connect

URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000"


def recv_exactly(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed early")
        data += chunk
    return data


class FakeServer:
    """One connection on loopback: an optional SOCKS5 handshake (choosing no authentication, or
    username/password when `auth` is set), then one HTTP exchange."""

    def __init__(  # noqa: PLR0913 (a test double's knobs, all keyword-only)
        self,
        response: bytes,
        *,
        socks: bytes | None = None,
        http: bool = True,
        auth: bytes | None = None,
        then_close: bool = False,
        drip: float = 0.0,
        hold: bool = False,
        socks_drip: float = 0.0,
    ) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.response, self.socks_reply, self.http, self.auth = response, socks, http, auth
        self.then_close, self.drip, self.hold, self.socks_drip = then_close, drip, hold, socks_drip
        self.greeting = b""
        self.credentials = b""
        self.socks_request = b""
        self.request = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn:
            conn.settimeout(5)
            try:
                if self.socks_reply is not None and not self._socks(conn):
                    return
                while b"\r\n\r\n" not in self.request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    self.request += chunk
                if self.drip:
                    for i in range(len(self.response)):
                        conn.sendall(self.response[i : i + 1])
                        time.sleep(self.drip)
                else:
                    conn.sendall(self.response)
                if not self.then_close:
                    recv_exactly(conn, 1)  # wait for the client to hang up
            except (ConnectionError, TimeoutError):
                return

    def _socks(self, conn: socket.socket) -> bool:
        """The proxy's side of the handshake; whether HTTP follows."""
        assert self.socks_reply is not None
        head = recv_exactly(conn, 2)
        self.greeting = head + recv_exactly(conn, head[1])
        if self.auth is None:
            conn.sendall(b"\x05\x00")
        else:
            conn.sendall(b"\x05\x02")
            user = recv_exactly(conn, 2)
            user += recv_exactly(conn, user[1])
            password = recv_exactly(conn, 1)
            self.credentials = user + password + recv_exactly(conn, password[0])
            conn.sendall(self.auth)
            if self.auth != b"\x01\x00":
                return False
        head = recv_exactly(conn, 5)
        self.socks_request = head + recv_exactly(conn, head[4] + 2)
        if self.socks_drip:
            for i in range(len(self.socks_reply)):
                conn.sendall(self.socks_reply[i : i + 1])
                time.sleep(self.socks_drip)
        else:
            conn.sendall(self.socks_reply)
        if not self.http:
            while self.hold and conn.recv(4096):  # silent: read whatever comes, answer nothing
                pass
            return False
        return True

    def close(self) -> None:
        self.thread.join(5)
        self.listener.close()


def ok(body: bytes, length: bool = True) -> bytes:
    head = b"HTTP/1.1 200 OK\r\n" + (b"Content-Length: %d\r\n" % len(body) if length else b"")
    return head + b"Connection: close\r\n\r\n" + body


@pytest.fixture
def direct(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeServer]]:
    """Point the connection at a fake loopback server, without the encryption: the same deadline-bound
    socket class (`_DeadlineIO`) that TLS uses, over plain TCP. Everything else is the real path."""
    servers: list[FakeServer] = []
    contexts: list[ssl.SSLContext] = []

    def tcp(self: Any) -> socket.socket:
        contexts.append(self._tls)
        return socket.create_connection(
            ("127.0.0.1", servers[-1].port), timeout=self._deadline.left(self.host)
        )

    def wrap(self: Any, sock: socket.socket) -> socket.socket:
        plain = fetch._DeadlineIO(fileno=sock.detach())
        plain.deadline, plain.host = self._deadline, self.host
        return plain

    monkeypatch.setattr(fetch._Connection, "_tcp", tcp)
    monkeypatch.setattr(fetch._Connection, "_wrap", wrap)
    yield servers
    for s in servers:
        s.close()
    for ctx in contexts:  # the context the real _wrap would use
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
        assert ctx.sslsocket_class is fetch._DeadlineSocket


def test_a_download_sends_one_fixed_request_and_streams_the_body(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"a" * 100_000)))
    with open_url(URL, max_bytes=100_000) as body:
        data = b"".join(iter(lambda: body.read(30_000), b""))
    assert data == b"a" * 100_000
    head = direct[0].request.decode()
    lines = head.split("\r\n")
    assert lines[0] == "GET /api/v2/ohlc/btcusd/?step=86400&limit=1000 HTTP/1.1"
    headers = sorted(line.split(": ", 1)[0] for line in lines[1:] if line)
    # PLAN §6: a browser User-Agent, a generic Accept, and what http.client adds; nothing about the app
    assert headers == ["Accept", "Accept-Encoding", "Host", "User-Agent"]
    assert f"User-Agent: {USER_AGENT}" in lines and "Host: www.bitstamp.net" in lines
    assert all(f"{k}: {v}" in lines for k, v in HEADERS.items())


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
    assert server.greeting == b"\x05\x02\x02\x00"  # username/password (for isolation) or none
    # CONNECT by domain name (type 3), so the proxy resolves it (remote DNS)
    assert server.socks_request == b"\x05\x01\x00\x03" + bytes([len(name)]) + name + b"\x01\xbb"


def test_each_connection_offers_fresh_random_credentials_for_tor_isolation() -> None:
    seen = []
    for _ in range(2):
        server = FakeServer(b"", socks=socks_ok(), http=False, auth=b"\x01\x00")
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            socks5_connect(sock, "www.ecb.europa.eu", 443)
        server.close()
        version, ulen = server.credentials[0], server.credentials[1]
        user = server.credentials[2 : 2 + ulen]
        password = server.credentials[3 + ulen :]
        assert version == 1 and len(user) == len(password) == 16  # RFC 1929, 8 random bytes in hex each
        seen.append((user, password))
        assert server.socks_request.startswith(b"\x05\x01\x00\x03")
    assert seen[0] != seen[1] and seen[0][0] != seen[0][1]


def test_a_proxy_that_refuses_the_isolation_credentials_is_a_fetch_error() -> None:
    server = FakeServer(b"", socks=socks_ok(), http=False, auth=b"\x01\x01")
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        with pytest.raises(FetchError, match="refused the isolation credentials"):
            socks5_connect(sock, "www.ecb.europa.eu", 443)
    server.close()


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


@pytest.mark.parametrize("choice", [b"\x05\xff", b"\x05\x01", b"\x04\x00"])
def test_a_proxy_that_takes_neither_offered_method_is_refused(choice: bytes) -> None:
    a, b = socket.socketpair()
    with a, b:
        b.sendall(choice)
        with pytest.raises(FetchError, match="wants another method"):
            socks5_connect(a, "www.ecb.europa.eu", 443)


def test_a_host_name_too_long_for_socks5_is_refused() -> None:
    a, b = socket.socketpair()
    with a, b, pytest.raises(FetchError, match="too long"):
        socks5_connect(a, "x" * 256, 443)


def test_a_download_through_the_proxy_wraps_tls_around_the_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(ok(b"rates"), socks=socks_ok())
    wrapped: list[str] = []

    def wrap(self: Any, sock: socket.socket) -> socket.socket:
        wrapped.append(self.host)  # TLS starts after the tunnel, for the source's own name
        assert self._tls.verify_mode == ssl.CERT_REQUIRED and self._tls.check_hostname
        assert self._context is self._tls  # one context: the one the tunnel is wrapped in
        plain = fetch._DeadlineIO(fileno=sock.detach())
        plain.deadline, plain.host = self._deadline, self.host
        return plain

    monkeypatch.setattr(fetch._Connection, "_wrap", wrap)
    with open_url(
        "https://www.ecb.europa.eu/stats/x.zip", Proxy("127.0.0.1", server.port), max_bytes=10
    ) as body:
        assert body.read() == b"rates"
    server.close()
    assert wrapped == ["www.ecb.europa.eu"]
    assert b"\x03\x11www.ecb.europa.eu\x01\xbb" in server.socks_request
    assert server.request.startswith(b"GET /stats/x.zip HTTP/1.1\r\n")


def test_a_tls_handshake_that_stalls_is_cut_off_at_the_deadline() -> None:
    # the real TLS path: after the tunnel, the "source" never answers the ClientHello
    server = FakeServer(b"", socks=socks_ok(), http=False, hold=True)
    started = time.monotonic()
    with pytest.raises(FetchError, match=r"www\.ecb\.europa\.eu: the download took too long"):
        with open_url(
            "https://www.ecb.europa.eu/",
            Proxy("127.0.0.1", server.port),
            max_bytes=1,
            timeout=5,
            deadline=0.4,
        ):
            pass
    assert time.monotonic() - started < 2.5
    server.close()


def test_the_tcp_connect_waits_at_most_the_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    def stalled(address: Any, timeout: float) -> socket.socket:
        waits.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(socket, "create_connection", stalled)
    with pytest.raises(FetchError, match="the download failed"):
        with open_url(URL, max_bytes=1, timeout=60, deadline=0.5):
            pass
    assert len(waits) == 1 and 0 < waits[0] <= 0.5


def test_a_proxy_that_is_not_listening_is_a_fetch_error() -> None:
    port = socket.create_server(("127.0.0.1", 0))
    free = port.getsockname()[1]
    port.close()
    with pytest.raises(FetchError, match=r"www\.ecb\.europa\.eu: the download failed"):
        with open_url("https://www.ecb.europa.eu/", Proxy("127.0.0.1", free), max_bytes=1):
            pass


def test_a_proxy_that_refuses_the_connection_fails_the_download_naming_the_host() -> None:
    server = FakeServer(b"", socks=b"\x05\x04\x00\x01" + b"\x00" * 6, http=False)
    with pytest.raises(FetchError, match=r"www\.ecb\.europa\.eu: the SOCKS5 proxy couldn't connect: host"):
        with open_url("https://www.ecb.europa.eu/", Proxy("127.0.0.1", server.port), max_bytes=1):
            pass
    server.close()


def test_a_silent_proxy_times_out_as_a_fetch_error() -> None:
    a, b = socket.socketpair()
    with a, b:
        a.settimeout(0.05)
        with pytest.raises(FetchError, match=r"the SOCKS5 proxy failed \(TimeoutError\)"):
            socks5_connect(a, "www.ecb.europa.eu", 443)


def test_the_body_reads_whole_past_one_chunk_and_wraps_as_a_stream(direct: list[FakeServer]) -> None:
    text = b"".join(b"%d,1.0,2\n" % i for i in range(30_000))  # over 64 KiB
    direct.append(FakeServer(ok(text)))
    with open_url(URL, max_bytes=len(text)) as body:
        assert body.readable() and body.read() == text and body.read() == b""
    direct.append(FakeServer(ok(gzip.compress(text))))
    with open_url(URL, max_bytes=len(text)) as body, gzip.GzipFile(fileobj=body) as unzipped:
        lines = list(io.TextIOWrapper(unzipped, encoding="ascii", newline=""))
    assert len(lines) == 30_000 and lines[-1] == "29999,1.0,2\n"


def test_a_cut_off_download_is_an_error_not_an_end(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + b"a" * 10, then_close=True))
    with pytest.raises(FetchError, match=r"www\.bitstamp\.net: the download was cut off"):
        with open_url(URL, max_bytes=1000) as body:
            body.read()


def test_a_cut_off_chunked_download_is_a_fetch_error(direct: list[FakeServer]) -> None:
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    direct.append(FakeServer(head + b"a\r\n0123456789\r\n5\r\nab", then_close=True))
    with pytest.raises(FetchError, match=r"the download failed \(IncompleteRead\)"):
        with open_url(URL, max_bytes=1000) as body:
            body.read()


def test_a_body_that_stalls_times_out_as_a_fetch_error(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc"))
    with pytest.raises(FetchError, match=r"the download failed \(TimeoutError\)"):
        with open_url(URL, max_bytes=10, timeout=0.2) as body:
            body.read()


def test_headers_that_drip_past_the_deadline_are_cut_off(direct: list[FakeServer]) -> None:
    # each byte arrives well within the per-read timeout; only the whole-download deadline stops it
    direct.append(FakeServer(ok(b"abc") + b"x" * 100, drip=0.05))
    started = time.monotonic()
    with pytest.raises(FetchError, match=r"www\.bitstamp\.net: the download took too long"):
        with open_url(URL, max_bytes=10, timeout=5, deadline=0.3):
            pass
    assert time.monotonic() - started < 2.5


@pytest.mark.parametrize("length", [True, False])
def test_a_body_that_drips_past_the_deadline_is_cut_off(direct: list[FakeServer], length: bool) -> None:
    # ok() sends "Connection: close" (and here, maybe no length): http.client hands the socket to the
    # response, and the drip is still cut
    direct.append(FakeServer(ok(b"a" * 100, length=length), drip=0.01))
    started = time.monotonic()
    with pytest.raises(FetchError, match="took too long"):
        with open_url(URL, max_bytes=100, timeout=5, deadline=0.6) as body:
            body.read()
    assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("deadline", float("nan")),
        ("deadline", float("inf")),
        ("deadline", 0),
        ("deadline", -1),
        ("deadline", True),
        ("timeout", "1"),
        ("timeout", 0),
        ("timeout", 1e300),
        ("deadline", 86_401),
    ],
)
def test_the_timeout_and_deadline_must_be_positive_finite_seconds(name: str, value: object) -> None:
    with pytest.raises(FetchError, match=f"{name} must be a positive number of seconds"):
        with open_url(URL, max_bytes=1, **{name: value}):  # type: ignore[arg-type]  # bad on purpose
            pass


def test_a_chunked_body_ignores_a_content_length_beside_it(direct: list[FakeServer]) -> None:
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 999\r\n\r\n"
    direct.append(FakeServer(head + b"3\r\nabc\r\n0\r\n\r\n"))
    with open_url(URL, max_bytes=10) as body:  # RFC 9112 §6.1: Transfer-Encoding wins
        assert body.read() == b"abc"


def test_a_bad_chunk_size_is_a_fetch_error(direct: list[FakeServer]) -> None:
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    direct.append(FakeServer(head + b"zz\r\nabc\r\n"))
    with pytest.raises(FetchError, match=r"the download failed \((IncompleteRead|ValueError)\)"):
        with open_url(URL, max_bytes=10) as body:
            body.read()


def test_a_declared_length_may_have_surrounding_spaces(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(b"HTTP/1.1 200 OK\r\nContent-Length: 3 \t\r\n\r\nabc"))
    with open_url(URL, max_bytes=3) as body:
        assert body.read() == b"abc"


def test_a_huge_declared_length_is_too_large_not_a_crash(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\nabc"))
    with pytest.raises(FetchError, match="larger than allowed"):
        with open_url(URL, max_bytes=10):
            pass


def test_leading_zeros_in_a_declared_length_are_read_as_the_number(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(b"HTTP/1.1 200 OK\r\nContent-Length: " + b"0" * 5000 + b"3\r\n\r\nabc"))
    with open_url(URL, max_bytes=10) as body:  # 5000 digits would be past int()'s limit
        assert body.read() == b"abc"


def test_an_empty_body_fits_a_zero_limit(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"")))
    with open_url(URL, max_bytes=0) as body:
        assert body.read() == b""


def test_a_body_without_a_declared_length_ends_where_the_connection_does(direct: list[FakeServer]) -> None:
    # over plain TCP a cut looks like an end; under TLS, a close without close_notify is an error
    # (suppress_ragged_eofs=False), and the parsers validate what they read anyway (T-304)
    direct.append(FakeServer(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc", then_close=True))
    with open_url(URL, max_bytes=10) as body:
        assert body.read() == b"abc"


def test_a_body_is_closed_when_the_download_ends(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"abc")))
    with open_url(URL, max_bytes=10) as body:
        pass
    assert body.closed and body._response.isclosed()  # the response, which may own the socket, too
    with pytest.raises(ValueError, match="closed"):
        body.read()


@pytest.mark.parametrize("limit", [-1, -2, 1.5, True, "10"])
def test_the_size_limit_must_be_a_non_negative_integer(limit: object) -> None:
    with pytest.raises(FetchError, match="max_bytes must be a non-negative integer"):
        with open_url(URL, max_bytes=limit):  # type: ignore[arg-type]  # bad on purpose
            pass


@pytest.mark.parametrize("length", ["\u00b2", "-1", "1e3", ""])
def test_a_malformed_declared_length_is_refused(direct: list[FakeServer], length: str) -> None:
    direct.append(
        FakeServer(b"HTTP/1.1 200 OK\r\nContent-Length: " + length.encode("latin-1") + b"\r\n\r\nx")
    )
    with pytest.raises(FetchError, match="length is malformed"):
        with open_url(URL, max_bytes=10):
            pass


def test_a_proxy_that_stalls_its_handshake_is_cut_off_at_the_deadline() -> None:
    silent = socket.create_server(("127.0.0.1", 0))  # accepts (through the backlog), never answers
    started = time.monotonic()
    with silent, pytest.raises(FetchError, match=r"www\.ecb\.europa\.eu: the download took too long"):
        with open_url(
            "https://www.ecb.europa.eu/",
            Proxy("127.0.0.1", silent.getsockname()[1]),
            max_bytes=1,
            timeout=5,
            deadline=0.3,
        ):
            pass
    assert time.monotonic() - started < 2.5


def test_an_unexpected_error_in_the_socks_handshake_still_closes_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(b"", socks=socks_ok(), http=False)
    closed: list[bool] = []
    real_close = socket.socket.close

    def boom(sock: socket.socket, *args: Any) -> None:
        raise RuntimeError("unexpected")

    def close(self: socket.socket) -> None:
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(fetch, "socks5_connect", boom)
    monkeypatch.setattr(socket.socket, "close", close)
    with pytest.raises(RuntimeError, match="unexpected"):
        with open_url("https://www.ecb.europa.eu/", Proxy("127.0.0.1", server.port), max_bytes=1):
            pass
    assert closed
    monkeypatch.undo()
    server.close()


def test_a_body_closed_twice_closes_the_response_once(direct: list[FakeServer]) -> None:
    direct.append(FakeServer(ok(b"abc")))
    with open_url(URL, max_bytes=10) as body:
        body.close()
        body.close()
        assert body.closed


def test_a_socks_reply_that_drips_is_cut_at_the_deadline_not_after_it() -> None:
    # each byte comes well within the timeout; only the time left, rechecked per read, stops it
    server = FakeServer(b"", socks=socks_ok(), http=False, socks_drip=0.2)
    started = time.monotonic()
    with pytest.raises(FetchError, match="took too long"):
        with open_url(
            "https://www.ecb.europa.eu/",
            Proxy("127.0.0.1", server.port),
            max_bytes=1,
            timeout=5,
            deadline=0.35,
        ):
            pass
    assert time.monotonic() - started < 1.2  # the reply alone takes 2 s
    server.close()


def test_the_tls_layer_refuses_a_close_without_close_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    real = ssl.SSLContext.wrap_socket

    def spy(self: ssl.SSLContext, sock: socket.socket, **kwargs: Any) -> ssl.SSLSocket:
        calls.append(kwargs)
        return real(self, sock, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", spy)
    server = FakeServer(b"", socks=socks_ok(), http=False)  # closes after the tunnel: an abrupt end
    with pytest.raises(FetchError, match="the download failed"):
        with open_url("https://www.ecb.europa.eu/", Proxy("127.0.0.1", server.port), max_bytes=1):
            pass
    server.close()
    (kwargs,) = calls
    assert kwargs["suppress_ragged_eofs"] is False and kwargs["do_handshake_on_connect"] is False
    assert kwargs["server_hostname"] == "www.ecb.europa.eu"

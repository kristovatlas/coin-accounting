"""Bulk price downloads: the app's only internet traffic (flow F3; PLAN §6, ADR 0007; THREAT_MODEL T-301,
T-302, T-303, T-305).

- **HTTPS only,** with certificate and host-name checks and TLS 1.2 or later. There is no opt-out.
- **Only the hosts in HOSTS,** ADR 0007's F3 destinations, on port 443. A redirect is refused, not
  followed, so a source can't send the request anywhere else.
- **Optionally through a SOCKS5 proxy on loopback** (Tor), with remote DNS: the host name goes to the
  proxy, never to a local resolver, so no lookup leaves the machine either (T-302).
- **One fixed request shape:** GET, with the User-Agent, Accept and Accept-Language of a common browser
  (Tor Browser's), never a library's or this app's name. That keeps the request ordinary; it can't make
  it indistinguishable from a browser (the TLS handshake is Python's). The URLs come from the parsers'
  modules, never from user records (T-301).
- **Tor stream isolation:** each connection offers the proxy a fresh random username and password,
  so Tor puts it on its own circuit, apart from the user's other Tor traffic (Tor's IsolateSOCKSAuth,
  on by default; Bitcoin Core's -proxyrandomize does the same).
- **Bounded:** each response is a stream of at most `max_bytes`, with a timeout on every read and a
  deadline for the whole download. A body shorter than its declared length is an error, not an end.

Fetching happens only when the user asks for a refresh (services). Nothing here runs on import.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import secrets
import socket
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, NoReturn
from urllib.parse import urlsplit

HOSTS: Final = frozenset({"api.bitcoincharts.com", "www.bitstamp.net", "www.ecb.europa.eu"})  # ADR 0007
# Tor Browser's headers (Firefox 140 ESR): a common browser's, not a library's or this app's. Review them
# when Tor Browser moves to a new ESR (the next is due in 2027).
USER_AGENT: Final = "Mozilla/5.0 (Windows NT 10.0; rv:140.0) Gecko/20100101 Firefox/140.0"
HEADERS: Final = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
TIMEOUT: Final = 60.0  # seconds, for the connection and for each read
DEADLINE: Final = 1800.0  # seconds for a whole download: a slow drip can't hold a refresh forever


class FetchError(Exception):
    """A download that failed or was refused. The message names the host where there is one (a
    malformed URL or proxy setting has none), never a user value."""


@dataclass(frozen=True)
class Proxy:
    """A SOCKS5 proxy on this machine (Tor's is 127.0.0.1:9050). Only a loopback address is accepted:
    the proxy sees every host name, so it must be the user's own."""

    host: str
    port: int

    def __post_init__(self) -> None:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise FetchError("the SOCKS5 proxy must be a loopback address, such as 127.0.0.1")
        if type(self.port) is not int or not 0 < self.port < 65536:
            raise FetchError("the SOCKS5 proxy needs a port from 1 to 65535")


class Body(io.RawIOBase):
    """A response body as a raw binary stream (wrap it in `io.BufferedReader`, `gzip.GzipFile` or
    `io.TextIOWrapper`). Every failure is a FetchError naming the host: more than `max_bytes`, fewer
    bytes than the declared length (a cut-off download), the deadline passed, or a read that failed."""

    def __init__(
        self,
        response: http.client.HTTPResponse,
        host: str,
        max_bytes: int,
        declared: int | None,
        deadline: float,
    ) -> None:
        super().__init__()
        self._response, self._host, self._max, self._declared = response, host, max_bytes, declared
        self._deadline, self._read = deadline, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        if time.monotonic() > self._deadline:
            _fail(f"{self._host}: the download took too long")
        view = memoryview(buffer).cast("B")
        room = min(len(view), self._max - self._read + 1)  # one byte past the limit shows it's exceeded
        try:
            n = self._response.readinto(view[:room]) if room else 0
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"{self._host}: the download failed ({type(e).__name__})") from None
        self._read += n
        if self._read > self._max:
            _fail(f"{self._host}: the response is larger than allowed")
        if n == 0 and len(view) and self._declared is not None and self._read != self._declared:
            _fail(f"{self._host}: the download was cut off")
        return n


@contextmanager
def open_url(
    url: str,
    proxy: Proxy | None = None,
    *,
    max_bytes: int,
    timeout: float = TIMEOUT,
    deadline: float = DEADLINE,
) -> Iterator[Body]:
    """GET `url` and yield its body as a bounded stream; the connection closes on leaving the block.
    `deadline` is in seconds for the whole download, from now."""
    if type(max_bytes) is not int or max_bytes < 0:
        _fail("max_bytes must be a non-negative integer")
    until = time.monotonic() + deadline
    host, target = _check_url(url)
    if proxy is None:
        conn: http.client.HTTPSConnection = http.client.HTTPSConnection(
            host, 443, timeout=timeout, context=_context()
        )
    else:
        conn = _SocksHTTPSConnection(host, proxy, timeout)
    try:
        try:
            conn.request("GET", target, headers=HEADERS)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"{host}: the download failed ({type(e).__name__})") from None
        if response.status != http.client.OK:
            _fail(f"{host}: HTTP {response.status} (a redirect or error; nothing is followed)")
        length = response.getheader("Content-Length")
        declared = int(length) if length is not None and length.isascii() and length.isdigit() else None
        if length is not None and declared is None:
            _fail(f"{host}: the response's length is malformed")
        if declared is not None and declared > max_bytes:
            _fail(f"{host}: the response is larger than allowed")
        yield Body(response, host, max_bytes, declared, until)
    finally:
        conn.close()


def _check_url(url: str) -> tuple[str, str]:
    """(host, path?query) for an https URL to an allowed host on the default port, or FetchError."""
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        _fail("not a valid URL")
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        _fail("only plain https URLs are fetched")
    if parts.hostname not in HOSTS or parts.netloc != parts.hostname:  # so no port, not even :443
        _fail(f"{parts.hostname!r} is not a price source (ADR 0007)")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if not target.isascii() or any(c in target for c in " \r\n\t"):
        _fail("not a valid URL")
    return parts.hostname, target


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # certificate and host-name checks, the system's trust store
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # Python's default today; pinned so it can't drift
    return ctx


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to `host` through a loopback SOCKS5 proxy, which resolves the name (remote DNS)."""

    def __init__(self, host: str, proxy: Proxy, timeout: float) -> None:
        self._tls = _context()
        super().__init__(host, 443, timeout=timeout, context=self._tls)
        self._proxy = proxy

    def connect(self) -> None:
        sock = socket.create_connection((self._proxy.host, self._proxy.port), timeout=self.timeout)
        try:
            try:
                socks5_connect(sock, self.host, 443)
            except FetchError as e:
                raise FetchError(f"{self.host}: {e}") from None
            self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


_SOCKS_ERRORS: Final = {
    1: "general failure",
    2: "not allowed",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def socks5_connect(sock: socket.socket, host: str, port: int) -> None:
    """Ask a SOCKS5 proxy (RFC 1928), already connected on `sock`, to connect to `host`:`port` by
    name. It offers a fresh random username and password (RFC 1929), which Tor uses only to keep this
    connection on its own circuit, or no authentication if the proxy prefers. Raises FetchError if the
    proxy refuses or answers oddly."""
    name = host.encode("ascii")
    if not 0 < len(name) < 256:
        _fail("the host name is too long for SOCKS5")
    sock.sendall(b"\x05\x02\x02\x00")  # version 5, two methods: username/password, or none
    chosen = _recv(sock, 2)
    if chosen == b"\x05\x02":
        user, password = secrets.token_hex(8).encode(), secrets.token_hex(8).encode()
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        if _recv(sock, 2) != b"\x01\x00":
            _fail("the SOCKS5 proxy refused the isolation credentials")
    elif chosen != b"\x05\x00":
        _fail("the SOCKS5 proxy refused (it wants another method, or isn't SOCKS5)")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    version, reply, _, kind = _recv(sock, 4)
    if version != 5:
        _fail("the SOCKS5 proxy answered oddly")
    if reply != 0:
        _fail(f"the SOCKS5 proxy couldn't connect: {_SOCKS_ERRORS.get(reply, 'unknown error')}")
    if kind == 1:
        _recv(sock, 4 + 2)
    elif kind == 4:
        _recv(sock, 16 + 2)
    elif kind == 3:
        _recv(sock, _recv(sock, 1)[0] + 2)
    else:
        _fail("the SOCKS5 proxy answered oddly")


def _recv(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
        except OSError as e:
            raise FetchError(f"the SOCKS5 proxy failed ({type(e).__name__})") from None
        if not chunk:
            _fail("the SOCKS5 proxy closed the connection")
        data += chunk
    return data


def _fail(message: str) -> NoReturn:
    raise FetchError(message)

"""Bulk price downloads: the app's only internet traffic (flow F3; PLAN §6, ADR 0007; THREAT_MODEL T-301,
T-302, T-303, T-305).

- **HTTPS only,** with certificate and host-name checks and TLS 1.2 or later. There is no opt-out.
- **Only the hosts in HOSTS,** ADR 0007's F3 destinations, on port 443. A redirect is refused, not
  followed, so a source can't send the request anywhere else.
- **Optionally through a SOCKS5 proxy on loopback** (Tor), with remote DNS: the host name goes to the
  proxy, never to a local resolver, so no lookup leaves the machine either (T-302).
- **One fixed request shape:** GET, with a common browser User-Agent and nothing else that could tell
  this app or its user apart. The URLs come from the parsers' modules, never from user records (T-301).
- **Bounded:** each response is read as a stream of at most `max_bytes`, with a timeout on every read.

Fetching happens only when the user asks for a refresh (services). Nothing here runs on import.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, NoReturn
from urllib.parse import urlsplit

HOSTS: Final = frozenset({"api.bitcoincharts.com", "www.bitstamp.net", "www.ecb.europa.eu"})  # ADR 0007
# A common desktop browser's User-Agent, the one Tor Browser also sends: not a library's or this app's.
USER_AGENT: Final = "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"
TIMEOUT: Final = 60.0  # seconds, for the connection and for each read
_CHUNK: Final = 1 << 16


class FetchError(Exception):
    """A download that failed or was refused. The message names the host, never a user value."""


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


class Body:
    """A response body as a binary stream, cut off with FetchError past `max_bytes`."""

    def __init__(self, response: http.client.HTTPResponse, host: str, max_bytes: int) -> None:
        self._response, self._host, self._left = response, host, max_bytes

    def read(self, size: int = -1) -> bytes:
        want = _CHUNK if size is None or size < 0 else min(size, _CHUNK)
        data = self._response.read(min(want, self._left + 1))
        self._left -= len(data)
        if self._left < 0:
            _fail(f"{self._host}: the response is larger than allowed")
        return data

    def readable(self) -> bool:
        return True


@contextmanager
def open_url(
    url: str, proxy: Proxy | None = None, *, max_bytes: int, timeout: float = TIMEOUT
) -> Iterator[Body]:
    """GET `url` and yield its body as a bounded stream; the connection closes on leaving the block."""
    host, target = _check_url(url)
    if proxy is None:
        conn: http.client.HTTPSConnection = http.client.HTTPSConnection(
            host, 443, timeout=timeout, context=_context()
        )
    else:
        conn = _SocksHTTPSConnection(host, proxy, timeout)
    try:
        try:
            conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"{host}: the download failed ({type(e).__name__})") from None
        if response.status != http.client.OK:
            _fail(f"{host}: HTTP {response.status} (a redirect or error; nothing is followed)")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > max_bytes):
            _fail(f"{host}: the response is larger than allowed")
        yield Body(response, host, max_bytes)
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
        super().__init__(host, 443, timeout=timeout, context=_context())
        self._proxy = proxy
        self._tls = _context()

    def connect(self) -> None:
        sock = socket.create_connection((self._proxy.host, self._proxy.port), timeout=self.timeout)
        try:
            socks5_connect(sock, self.host, 443)
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
    name, with no authentication. Raises FetchError if the proxy refuses or answers oddly."""
    name = host.encode("ascii")
    if not 0 < len(name) < 256:
        _fail("the host name is too long for SOCKS5")
    sock.sendall(b"\x05\x01\x00")  # version 5, one method: no authentication
    if _recv(sock, 2) != b"\x05\x00":
        _fail("the SOCKS5 proxy refused (it wants authentication, or isn't SOCKS5)")
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

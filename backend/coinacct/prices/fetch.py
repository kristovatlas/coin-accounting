"""Bulk price downloads: the app's only internet traffic (flow F3; PLAN §6, ADR 0039; THREAT_MODEL T-301,
T-302, T-303, T-305).

- **HTTPS only,** with certificate and host-name checks and TLS 1.2 or later. There is no opt-out.
- **Only the hosts in HOSTS,** the F3 destinations (ADR 0039), on port 443. A redirect is refused, not
  followed, so a source can't send the request anywhere else.
- **Optionally through a SOCKS5 proxy on loopback** (Tor), with remote DNS: the host name goes to the
  proxy, never to a local resolver, so no lookup leaves the machine either (T-302).
- **One fixed request shape:** GET, with Tor Browser's User-Agent and a generic Accept (PLAN §6: a
  common browser User-Agent and nothing else), never a library's or this app's name. That keeps the
  request ordinary; it can't make it indistinguishable from a browser (the TLS handshake is Python's).
  The URLs come from the parsers' modules, never from user records (T-301).
- **Tor stream isolation:** each connection offers the proxy a fresh random username and password,
  so Tor puts it on its own circuit, apart from the user's other Tor traffic (Tor's IsolateSOCKSAuth,
  on by default; Bitcoin Core's -proxyrandomize does the same).
- **Bounded:** each response is a stream of at most `max_bytes`. Every blocking step waits at most
  `timeout`, and at most the time left before the download's deadline. A body shorter than its
  declared length is an error, not an end, and so is a TLS close without close_notify; the parsers
  still validate what they read (T-304).

The sources' downloads (`download_ohlc`, `download_ecb`) are at the end. Their
requests are fixed by the calendar alone: the same for every user (T-301). Fetching happens only when
the user asks for a refresh (services). Nothing here runs on import.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import secrets
import socket
import ssl
import time
import zipfile
import zlib
from collections.abc import Buffer, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, NoReturn, cast
from urllib.parse import urlsplit

from coinacct.prices import CURRENCIES, DailyPrice
from coinacct.prices.bitstamp import typical_by_day
from coinacct.prices.fx import Rates, ecb_rates

# ADR 0039: the bitcoincharts archive is gone (unreachable, and a lapsed domain could be taken over);
# Coin Metrics joins when its downloader lands.
HOSTS: Final = frozenset({"www.bitstamp.net", "www.ecb.europa.eu"})
# Tor Browser's User-Agent (Firefox 140 ESR): a common browser's, not a library's or this app's. Review
# it at each Tor Browser major release.
USER_AGENT: Final = "Mozilla/5.0 (Windows NT 10.0; rv:140.0) Gecko/20100101 Firefox/140.0"
# PLAN §6: a common browser User-Agent and nothing else about the client (`Accept: */*` is generic).
HEADERS: Final = {"User-Agent": USER_AGENT, "Accept": "*/*"}
TIMEOUT: Final = 60.0  # seconds, for the connection and for each read
DEADLINE: Final = 1800.0  # seconds for a whole download: a slow drip can't hold a refresh forever
_LONGEST: Final = 86_400.0  # a day: the most either may be (socket timeouts overflow far beyond it)


class FetchError(Exception):
    """A download that failed or was refused. The message names the host where there is one (a
    malformed URL or proxy setting has none), never a user value."""


class Cancelled(FetchError):
    """The user cancelled the refresh: not a failure, and never a partial result."""


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
        deadline: _Deadline,
    ) -> None:
        super().__init__()
        self._response, self._host, self._max, self._declared = response, host, max_bytes, declared
        self._deadline, self._read = deadline, 0

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        if not self.closed:
            self._response.close()  # it may own the socket (a "Connection: close" response)
        super().close()

    def readinto(self, buffer: Buffer) -> int:
        if self.closed:
            raise ValueError("read from a closed download")
        self._deadline.left(self._host)
        view = memoryview(buffer).cast("B")
        room = min(len(view), self._max - self._read + 1)  # one byte past the limit shows it's exceeded
        try:
            n = self._response.readinto(view[:room]) if room else 0
        except (OSError, http.client.HTTPException, ValueError) as e:  # ValueError: a bad chunk size
            self._deadline.left(self._host)  # a read cut by the deadline is "too long", not "failed"
            raise FetchError(f"{self._host}: the download failed ({type(e).__name__})") from None
        self._read += n
        if self._read > self._max:
            _fail(f"{self._host}: the response is larger than allowed")
        if n == 0 and len(view) and self._declared is not None and self._read != self._declared:
            _fail(f"{self._host}: the download was cut off")
        return n


class _Deadline:
    """The whole download's deadline, enforced without a thread: every blocking step (the TCP connect,
    each SOCKS read, the TLS handshake, each read of headers or body) gets a socket timeout of the
    time left, at most `timeout`. A source that sends a byte just under each timeout can't hold the
    refresh past the deadline (the job worker is single, architecture §3). CPython's TLS layer treats
    a timeout as the limit for a whole call, the handshake included (`ssl` docs, since 3.5). The one
    step no timeout reaches is the direct path's DNS lookup, which the system resolver bounds; through
    a proxy there is no local lookup at all."""

    def __init__(self, seconds: float, timeout: float, cancelled: Callable[[], bool] | None = None) -> None:
        self._until, self._timeout, self._cancelled = time.monotonic() + seconds, timeout, cancelled

    def left(self, host: str) -> float:
        """The timeout for the next blocking step, or FetchError once the deadline has passed, or
        Cancelled once the refresh is cancelled: so a cancel takes effect within one `timeout`."""
        if self._cancelled is not None and self._cancelled():
            raise Cancelled(f"{host}: the refresh was cancelled")
        left = self._until - time.monotonic()
        if left <= 0:
            _fail(f"{host}: the download took too long")
        return min(self._timeout, left)


class _DeadlineIO(socket.socket):
    """A socket whose every read and write is bounded by the download's deadline. http.client reads
    through `makefile`, which calls `recv_into`; a buffered read that loops asks again each time, so
    each wait is cut to the time left. Used under TLS (`_DeadlineSocket`)."""

    deadline: _Deadline
    host: str

    def recv_into(self, buffer: Buffer, *args: Any) -> int:
        self.settimeout(self.deadline.left(self.host))
        return super().recv_into(buffer, *args)

    def sendall(self, data: Buffer, *args: Any) -> None:
        self.settimeout(self.deadline.left(self.host))
        super().sendall(data, *args)


class _DeadlineSocket(_DeadlineIO, ssl.SSLSocket):
    """The TLS socket the connection's context makes (its `sslsocket_class`)."""


@contextmanager
def open_url(  # noqa: PLR0913 - the URL, the proxy, and four keyword-only limits
    url: str,
    proxy: Proxy | None = None,
    *,
    max_bytes: int,
    timeout: float = TIMEOUT,
    deadline: float = DEADLINE,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[Body]:
    """GET `url` and yield its body as a bounded stream; the connection closes on leaving the block.
    `deadline` is in seconds for the whole download, from now. `cancelled`, if given, is asked before
    connecting and before every read of the body: a cancel takes effect within one `timeout`."""
    if type(max_bytes) is not int or max_bytes < 0:
        _fail("max_bytes must be a non-negative integer")
    for name, value in (("timeout", timeout), ("deadline", deadline)):
        if type(value) not in (int, float) or not 0 < value <= _LONGEST:  # NaN fails both comparisons
            _fail(f"{name} must be a positive number of seconds")
    host, target = _check_url(url)
    limit = _Deadline(deadline, timeout, cancelled)  # the first connect asks it: cancelled, no connection
    conn = _Connection(host, proxy, limit)
    body: Body | None = None
    try:
        try:
            conn.request("GET", target, headers=HEADERS)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException, ValueError) as e:
            limit.left(host)  # a step cut by the deadline (or a cancel) says so, not "failed"
            raise FetchError(f"{host}: the download failed ({type(e).__name__})") from None
        if response.status != http.client.OK:
            _fail(f"{host}: HTTP {response.status} (a redirect or error; nothing is followed)")
        declared = (
            None if response.chunked else _length(response.getheader("Content-Length"), host, max_bytes)
        )
        body = Body(response, host, max_bytes, declared, limit)
        yield body
    finally:
        if body is not None:
            body.close()
        conn.close()


def _length(header: str | None, host: str, max_bytes: int) -> int | None:
    """The declared Content-Length, or None. Surrounding spaces and tabs are allowed (RFC 9110 §5.5);
    anything but ASCII digits is malformed, and a length past `max_bytes` is refused before reading.
    The digits are counted before conversion, so a huge header can't reach int()'s length limit."""
    if header is None:
        return None
    text = header.strip(" \t")
    if not text or not text.isascii() or not text.isdigit():
        _fail(f"{host}: the response's length is malformed")
    digits = text.lstrip("0") or "0"
    if len(digits) > len(str(max_bytes)) or int(digits) > max_bytes:
        _fail(f"{host}: the response is larger than allowed")
    return int(digits)


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
        _fail(f"{parts.hostname!r} is not a price source (ADR 0039)")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if not target.isascii() or any(c in target for c in " \r\n\t"):
        _fail("not a valid URL")
    return parts.hostname, target


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # certificate and host-name checks, the system's trust store
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # Python's default today; pinned so it can't drift
    return ctx


class _Connection(http.client.HTTPSConnection):
    """HTTPS to `host`, directly or through a loopback SOCKS5 proxy (which resolves the name: remote
    DNS), with every step bounded by the download's deadline. TLS wraps the socket without a
    handshake first, so the handshake runs under the deadline's timeout too; a TLS close without
    close_notify is an error, not an end, so a body with no declared length can't be cut unnoticed."""

    def __init__(self, host: str, proxy: Proxy | None, deadline: _Deadline) -> None:
        self._tls = _context()
        self._tls.sslsocket_class = _DeadlineSocket
        super().__init__(host, 443, timeout=TIMEOUT, context=self._tls)
        self._proxy, self._deadline = proxy, deadline

    def connect(self) -> None:
        sock = self._tcp()
        try:
            self.sock = self._wrap(sock)
        except BaseException:
            sock.close()
            raise

    def _tcp(self) -> socket.socket:
        """A TCP connection to the source, or through the proxy to it."""
        to = (self.host, 443) if self._proxy is None else (self._proxy.host, self._proxy.port)
        sock = socket.create_connection(to, timeout=self._deadline.left(self.host))
        if self._proxy is not None:
            try:
                socks5_connect(sock, self.host, 443, lambda: self._deadline.left(self.host))
            except FetchError as e:
                sock.close()
                self._deadline.left(self.host)  # a handshake cut by the deadline is "too long"
                raise FetchError(f"{self.host}: {e}") from None
            except BaseException:
                sock.close()
                raise
        return sock

    def _wrap(self, sock: socket.socket) -> socket.socket:
        tls = self._tls.wrap_socket(
            sock, server_hostname=self.host, do_handshake_on_connect=False, suppress_ragged_eofs=False
        )
        tls = cast(_DeadlineSocket, tls)  # the context's sslsocket_class, set in __init__
        tls.deadline, tls.host = self._deadline, self.host
        tls.settimeout(self._deadline.left(self.host))
        tls.do_handshake()
        return tls


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


def socks5_connect(
    sock: socket.socket, host: str, port: int, timeout: Callable[[], float] | None = None
) -> None:
    """Ask a SOCKS5 proxy (RFC 1928), already connected on `sock`, to connect to `host`:`port` by
    name. It offers a fresh random username and password (RFC 1929), which Tor uses only to keep this
    connection on its own circuit, or no authentication if the proxy prefers. Raises FetchError if the
    proxy refuses or answers oddly. `timeout`, if given, sets the socket timeout before each read.

    """
    name = host.encode("ascii")
    if not 0 < len(name) < 256:
        _fail("the host name is too long for SOCKS5")
    sock.sendall(b"\x05\x02\x02\x00")  # version 5, two methods: username/password, or none
    chosen = _recv(sock, timeout, 2)
    if chosen == b"\x05\x02":
        user, password = secrets.token_hex(8).encode(), secrets.token_hex(8).encode()
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        if _recv(sock, timeout, 2) != b"\x01\x00":
            _fail("the SOCKS5 proxy refused the isolation credentials")
    elif chosen != b"\x05\x00":
        _fail("the SOCKS5 proxy refused (it wants another method, or isn't SOCKS5)")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    version, reply, _, kind = _recv(sock, timeout, 4)
    if version != 5:
        _fail("the SOCKS5 proxy answered oddly")
    if reply != 0:
        _fail(f"the SOCKS5 proxy couldn't connect: {_SOCKS_ERRORS.get(reply, 'unknown error')}")
    if kind == 1:
        _recv(sock, timeout, 4 + 2)
    elif kind == 4:
        _recv(sock, timeout, 16 + 2)
    elif kind == 3:
        _recv(sock, timeout, _recv(sock, timeout, 1)[0] + 2)
    else:
        _fail("the SOCKS5 proxy answered oddly")


def _recv(sock: socket.socket, timeout: Callable[[], float] | None, n: int) -> bytes:
    data = b""
    while len(data) < n:
        if timeout is not None:
            sock.settimeout(timeout())
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


# --- the sources (ADR 0039). Every URL is a constant or built from the calendar alone.

OHLC_URL: Final = "https://www.bitstamp.net/api/v2/ohlc/btc{currency}/?step=86400&limit=1000&start={start}"
ECB_URL: Final = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
ECB_MEMBER: Final = "eurofxref-hist.csv"
OHLC_FIRST: Final = date(2011, 8, 18)  # Bitstamp's first trading day: every page starts on this grid
OHLC_PAGE_DAYS: Final = 1000  # Bitstamp's largest `limit`
OHLC_MAX: Final = 1 << 20  # one page of 1000 candles is about 150 kB
ECB_MAX: Final = 16 << 20  # the zip is well under 1 MB
ECB_CSV_MAX: Final = 64 << 20  # and its one file a few MB, unzipped
# The only zip flag bits accepted: a data descriptor (0x08) and UTF-8 names (0x800). Encryption (0x01,
# 0x40), patched data (0x20) and anything newer are refused before zipfile meets them.
_ZIP_FLAGS_OK: Final = 0x08 | 0x800


def ohlc_pages(complete_before: date) -> list[str]:
    """Every OHLC page's start for one pair, from Bitstamp's first day to before `complete_before`: a
    fixed grid, so the request set depends on nothing but the date (T-301)."""
    starts = []
    day = OHLC_FIRST
    while day < complete_before:
        starts.append(str(int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())))
        day = date.fromordinal(day.toordinal() + OHLC_PAGE_DAYS)
    return starts


def download_ohlc(
    currency: str, complete_before: date, proxy: Proxy | None, cancelled: Callable[[], bool]
) -> tuple[list[DailyPrice], str]:
    """Every complete day's typical price for BTC in `currency`, page by page, and a SHA-256 over the
    pages as fetched, each prefixed with its start and length. Every candle on a page must fall on the
    page's own days (`typical_by_day`'s `within`): a page that answers for others is refused."""
    if currency not in CURRENCIES:
        _fail(f"unsupported currency {currency!r}")
    out: list[DailyPrice] = []
    sha = hashlib.sha256()
    starts = ohlc_pages(complete_before)
    for start in starts:
        url = OHLC_URL.format(currency=currency.lower(), start=start)
        with open_url(url, proxy, max_bytes=OHLC_MAX, cancelled=cancelled) as body:
            data = body.read()
        sha.update(f"{start}:{len(data)}\n".encode() + data)
        first = datetime.fromtimestamp(int(start), UTC).date()
        end = date.fromordinal(first.toordinal() + OHLC_PAGE_DAYS)  # the grid's next start, last page too
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            _fail("www.bitstamp.net: an OHLC page isn't ASCII")
        page = typical_by_day(text, currency, complete_before, within=(first, end))
        out.extend(page)
    return out, sha.hexdigest()


def download_ecb(proxy: Proxy | None, cancelled: Callable[[], bool]) -> tuple[Rates, str]:
    """The ECB's reference rates for USD and the display currencies, from its zipped history, and the
    SHA-256 of the zip as fetched."""
    with open_url(ECB_URL, proxy, max_bytes=ECB_MAX, cancelled=cancelled) as body:
        data = body.read()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo(ECB_MEMBER)
            if len(archive.infolist()) != 1:
                _fail("www.ecb.europa.eu: the zip isn't the one expected file")
            if (
                info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or info.flag_bits & ~_ZIP_FLAGS_OK
            ):
                _fail("www.ecb.europa.eu: the zip's file is encrypted or packed an unexpected way")
            with archive.open(info) as member:  # bounded by what is read, not the header's claim
                text = member.read(ECB_CSV_MAX + 1)
    except (
        zipfile.BadZipFile,
        KeyError,
        OSError,
        EOFError,
        zlib.error,
        NotImplementedError,
        ValueError,
    ) as e:
        _fail(f"www.ecb.europa.eu: the rates aren't a valid zip ({type(e).__name__})")
    if len(text) > ECB_CSV_MAX:
        _fail("www.ecb.europa.eu: the rates file is larger than allowed")
    if any(b < 0x20 and b not in (0x0A, 0x0D) for b in text) or b"\r" in text.replace(b"\r\n", b""):
        _fail("www.ecb.europa.eu: the rates file holds a control character")  # in any column, used or not
    try:
        lines = text.decode("ascii").split("\n")
    except UnicodeDecodeError:
        _fail("www.ecb.europa.eu: the rates file isn't ASCII")
    return ecb_rates(lines), hashlib.sha256(data).hexdigest()

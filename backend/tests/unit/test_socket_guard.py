"""The socket guard itself (ENGINEERING §3.2, THREAT_MODEL T-305).

The blocked cases use documentation-only addresses (RFC 5737, RFC 3849) and reserved names
(RFC 2606) with a short timeout, so if the guard were missing the test would fail on a timeout
or resolver error rather than on the guard's own exception.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Iterator

import pytest

from tests import socket_guard
from tests.socket_guard import OutboundConnectionBlockedError, is_loopback


@pytest.fixture(autouse=True)
def _expected_blocks() -> Iterator[None]:
    """These tests trigger blocks on purpose; clear them before conftest's check runs."""
    yield
    socket_guard.BLOCKED.clear()


def test_connect_to_a_non_loopback_ipv4_address_is_blocked_t305() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        with pytest.raises(OutboundConnectionBlockedError, match=re.escape("192.0.2.1")):
            s.connect(("192.0.2.1", 9))


def test_connect_ex_to_a_non_loopback_ipv6_address_is_blocked_t305() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        with pytest.raises(OutboundConnectionBlockedError, match="2001:db8::1"):
            s.connect_ex(("2001:db8::1", 9, 0, 0))


def test_udp_datagram_to_a_non_loopback_address_is_blocked_t305() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(OutboundConnectionBlockedError, match="sendto"):
            s.sendto(b"x", ("192.0.2.1", 53))


@pytest.mark.parametrize(
    "name", ["example.invalid", "127.0.0.1.example.invalid", "localhost.example.invalid"]
)
def test_name_lookups_of_non_loopback_names_are_blocked_t305(name: str) -> None:
    with pytest.raises(OutboundConnectionBlockedError, match="name lookup"):
        socket.getaddrinfo(name, 443)


def test_create_connection_by_name_is_blocked_before_any_lookup_t305() -> None:
    with pytest.raises(OutboundConnectionBlockedError):
        socket.create_connection(("example.invalid", 80), timeout=0.5)


@pytest.mark.parametrize("lookup", ["gethostbyname", "gethostbyname_ex", "gethostbyaddr"])
def test_legacy_resolver_calls_are_blocked_t305(lookup: str) -> None:
    with pytest.raises(OutboundConnectionBlockedError, match=lookup):
        getattr(socket, lookup)("example.invalid")


def test_sendmsg_to_a_non_loopback_address_is_blocked_t305() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(OutboundConnectionBlockedError, match="sendmsg"):
            s.sendmsg([b"x"], [], 0, ("192.0.2.1", 53))


def test_sendto_without_an_address_still_fails_normally() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(TypeError):
            s.sendto(b"x")  # type: ignore[call-overload]  # the missing address is the point


def test_reverse_lookup_of_a_non_loopback_address_is_blocked_t305() -> None:
    with pytest.raises(OutboundConnectionBlockedError, match="getnameinfo"):
        socket.getnameinfo(("192.0.2.1", 80), 0)


def test_legacy_lookups_of_loopback_still_work() -> None:
    assert socket.gethostbyname("localhost").startswith("127.")
    assert socket.getnameinfo(("127.0.0.1", 80), socket.NI_NUMERICHOST)[0] == "127.0.0.1"


def test_loopback_connections_still_work() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=2) as client:
            conn, _ = server.accept()
            with conn:
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("::1%lo", True),
        ("localhost", True),
        ("LOCALHOST", True),
        (b"127.0.0.1", True),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("localhost.", False),
        ("example.invalid", False),
        (None, False),
    ],
)
def test_loopback_classification(host: object, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_the_guard_wraps_every_other_plugins_configure_and_unconfigure_t305() -> None:
    # pluggy runs newest-registered hooks first; tryfirst/trylast keep the guard on around the
    # built-in and `-p` plugins (e.g. pastebin, which uploads in its own pytest_unconfigure).
    from tests import conftest  # noqa: PLC0415 - the hooks under test live there

    assert conftest.pytest_configure.pytest_impl["tryfirst"] is True  # type: ignore[attr-defined]
    assert conftest.pytest_unconfigure.pytest_impl["trylast"] is True  # type: ignore[attr-defined]


def test_a_swallowed_block_is_still_recorded_and_fails_the_check_t305() -> None:
    # Telemetry code typically wraps its phone-home in `except Exception`.
    try:
        socket.getaddrinfo("telemetry.example.invalid", 443)
    except Exception:  # noqa: S110 - the swallowing is the point
        pass
    assert socket_guard.BLOCKED == ["socket guard: name lookup of 'telemetry.example.invalid' (T-305)"]
    with pytest.raises(OutboundConnectionBlockedError, match=re.escape("telemetry.example.invalid")):
        socket_guard.raise_if_blocked()
    assert socket_guard.BLOCKED == []

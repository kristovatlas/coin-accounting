"""Test-time socket guard (ENGINEERING §3.2, THREAT_MODEL T-305).

While installed, any connection, datagram or name lookup that isn't for loopback fails the test
before anything leaves the machine. Tests talk only to the regtest node and to the app under
test, both on loopback. This catches accidental phoning home; it is not a security boundary
against malicious code in the process (R-4).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

# Names that resolve to loopback without asking a resolver (RFC 6761 §6.3).
LOOPBACK_NAMES = frozenset({"localhost"})


class OutboundConnectionBlockedError(AssertionError):
    """A test tried to reach something other than loopback."""


def is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        # An IPv6 scope id ("::1%lo") doesn't change whether the address is loopback.
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check(family: int, address: Any, what: str) -> None:
    if family == getattr(socket, "AF_UNIX", object()):
        return  # a local socket file: no network
    host = address[0] if isinstance(address, tuple) and address else address
    if not is_loopback(host):
        raise OutboundConnectionBlockedError(f"socket guard: {what} to {host!r} is not loopback (T-305)")


@contextmanager
def installed() -> Iterator[None]:
    """Patch the socket module for the duration of the block."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        _check(self.family, address, "connect")
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(self.family, address, "connect")
        return real_connect_ex(self, address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        if args:  # sendto(data, address) or sendto(data, flags, address)
            _check(self.family, args[-1], "sendto")
        return real_sendto(self, data, *args)

    def sendmsg(self: socket.socket, buffers: Any, *args: Any, **kwargs: Any) -> int:
        # sendmsg(buffers[, ancdata[, flags[, address]]]): an unconnected datagram socket can send
        # to `address` without connect() or sendto().
        address = args[2] if len(args) >= 3 else kwargs.get("address")
        if address is not None:
            _check(self.family, address, "sendmsg")
        return real_sendmsg(self, buffers, *args, **kwargs)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not is_loopback(host):
            raise OutboundConnectionBlockedError(f"socket guard: name lookup of {host!r} (T-305)")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_lookup(name: str) -> Callable[..., Any]:
        real = getattr(socket, name)

        def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
            # getnameinfo takes a socket address tuple; the others take a host.
            target = host[0] if isinstance(host, tuple) and host else host
            if not is_loopback(target):
                raise OutboundConnectionBlockedError(f"socket guard: {name}({target!r}) (T-305)")
            return real(host, *args, **kwargs)

        return lookup

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket.socket, "sendto", sendto)
        mp.setattr(socket.socket, "sendmsg", sendmsg)
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        # Older resolver entry points that bypass getaddrinfo. Loopback stays allowed: the stdlib
        # HTTP server asks getfqdn("127.0.0.1") when it binds.
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
            mp.setattr(socket, name, guarded_lookup(name))
        yield

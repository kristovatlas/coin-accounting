"""The socket guard is already on while test modules are imported (ENGINEERING §3.2, T-305).

This module tries a name lookup at import time, as an import-time telemetry or update check in a
dependency would. If the guard were only a fixture, the lookup would reach the resolver.
"""

from __future__ import annotations

import socket

from tests.socket_guard import OutboundConnectionBlockedError

try:
    socket.getaddrinfo("example.invalid", 443)
    AT_IMPORT = "not blocked"
except OutboundConnectionBlockedError:
    AT_IMPORT = "blocked"
except OSError:
    AT_IMPORT = "reached the resolver"


def test_the_guard_is_on_while_test_modules_are_imported_t305() -> None:
    assert AT_IMPORT == "blocked"

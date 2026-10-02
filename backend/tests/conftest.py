"""Suite-wide fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests import socket_guard


@pytest.fixture(autouse=True, scope="session")
def _socket_guard() -> Iterator[None]:
    """Every test runs with the socket guard on (ENGINEERING §3.2, T-305)."""
    with socket_guard.installed():
        yield

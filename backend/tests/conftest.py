"""Suite-wide hooks.

The socket guard (ENGINEERING §3.2, T-305) is installed in `pytest_configure`, before collection,
so code that runs while test modules and the modules they import are being imported is guarded
too, and it stays on until `pytest_unconfigure`, after every fixture's teardown.
"""

from __future__ import annotations

from contextlib import ExitStack

import pytest

from tests import socket_guard

_guard = ExitStack()


# tryfirst/trylast: pluggy calls hooks newest-registered first, so without these the guard would
# go up after, and come down before, the built-in and `-p` plugins' own configure/unconfigure.
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    _guard.enter_context(socket_guard.installed())


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    _guard.close()

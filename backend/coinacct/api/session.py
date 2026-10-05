"""The launch token exchange and the session (architecture §4; THREAT_MODEL T-103, T-110).

The launcher creates a one-time bootstrap token T and hands it to the browser through a 0600 file.
The SPA sends T once to `POST /api/session` and gets the session token S, which lasts until the
backend exits. T is single-use and expires 60 s after start-up; a second claim is refused and logged.
There is exactly one session per process. Comparisons are constant-time.
"""

from __future__ import annotations

import enum
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Final

log = logging.getLogger(__name__)

BOOTSTRAP_TTL_SECONDS: Final = 60.0
ALREADY_CLAIMED: Final = "Session already claimed. Restart the app."


class ClaimError(enum.Enum):
    WRONG_TOKEN = "the launch token is not valid"  # noqa: S105 - a message, not a credential
    EXPIRED = "the launch link has expired. Restart the app."
    ALREADY_CLAIMED = ALREADY_CLAIMED


class Sessions:
    def __init__(
        self,
        bootstrap_token: str,
        *,
        ttl: float = BOOTSTRAP_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_claimed: Callable[[], None] = lambda: None,
    ) -> None:
        if not bootstrap_token:
            raise ValueError("the bootstrap token must not be empty")
        self._bootstrap = bootstrap_token
        self._clock = clock
        self._expires_at = clock() + ttl
        self._on_claimed = on_claimed
        self._session: str | None = None
        self._lock = threading.Lock()

    def claim(self, token: str) -> str | ClaimError:
        """Exchange the bootstrap token for the session token, once. Returns the session token or
        why the claim was refused."""
        with self._lock:
            if self._session is not None:
                log.warning("a second claim of the launch token was refused (T-110)")
                return ClaimError.ALREADY_CLAIMED
            if not hmac.compare_digest(token.encode(), self._bootstrap.encode()):
                log.warning("a claim with a wrong launch token was refused")
                return ClaimError.WRONG_TOKEN
            if self._clock() >= self._expires_at:
                log.warning("a claim of an expired launch token was refused (T-110)")
                return ClaimError.EXPIRED
            self._session = secrets.token_urlsafe(32)
            session = self._session
        log.info("session claimed")
        try:
            self._on_claimed()  # the launcher deletes the bootstrap file
        except Exception:
            log.exception("removing the bootstrap file after the claim failed")
        return session

    def is_session(self, token: str) -> bool:
        """True if `token` is the session token. Constant-time."""
        session = self._session
        return session is not None and hmac.compare_digest(token.encode(), session.encode())

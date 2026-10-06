"""The launch token exchange and the session (architecture §4; THREAT_MODEL T-103, T-110).

The launcher creates a one-time bootstrap token T and hands it to the browser through a 0600 file.
The SPA sends T once to `POST /api/session` and gets the session token S, which lasts until the
backend exits. T is single-use and expires 60 s after start-up; a second claim is refused and logged.
There is exactly one session per process. Comparisons are constant-time.

Refused claims are logged in two separately capped groups, `MAX_LOGGED_REFUSALS` each:
- **Wrong tokens** (before or after the claim): the route needs no authentication, so any local
  process could otherwise fill the log on the data volume (T-103).
- **The real token used again** after the claim, or after it expired (T-110: a sign it leaked).
  Only someone holding the token can cause these, so probes with wrong tokens can't use up their
  budget and hide them.
Past a cap, refusals are only counted (`refused`). The launcher is to delete the bootstrap file on
claim (`on_claimed`) and at `expires_at`.
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
MAX_LOGGED_REFUSALS: Final = 10
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
        self.refused = 0
        self._logged = {"wrong token": 0, "token reuse": 0}

    @property
    def expires_at(self) -> float:
        """When the bootstrap token stops working, on the clock given to the constructor."""
        return self._expires_at

    def _refuse(self, error: ClaimError, group: str, why: str) -> ClaimError:
        self.refused += 1
        self._logged[group] += 1
        if self._logged[group] <= MAX_LOGGED_REFUSALS:
            log.warning("%s", why)
        if self._logged[group] == MAX_LOGGED_REFUSALS:
            log.warning("further refused claims (%s) are counted but not logged", group)
        return error

    def claim(self, token: str) -> str | ClaimError:
        """Exchange the bootstrap token for the session token, once. Returns the session token or
        why the claim was refused."""
        with self._lock:
            # surrogatepass: any str compares (as a non-match) instead of raising.
            matches = hmac.compare_digest(token.encode("utf-8", "surrogatepass"), self._bootstrap.encode())
            if self._session is not None:
                if matches:
                    return self._refuse(
                        ClaimError.ALREADY_CLAIMED,
                        "token reuse",
                        "the launch token was used again after the session was claimed (T-110)",
                    )
                return self._refuse(
                    ClaimError.ALREADY_CLAIMED,
                    "wrong token",
                    "a claim was refused: the session is already claimed",
                )
            if not matches:
                return self._refuse(
                    ClaimError.WRONG_TOKEN, "wrong token", "a claim with a wrong launch token was refused"
                )
            if self._clock() >= self._expires_at:
                return self._refuse(
                    ClaimError.EXPIRED,
                    "token reuse",
                    "a claim of an expired launch token was refused (T-110)",
                )
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

---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0015: Local authentication: bootstrap file and bearer session

## Context and Problem Statement

The API is on `127.0.0.1`. Other local users, other web apps on `127.0.0.1`, and websites in the same browser must not be able to use it.

## Considered Options

1. A launch token in the URL, exchanged for a cookie, plus a CSRF header. Rejected for two reasons:
   - The token appears in the browser's argv, which other users can read.
   - Cookies are shared by every port on the host.
2. **A one-time token in a 0600 bootstrap file, exchanged for a bearer session in `sessionStorage`.**
3. A desktop shell with no network API. Deferred (ADR 0016).

## Decision Outcome

Option 2, as specified in architecture §4:
- The launcher writes the token (60 s, single use) into a mode-0600 file, and opens that **file** in the default browser. The argv holds only a path.
- The SPA exchanges the token for a session token, keeps it in `sessionStorage` (scoped to origin and port), and sends it as `Authorization: Bearer` on every request.
- There are no cookies, so CSRF needs no separate token.
- A Host allowlist stops DNS rebinding.
- A second claim is refused, and the page shows "Session already claimed."

### Consequences

- Good: other users, other localhost apps and cross-site requests can't obtain or use the session.
- Bad: reloading the tab keeps the session, but a new tab or window needs a relaunch. Browser extensions can still read the page (ADR 0016).

## References

- architecture §4; THREAT_MODEL T-101–T-103, T-110

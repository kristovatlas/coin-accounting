---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0002: Local web app (FastAPI backend + React SPA on loopback)

## Context and Problem Statement

The app needs a visual, interactive UTXO graph and must run fully locally for a single user.

## Considered Options

1. Local web app: a Python (FastAPI) backend on `127.0.0.1` plus a browser SPA (React + Cytoscape.js)
2. Desktop app with Tauri (Rust core + web UI)
3. Desktop app with Python + Qt

## Decision Outcome

Option 1, chosen by the user during planning: it is the fastest to build and has the best graph libraries.

### Consequences

- Good: mature graph tooling; one backend language; easy E2E tests with Playwright.
- Bad: the browser is a trust boundary. We need loopback hardening: a Host check; a one-time token delivered through a 0600 bootstrap file (never argv) and exchanged for a bearer session in `sessionStorage` (no cookies, because cookies are shared by every port on the host); and a strict CSP (architecture §4), and the browser can leak data to disk (THREAT_MODEL T-101–T-110, R-5).
- v1 opens the user's **default browser**. Two alternatives were considered and deferred to a future version by the user (2026-09-27): launching a specific browser with a profile on the volume, and packaging a desktop shell (Electron, Tauri, pywebview). They add complexity and supply-chain surface for residual risks that are accepted for now: disk leakage by the browser (R-5), and browser extensions reading the app's pages (R-7).

## References

- PLAN "Architecture"; architecture.md §1

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
- Bad: the browser is a trust boundary. We need loopback hardening: a Host check, a bootstrap-file launch with a bearer session (ADR 0015), and a strict CSP (THREAT_MODEL T-104), and the browser can leak data to disk (THREAT_MODEL T-101–T-110, R-5).
- v1 opens the user's **default browser** (ADR 0016).

## References

- PLAN "Architecture"; architecture.md §1

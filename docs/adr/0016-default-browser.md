---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0016: Use the user's default browser; no managed profile in v1

## Context and Problem Statement

A browser can keep app content on plain disk (cache, session restore, crash reports), and its extensions can read the app's pages.

## Considered Options

1. **The user's default browser**, with app-side controls only (`no-store`, no sensitive URLs).
2. Launch a specific browser with a profile on the volume. Rejected for v1: it needs per-OS browser detection, and Snap/Flatpak browsers cause problems.
3. A desktop shell (Electron, Tauri, pywebview). Deferred: it adds a large supply-chain surface and packaging work.

## Decision Outcome

Option 1 (user decision, 2026-09-27). The docs recommend a private window or a clean profile with extensions disabled.

### Consequences

- Good: no extra complexity or dependencies.
- Bad: accepted risks **R-5** (browser disk leakage) and **R-7** (extensions read the app's pages). To be revisited together, via a new ADR.

## References

- THREAT_MODEL T-105, T-111, R-5, R-7; architecture §6

---
status: accepted
date: 2026-10-06
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 3f735c07a764239cbb240f8df751e7cca0821e2412a04cb576b235bba196dc2c
---

# 0034: The launcher reads the built frontend at start-up

## Context and Problem Statement

The app serves the production frontend bundle under its CSP (ENGINEERING §3.1, THREAT_MODEL T-104, T-106). Something has to read `frontend/dist` from disk. Architecture §2 gives filesystem access only to `storage/`, the launcher (for "bootstrap file, hardening"), the tests and the E2E harness. `api/` has none.

`storage/` is the wrong home. Its rule is that every path it touches resolves under the verified data directory (architecture §6). The build is in the checkout, never on the volume. The launcher reading it isn't one of the purposes §2 lists, so the reviewers of PR #154 rightly flagged this as a contradiction of §2. The architecture check (`scripts/check_architecture.py`) checks only which module may use the filesystem, not why, so it doesn't catch this.

## Considered Options

1. **The launcher reads the build once at start-up,** and hands it to `api/` as an in-memory mapping.
2. **`storage/` reads it,** which breaks the rule that `storage/` stays under the data directory.
3. **`api/` reads it through a static-files route,** which gives the request-handling module filesystem access.
4. **Embed the bundle in the Python package at build time,** which adds a generated module and a build step for every frontend change.

## Decision Outcome

Chosen option: **1**, pending the owner's decision.

- **Read once, read-only, before serving:** `launcher.load_bundle` reads `frontend/dist` before the server starts. It never writes there.
  - Only regular files are read, opened with `O_NOFOLLOW`.
  - The bundle is bounded: at most 200 files and 20 MiB in total, counting the bytes actually read.
  - Any link, special file, unreadable directory or read error stops start-up, so a partial or foreign tree is never served.
- **No build:** the app serves its placeholder page.
- **What `api/` gets:** only the mapping. It serves `index.html` at `/` and the build's own files under `/assets/`. Every other path is a 404.
- **Architecture §2** lists this as a third launcher purpose, and §1 lists it among the launcher's steps. This ADR records the new SHA-256 (ADR 0014).

### Consequences

- Good: no request handler touches the filesystem, and `storage/` keeps its rule.
- Good: what is served is fixed at start-up, so a later change on disk can't alter a running app's pages.
- Bad: the launcher has one more filesystem use. The architecture check can't tell the purposes apart, so review is still what keeps the launcher to the listed ones.

## References

- Architecture §1, §2, §6; ADR 0014 (the architecture baseline)
- THREAT_MODEL T-104, T-106, T-110; ENGINEERING §3.1
- PR #154 round 1 (Opus security review)

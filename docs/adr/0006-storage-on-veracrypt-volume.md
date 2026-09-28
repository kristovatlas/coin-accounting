---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0006: Everything the app writes lives on the VeraCrypt volume

## Decision Outcome

- The user DB, chain cache, logs, exports and app config (including RPC credentials) all live under `<volume>/coinacct/`.
- The app adds no encryption layer of its own.
- The app refuses to start on a path that isn't on an encrypted volume unless explicitly overridden (T-401).
- SQLite side files and `TMPDIR` are on the volume; files are mode 0600.
- A dismount watchdog makes the backend exit when the volume disappears.

### Consequences

- Good: a single, well-understood at-rest protection; no plaintext spill by the app.
- Bad: VeraCrypt refuses a normal dismount while files are open, so the v1 workflow is to quit and then dismount (an in-app lock is deferred). The app opens the user's default browser and doesn't control what it stores, so the browser may keep app content on plain disk (accepted risk R-5; a dedicated profile or desktop shell is deferred).

## References

- THREAT_MODEL §5.4; architecture.md §4

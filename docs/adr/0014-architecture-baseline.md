---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 8e9555b8dd24e7758599292565b6f67349cee598200aca2c1af901b5b4c4625c
---

# 0014: Adopt the v1 architecture baseline

## Decision Outcome

Adopt `docs/architecture.md` v0.2.1 (components and trust boundaries, module import and capability rules, runtime model, local authentication, runtime flows F1–F3, data at rest, the data flow, chain-access sequences, build flows) as the binding v1 architecture.

- The CI check compares `sha256(docs/architecture.md)` with `architecture_sha256` above.
- Any later change to the architecture needs a new ADR with the new hash (ENGINEERING §4.2).
- If `docs/architecture.md` changes during review of PR #5, this hash is updated before merge.

## References

- docs/architecture.md; ENGINEERING §4.2; PLAN P0.3

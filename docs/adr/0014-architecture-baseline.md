---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: b569cd6f8362f85a1b4a141265a6760474e19df509802701a56d29076cd84ccd
---

# 0014: Adopt the v1 architecture baseline

## Decision Outcome

Adopt `docs/architecture.md` v0.1.1 (components and trust boundaries, module import rules, runtime flows F1–F3, data at rest, the data flow, chain-access sequences, build flows) as the binding v1 architecture.

- The CI check compares `sha256(docs/architecture.md)` with `architecture_sha256` above.
- Any later change to the architecture needs a new ADR with the new hash (ENGINEERING §4.2).
- If `docs/architecture.md` changes during review of PR #5, this hash is updated before merge.

## References

- docs/architecture.md; ENGINEERING §4.2; PLAN P0.3

---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 7004ac14b985a4279cbff0d60267398b7057479e4c519950fadb2133d1f9de25
---

# 0014: Adopt the v1 architecture baseline

## Decision Outcome

Adopt `docs/architecture.md` v0.2.1 as the binding v1 architecture:
- components and trust boundaries
- module import and capability rules
- the runtime model
- local authentication
- runtime flows F1–F3
- data at rest
- the data flow
- chain-access sequences
- build flows

**Hash workflow** (the CI check is to be built in M0):
- **Anchor:** the ADR on `main` with the highest number that has an `architecture_sha256` field. On `main`, `sha256(docs/architecture.md)` must equal the anchor's hash.
- **A PR that changes `docs/architecture.md`** must add exactly one new ADR with the new hash, and CI checks the PR's version of the file against it. When the human merges, that ADR becomes the anchor.
- **Any other PR** must leave the file's hash equal to the anchor's.
- `.gitattributes` fixes line endings (`eol=lf`), so a checkout can't change the hash.
- If `docs/architecture.md` changes before this PR (#6) merges, the hash above is updated first.

## References

- docs/architecture.md; ENGINEERING §4.2; ADR 0001

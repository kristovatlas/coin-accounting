---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0001: Record significant decisions as ADRs

## Context and Problem Statement

The project is privacy-critical and produces tax figures. Decisions have to stay traceable, and AI agents must be able to find the rules they are bound by.

## Decision Outcome

- Use [MADR](https://adr.github.io/madr/) files in `docs/adr/NNNN-kebab-title.md`, with the status in YAML front matter (`proposed` → `accepted` / `rejected`, later `deprecated` or `superseded by NNNN`).
- **Acceptance means the human merged the PR** containing the ADR.
- After acceptance, only the status line may change. A CI check enforces this. Changing a decision means writing a new ADR that supersedes the old one.
- When an ADR is required: ENGINEERING §4.1.

### Consequences

- Good: a durable history of why things are the way they are; one place for agents to look.
- Bad: some process overhead for every significant change.

## References

- ENGINEERING §4

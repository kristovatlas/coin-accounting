---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0001: Record significant decisions as ADRs

## Context and Problem Statement

The project is privacy-critical and produces tax figures. Decisions must stay traceable, and AI agents must be able to find the rules they are bound by.

## Decision Outcome

- Use [MADR](https://adr.github.io/madr/) files in `docs/adr/NNNN-kebab-title.md`, with YAML front matter (`status`, `date`, `deciders`, optional `supersedes`, optional `architecture_sha256`).
- **Status lifecycle:**
  - An ADR is written with `status: proposed`.
  - When the human has approved the PR, the **last commit before merge** sets `status: accepted` (or `rejected`).
  - **The human's merge is the acceptance.** Any ADR file on `main` counts as decided, whatever its status says.
  - Later, only the status line of a decided ADR may change, to `deprecated` or `superseded by NNNN`.
  - To change a decision, write a new ADR with `supersedes: NNNN`.
- **CI (to be built in M0)** rejects any change to an ADR on `main` other than its status line, and derives the index in `README.md` from the front matter.
- When an ADR is required: ENGINEERING §4.1.

### Consequences

- Good: a durable history of why things are the way they are; one place for agents to look.
- Bad: some process overhead for every significant change.

## References

- ENGINEERING §4; [ADR 0014](0014-architecture-baseline.md) (hash workflow); [ADR 0018](0018-repository-governance.md) (precedence)

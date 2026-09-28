---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0003: Supported platforms are Linux and macOS

## Decision Outcome

- v1 supports **Linux and macOS**; Windows is not planned.
- Avoid OS-specific mechanisms unless both platforms are covered.
- CI and E2E run on both.

### Consequences

- Good: covers the expected users; keeps the build simple.
- Bad: VeraCrypt detection needs a macOS method (designed in M0, with an explicit-confirmation fallback; T-401). OS-level egress sandboxing was rejected partly for portability reasons (ADR 0012).

## References

- THREAT_MODEL §10.1

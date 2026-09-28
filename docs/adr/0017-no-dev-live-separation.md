---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0017: No technical separation of development and real-data use

## Context and Problem Statement

An AI agent or a compromised dev tool can leave code in the working tree (venv, `node_modules`, the built bundle, git hooks). That code would run later when the app is started with real data.

## Considered Options

1. A separate clean checkout at a reviewed tag for real-data runs.
2. **No technical control:** warn users not to do dev work, or run AI agents, on a machine that holds real financial data.

## Decision Outcome

Option 2 (user decision, 2026-09-27). This weakens a proposed control, so it is recorded here.

### Consequences

- Good: a simpler workflow.
- Bad: accepted risk **R-6**. Users who develop on the same machine carry this risk.

## References

- THREAT_MODEL R-6, T-607; ENGINEERING §6

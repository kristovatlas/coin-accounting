---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0010: Doxx propagation rules and confidence levels

## Decision Outcome

A coin's doxx set is the set of entities with `knows_identity` that can link it to the user.

| Rule | Confidence |
|---|---|
| **Paid to K**: every owned input and owned output of that tx | certain |
| **Received from K**: from a confirmed event, not from address recognition | certain |
| **Address reuse**: every output ever paid to an address holding a K-doxxed coin | certain |
| **Forward**: owned outputs inherit the union of their owned inputs' sets | certain; only *inferred* through txs flagged `mixing`, whose inputs aren't linked to each other |
| **Backward cluster**: owned addresses co-spent with a doxxed address in earlier non-mixing txs | inferred |

- The UI says "known links" and never "private" or "clean"; this is a lower bound (R-3, T-505).
- Heuristics only suggest; they are never auto-applied.

## References

- PLAN §5; THREAT_MODEL T-504, T-505, T-511

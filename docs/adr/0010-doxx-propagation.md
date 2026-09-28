---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0010: Doxx propagation rules and confidence levels

## Context and Problem Statement

The user wants to know which coins each identity-knowing party (exchange, employer, KYC'd person) can link to them, so they can choose which coins to sell or spend where.

## Decision Outcome

A coin's **doxx set** is the set of entities with `knows_identity` that can link it to the user. A coin can be in several sets.

| Rule | Confidence |
|---|---|
| **Paid to K**: every owned input and owned output of that tx | certain. In a tx flagged `mixing`, the owned inputs are only *inferred*, because K can't tell which inputs are the user's |
| **Received from K**: from a **user-recorded** event (withdrawal, salary …), not from recognising addresses | certain |
| **Address reuse**: every output ever paid to an address that holds a K-doxxed coin | certain |
| **Forward**: owned outputs inherit the union of their owned inputs' sets | certain; only *inferred* through txs flagged `mixing`, whose inputs aren't linked to each other |
| **Backward cluster**: owned addresses co-spent with a doxxed address in earlier non-mixing txs | inferred |

- Doxx sets don't propagate into third-party outputs.
- Manual overrides persist across recomputation.
- Heuristics only suggest; they are never applied automatically.
- The UI says "known links", never "private" or "clean".

### Consequences

- Good: a useful, honest lower bound on what each party knows.
- Bad: chain-analysis firms use heuristics the app doesn't model. This is accepted risk R-3, and the UI says so (T-505).

## References

- PLAN §5; THREAT_MODEL T-504, T-505, T-511

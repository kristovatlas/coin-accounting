---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
---

# 0038: A migration that implements PLAN §2's data model needs no ADR of its own

## Context and Problem Statement

ENGINEERING §4.1 lists "storage format changes" among the changes that need an ADR. The user DB changes shape through numbered migrations (`backend/coinacct/storage/migrations/`). m0001 to m0009 went in without an ADR: in practice "storage format" was read as how and where the data is stored (SQLite on the VeraCrypt volume, ADR 0006), and a migration that adds a table or column PLAN §2 already describes was read as implementing that approved model.

PR #232's review panel read the rule the other way and asked for an ADR for its migration m0010. Which reading holds is an ADR-level question, so it went to the owner (#232). M5's price storage and settings, and M6's and M7's tables, all add migrations, so the answer is needed before they can land.

## Considered Options

1. **A migration that implements PLAN §2's data model needs no ADR of its own.** An ADR is still needed for what changes how or where data is stored, or what PLAN §2 doesn't describe.
2. **Every migration needs an ADR,** including retroactive ones for m0001 to m0009, perhaps one per milestone.

## Decision Outcome

Chosen option: 1, because PLAN §2 is the owner-approved data model, and an ADR per migration would restate it one table at a time without recording any decision that wasn't already made. It also matches how m0001 to m0009 were reviewed and merged.

"Storage format changes" in ENGINEERING §4.1 therefore means any of these, each of which still needs an ADR:

- where the user DB lives, its file format, or how it is protected at rest (ADR 0006, architecture §6)
- a table, column or kind of data that PLAN §2 doesn't describe
- a migration that deletes, or rewrites in place, data the user entered (the change log, events, tags, identifications, overrides), as opposed to recomputable data such as the chain cache
- a change to how chain data is obtained from the node (already named in §4.1)

A migration within PLAN §2's model still goes through the usual review: its PR names the PLAN §2 entry it implements, and its tests cover the upgrade from the previous version.

### Consequences

- Good: M5 to M7 can add their tables without an ADR each; the ADR log stays a record of decisions.
- Good: the cases that matter (where data lives, data outside the plan, destroying user data) still need the owner's decision.
- Bad: whether a table is "described by PLAN §2" is a judgment call. A reviewer who thinks it isn't raises it as an ADR-level question, as on #232.

## References

- ENGINEERING §4.1; PLAN §2; ADR 0006; architecture §6
- #232 (m0010, the change log's origin column) and its triage

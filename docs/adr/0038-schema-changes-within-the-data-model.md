---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
---

# 0038: A migration that implements the approved design needs no ADR of its own

## Context and Problem Statement

ENGINEERING §4.1 lists "storage format changes" among the changes that need an ADR. The user DB changes shape through numbered migrations (`backend/coinacct/storage/migrations/`). m0001 to m0009 went in without an ADR. In practice "storage format" was read as how and where the data is stored (SQLite on the VeraCrypt volume, ADR 0006), and a migration that implements the approved design (PLAN and the architecture) as implementing that design. Some of those migrations add tables and columns PLAN §2 never names, because they support kinds of data it does describe: `chain_state`'s target columns (m0002), `scan_marker` and `coverage.candidates` (m0003, architecture §8.2), `review_queue` (m0004, §8.4), and `descriptor_script` and `descriptor_client` (m0005).

PR #232's review panel read the rule the other way and asked for an ADR for its migration m0010, which adds an `origin` column to the change log. Which reading holds is an ADR-level question, so it went to the owner (#232). M5's price storage and settings, and M6's and M7's tables, all add migrations, so the answer is needed before they can land.

## Considered Options

1. **A migration that implements the approved design needs no ADR of its own.** An ADR is still needed for the changes listed below.
2. **Every migration needs an ADR,** including retroactive ones for m0001 to m0009, perhaps one per milestone.

## Decision Outcome

Chosen option: 1, because PLAN and the architecture are the owner-approved design. An ADR per migration would restate them one table at a time, without recording any decision that wasn't already made. It also matches how m0001 to m0009 were reviewed and merged.

**Within the design** (no ADR of its own): a migration that adds a table, column, index, constraint or trigger implementing a kind of data that PLAN (§1–§2) or the architecture describes. Supporting tables and columns count, not only those PLAN §2 names. m0002 to m0005, and #232's m0010, are examples.

**Still a "storage format change"** under ENGINEERING §4.1 (needs an ADR):

- where the user DB lives, its file format, or how it is protected at rest (ADR 0006, architecture §6), including storing credentials or other secrets in the user DB
- a kind of data that neither PLAN nor the architecture describes, and **a change to PLAN §2's data model itself** (a new kind of data, or one removed). PLAN.md can merge under autopilot (ADR 0031), so without this a PR could add a kind of data to PLAN and its migration together and skip the owner's decision. The architecture is already pinned by its hash (ADR 0014).
- a migration that deletes or alters data that can't be recomputed from the node or fetched again. That covers everything the user entered: entities, tax accounts, wallet clients, addresses and descriptors with their links, events, lots, identifications, tags, overrides and flags, settings, imported prices and the change log. It also covers the recorded chain in `chain_state`, which the storage policy relies on (T-206, T-401), even though PLAN §2 lists `chain_state` with the cache. A rebuild that keeps every value, such as SQLite's create-copy-drop-rename procedure for a change `ALTER TABLE` can't make, is not an alteration.
- a migration that drops, replaces or loosens a trigger, CHECK or other schema rule that a THREAT_MODEL mitigation relies on, such as the change log's append-only triggers (T-408) or the recorded chain's triggers (T-206). This is also "weakening a control" under §4.1. A migration that recreates such a rule unchanged, for example in a rebuild, needs no ADR, but its PR names the threat and runs that threat's tests against the migrated DB.
- a change to how chain data is obtained from the node (already named in §4.1)

A migration within the design still goes through the usual review. Its PR names the part of PLAN or the architecture it implements, and that text must already be on `main` before the PR: a PR can't make its own migration "described" by editing PLAN in the same change. It also names the threats it touches. Its tests cover the upgrade from the previous `user_version` on a DB that holds data, not only on an empty one.

ENGINEERING §4.1's "storage format changes" bullet now says this in short and points here (ENGINEERING 0.2.35, in this PR).

### Consequences

- Good: M5 to M7 can add their tables without an ADR each, and the ADR log stays a record of decisions.
- Good: the cases that matter still need the owner's decision: where data lives, data outside the design, losing user or security-relevant data, and loosening a schema-level control.
- Bad: whether a table implements a kind of data the design describes is a judgment call. A reviewer who thinks it doesn't raises it as an ADR-level question, as on #232.

## References

- ENGINEERING §4.1; PLAN §1–§2; architecture §6 and §8; ADR 0006
- THREAT_MODEL T-206, T-401, T-408
- SQLite, "Making Other Kinds Of Table Schema Changes": https://www.sqlite.org/lang_altertable.html
- #232 (m0010, the change log's origin column) and its triage

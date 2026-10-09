---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
---

# 0038: A migration that implements PLAN §2's data model needs no ADR of its own

## Context and Problem Statement

ENGINEERING §4.1 lists "storage format changes" among the changes that need an ADR. The user DB changes shape through numbered migrations (`backend/coinacct/storage/migrations/`). m0001 to m0009 went in without an ADR. In practice "storage format" was read as how and where the data is stored (SQLite on the VeraCrypt volume, ADR 0006), and a migration that implements the data model PLAN §2 describes as implementing that model. Some of those migrations add tables and columns PLAN §2 never names, because they support kinds of data it does describe: `chain_state`'s target columns (m0002), `scan_marker` and `coverage.candidates` (m0003, architecture §8.2), `review_queue` (m0004, §8.4), and `descriptor_script` and `descriptor_client` (m0005).

PR #232's review panel read the rule the other way and asked for an ADR for its migration m0010, which adds an `origin` column to the change log. Which reading holds is an ADR-level question, so it went to the owner (#232). M5's price storage and settings, and M6's and M7's tables, all add migrations, so the answer is needed before they can land.

## Considered Options

1. **A migration that implements PLAN §2's data model needs no ADR of its own.** An ADR is still needed for the changes listed below.
2. **Every migration needs an ADR,** including retroactive ones for m0001 to m0009, perhaps one per milestone.

## Decision Outcome

Chosen option: 1 (the owner's decision on #232), because PLAN §2 is the owner-approved data model. An ADR per migration would restate it one table at a time, without recording any decision that wasn't already made. It also matches how m0001 to m0009 were reviewed and merged.

**Within the data model** (no ADR of its own): a migration that adds a table, column, index, constraint or trigger implementing a kind of stored data that PLAN §2 describes, or that the architecture describes (its text is pinned by its hash, so it changes only with an ADR, ADR 0014). Supporting tables and columns count, not only those PLAN §2 names. Text elsewhere in PLAN is not a basis. m0002 to m0005, and #232's m0010, are examples.

**Still a "storage format change"** under ENGINEERING §4.1 (needs an ADR):

- where the user DB lives, its file format, or how it is protected at rest (ADR 0006, architecture §6), including storing credentials or other secrets in the user DB
- a kind of stored data that neither PLAN §2 nor the architecture describes, and **any change to PLAN that adds, removes or redefines a kind of stored data, in whatever section**. PLAN.md can merge under autopilot (ADR 0031), so without this an agent could describe new stored data in PLAN in one PR and add its migration in the next, and skip the owner's decision. The architecture is already pinned by its hash (ADR 0014).
- a migration that deletes or alters data that can't be recomputed from the node or fetched again. The property decides; these are examples, not a complete list. It covers everything the user entered: entities, tax accounts, wallet clients, addresses and descriptors with their links, events and the values entered with them (gift, inheritance and opening-allocation inputs), identifications, tags, overrides and flags, settings, imported prices and the change log. Derived data the engine recomputes from those, such as lot fragments, is not covered. It also covers app state that records what the node can no longer tell: the recorded chain in `chain_state`, which the storage policy relies on (T-206, T-401), even though PLAN §2 lists `chain_state` with the cache; `review_queue`, the txids a reorg removed from the cache (T-207, T-506); and `scan_marker`, the record of a node scan the app may have left running (T-212). A rebuild that keeps every value, such as SQLite's create-copy-drop-rename procedure for a change `ALTER TABLE` can't make, is not an alteration.
- a migration that drops, replaces or changes a trigger, CHECK or other schema rule that a THREAT_MODEL mitigation relies on, such as the change log's append-only triggers (T-408) or the recorded chain's triggers (T-206), **so that the DB as a whole accepts something it refused before**. This is also "weakening a control" under §4.1. A change after which the rules together refuse at least as much needs no ADR: recreating a rule unchanged in a rebuild, or m0009, which replaced one T-408 trigger with a narrower one alongside m0008's new trigger and an upgrade check, so the change log refuses more than before. Its PR shows that, names the threat, and runs that threat's tests against the migrated DB.
- a change to how chain data is obtained from the node (already named in §4.1)

A migration within the data model still goes through the usual review. Its PR names the PLAN §2 entry or architecture section it implements, and that text must already be on `main` before the PR: a PR can't make its own migration "described" by editing PLAN in the same change. It also names the threats it touches. Its tests cover the upgrade from the previous `user_version` on a DB that holds data, not only on an empty one.

ENGINEERING §4.1's "storage format changes" bullet now says this in short and points here (ENGINEERING 0.2.35, in this PR).

### Consequences

- Good: M5 to M7 can add their tables without an ADR each, and the ADR log stays a record of decisions.
- Good: the cases that matter still need the owner's decision: where data lives, data outside the data model, losing user or security-relevant data, and loosening a schema-level control.
- Bad: whether a table implements a kind of data PLAN §2 describes is a judgment call. A reviewer who thinks it doesn't raises it as an ADR-level question, as on #232.

## References

- ENGINEERING §4.1; PLAN §1–§2; architecture §6 and §8; ADR 0006
- THREAT_MODEL T-206, T-401, T-408
- SQLite, "Making Other Kinds Of Table Schema Changes": https://www.sqlite.org/lang_altertable.html
- #232 (m0010, the change log's origin column) and its triage

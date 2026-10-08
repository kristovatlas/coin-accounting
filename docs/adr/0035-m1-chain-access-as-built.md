---
status: proposed
date: 2026-10-08
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 53ff1442780a776a01c772d4f3dd3c97d5678c37b19bf76c031610eaf049c907
---

# 0035: The architecture text follows M1's chain access as built

## Context and Problem Statement

M1 implemented chain access (PRs #159 to #179). The review panels on those PRs, and on the M1 closing PR #181, found places where the architecture text and the code differ. Code that contradicts `docs/architecture.md` is a bug (AGENTS.md), so each difference is either a code change or an architecture change, and an architecture change needs an ADR (ADR 0014).

The differences that are design choices the reviews kept, not defects:

1. **§8.2, a range that keeps failing its guard.** The text says "retry (max 3, then fail the job)". The code retries a range 3 times, then leaves that subject for the next sync, which the tip poller queues again. Failing the job would leave the same gap, and nothing would retry it until the next tip.
2. **§8.2, a busy script over its budget.** The text says "pause job, ask the user". The code leaves the subject unfinished, the rest of the sync goes on, and the subject waits for the next tip rather than the next poll. Asking the user needs the M2 UI.
3. **§8.2, the stop height** comes from the scan target the catch-up recorded, not from a fresh `getblockcount`, so every subject in a sync scans to the same tip.
4. **§8.2, the cancel check.** A cancelled job starts no further range: the check runs after the in-flight marker is set and right before `scanblocks`, so either the job sees the cancel or shutdown sees the marker and aborts (T-212).
5. **§8.4, two tips.** The catch-up records the new tip as a **scan target**. The **last-seen tip** moves to it only once every subject has reached it, so a crash or a failed sync resumes from the target. The walk back starts from the reference tip (the unfinished target, otherwise the last-seen tip).
6. **§8.4, the review queue.** The txids a reorg removed are kept in the user DB, in the same transaction as the invalidation, until their events have been flagged (T-207, T-506).
7. **§3 step 3.** A job that hasn't stopped within its short join keeps the DB open. Closing a connection a thread may still be writing through isn't safe, and WAL rolls the open transaction back on the next open.
8. **§2 module tree.** `chain/txs.py` exists, and the chain cache's tables live in `storage/chain_cache.py` and `storage/chain_state.py`, since only `storage/` may open the DB.

## Decision Outcome

The architecture text follows the code for these eight points (architecture 0.2.6). The new SHA-256 is recorded above (ADR 0014).

Not changed, because the code is behind the text and must catch up:
- §3's writer lock for API writes (needed before M2's API writes; #180)
- §3's job progress (`GET /api/jobs/<id>`; with the M2 UI)

### Consequences

- Good: the binding text describes what runs, and the scan protocol's failure handling is stated once.
- Good: no control is relaxed. A waiting subject keeps the catch-up unfinished, so the last-seen tip never moves past a gap.
- Bad: a subject that keeps failing is retried on every poll, with no backoff. That's tracked in THREAT_MODEL T-212's Pending list.

## References

- Architecture §2, §3, §8.2, §8.4; ADR 0014; THREAT_MODEL T-207, T-210, T-212, T-506
- PRs #171, #177, #179, #181; issues #178, #180

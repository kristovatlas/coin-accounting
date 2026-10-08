---
status: proposed
date: 2026-10-08
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 53010c5c27e48341d3d40932e704647c428c29976e5435ece42bfbbd9d67b99b
---

# 0036: The architecture text follows M2's import and discovery as built

## Context and Problem Statement

M2 implemented the user DB's accounts, import, discovery and the history views (PRs #183 to #199). Comparing `docs/architecture.md` 0.2.6 with the code found places where the text and the code differ. Code that contradicts the architecture is a bug (AGENTS.md), so each difference is either a code change or an architecture change, and an architecture change needs an ADR (ADR 0014).

The differences that are design choices the reviews kept, not defects:

1. **§3, node calls in a request.** The text says long work is never run in a request. A descriptor's preview and import call `getdescriptorinfo` and `deriveaddresses` in the request, on one of the framework's request worker threads (§3 now lists them): at most 1,000 indexes, never `scanblocks`, and each call has a 120 s per-operation timeout (a stalled or trickling node can hold a request longer, and an import repeats the preview's calls). They run through a node client of the API's own, so the job worker's client and the shutdown abort client are never held up by them. Putting a preview on the job queue would make the user wait behind a scan for an answer that takes well under a second.
2. **§3, three node clients.** These are the job worker's, the abort client and the import client. Each allows at most 4 calls at once, so 12 in all, below Core's default `rpcthreads` of 16 (PLAN §1).
3. **§3, same-tip syncs.** An import, or a descriptor window that grew, asks the tip poller for a sync even though the tip hasn't moved. The next poll queues it. Otherwise new subjects would wait for the next block.
4. **§3, the writer lock and readers.** A write that waits more than 5 s for the lock is refused as busy, and the API answers 503, so a request never waits on a long job write. Readers are opened per request, read-only and query-only, after the writer's file checks (T-401, T-402).
5. **§8.2, scan subjects.**
   - Each descriptor is one subject. An address a descriptor already derives gets no subject of its own.
   - Addresses are grouped into at most 16 `raw()` subjects per start height, so a list of many addresses doesn't become many scans (PLAN §3, "as few scans as possible").
   - A subject's name is a digest of what it scans, so any change makes a new subject, scanned from its start. That is correct whatever the earlier coverage; the cost is a rescan.
6. **§8.2 and §8.4, window growth.**
   - After each sync, a ranged descriptor whose own scan finished, and whose highest confirmed used index is near its window's end, grows (doubling, at most 1,000 indexes at once, never past index 10,000 by itself), and is scanned at the next poll.
   - Only confirmed use counts. Anyone who knows the xpub can pay its addresses, and unconfirmed use would let them grow the window and force rescans for free (T-205).
   - The 10,000 cap is provisional until the M1 perf check sizes it (PLAN §1).
7. **§6, uploads.** The text lists upload spooling under temp files. Uploads are request bodies of at most 64 KiB, held in memory and never written to disk.
8. **§2 and §7, module names.**
   - `chain/descriptors.py`, `storage/accounts.py` and `storage/datadir.py` exist, and `storage/models.py` doesn't.
   - The import, discovery and history services live in `services/`.
   - §7's diagram now names discovery's two halves (subjects and growth in `services/`, the scan in `chain/`) and the history views.

## Decision Outcome

The architecture text follows the code for these eight points (architecture 0.2.7, amended in the M2 closing PR). The new SHA-256 is recorded above (ADR 0014). The owner accepts this ADR before that PR merges. If the owner rejects it, the code changes to follow the old text instead, and the text change is withdrawn.

Not changed, because the code is behind the text and must catch up:
- §3's job progress (`GET /api/jobs/<id>`), as in ADR 0035. The history view shows whether a sync is catching up and how far each address is scanned, but not a job's progress.

### Consequences

- Good: the binding text describes what runs, including the two places where untrusted input can make the app do more work: an upload, and a third party paying the user's addresses.
- Good: no security control is relaxed. §3's "long work never in a request" rule gains one exception, for an import's node calls, which the owner accepts with this ADR.
  - A request's node calls are bounded and read-only, and go through the same `rpcwhitelist` and the app's own allowlist (T-203).
  - The 503 fails closed.
- Bad: a descriptor preview holds a request thread while the node is slow, for at least the per-call timeout and longer if the node trickles its reply; a fifth concurrent import waits for a free slot. The API refuses the import cleanly rather than hanging the jobs, but the user waits.
- Bad: each subject rename rescans the subject's whole history, and its scripts show "not scanned yet" until the rescan finishes (#188, #198).
- Bad: confirmed use by a third party can still grow a window up to the cap. Anyone who knows the xpub can pay addresses at several successive window edges in one transaction, and each rescan then reveals the next, so one fee can drive several growth steps (T-205; #190).
- Bad: a grown window's new scripts read "not scanned yet" from the moment it grows until the next poll scans them, after the sync has already recorded its tip as done (T-210 shows it; architecture §8.4).

## References

- Architecture §2, §3, §6, §7, §8.2, §8.4; ADR 0014, ADR 0035; THREAT_MODEL T-203, T-205, T-207, T-210, T-401, T-402
- PRs #183 to #199; issues #188, #190, #194, #198

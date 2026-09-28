---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0005: Chain data from Core's own indexes; no app-side chain index

## Context and Problem Statement

The app needs address/script history, xpub discovery, and both backward and forward traversal of coins. Core has no address index. It only gained a spender index in 31.0.

## Considered Options

1. Build our own full-chain index (LMDB/RocksDB, 150–350 GB, possibly a Rust parser)
2. A watch-only node wallet (rejected: it stores the user's addresses in the node datadir, outside the volume)
3. **Core's indexes:** `txindex`, `blockfilterindex` with `scanblocks` + `getdescriptoractivity`, and `txospenderindex` with `gettxspendingprevout`

## Decision Outcome

Option 3, chosen by the user.
- Backward expansion: `getrawtransaction … 2 <blockhash>`.
- Forward expansion: `gettxspendingprevout`.
- History and discovery: the **scan protocol** in PLAN §1. It uses bounded ranges, stops below the filter index height and below tip − 100, re-checks the index height and stop hash after each range, and reads the newest blocks only via `getdescriptoractivity`. This guards against `scanblocks` silently skipping filter ranges it can't read.
- Results are cached in the user DB with coverage, snapshots and fork-point reorg handling.

### Consequences

- Good: no large index to build or secure; no Rust; nothing on plain disk; forward expansion is a single call.
- Bad: we depend on Core's index correctness (trusted, THREAT_MODEL §8). Filter-index read errors remain a silent-skip residual (T-210), to be reported upstream. Discovery can be slow for busy scripts (budgets, T-205).

## References

- PLAN §1; THREAT_MODEL T-205–T-212; BIP158; PR #4 reviews

---
status: accepted
date: 2026-10-02
deciders: repository owner (human), drafted by Claude Code
architecture_sha256: 366c443a738f2c0d7f97559c2c09e69b393b81d079f0d20e529311bb574ecc4e
---

# 0029: Minimum Bitcoin Core version 31.1

## Context and Problem Statement

ADR 0004 sets the minimum at **Bitcoin Core 31.0**, the first release with `txospenderindex`. ENGINEERING §3.1 says regtest runs at both the minimum supported and the latest Core version. Only 31.1 is pinned (`scripts/toolchain.lock`), so 31.0 has never been tested (#109).

## Considered Options

1. **Pin 31.0 as well,** and run the integration tests against both versions.
2. **Raise the minimum to 31.1,** the version that is pinned and tested.
3. **Keep 31.0 untested,** and accept the gap.

## Decision Outcome

Chosen option: **2** (owner's decision, 2026-10-02). The minimum supported Bitcoin Core version is **31.1**. This replaces the "Bitcoin Core ≥ 31.0" line of ADR 0004; everything else in ADR 0004 stands.

- The startup node check refuses anything older than 31.1 (`MIN_VERSION = 310100`, which is how `getnetworkinfo` reports 31.1.0).
- Architecture §1 and §8.1, PLAN §1 and the DEPENDENCIES `bitcoind` row say 31.1.
- When a newer Core is pinned, the minimum and the latest differ again. ENGINEERING §3.1 then needs both runs, or a new ADR raising the minimum.

### Consequences

- Good: every supported version is tested, and nothing new needs to be pinned.
- Bad: users on 31.0 must upgrade to 31.1, a point release.

## References

- ADR 0004 (node access), ENGINEERING §3.1, #109
- Architecture §1 and §8.1 (this ADR records its new hash, ADR 0014)

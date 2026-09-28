---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0013: Supply-chain policy

## Decision Outcome

As detailed in ENGINEERING §2:
- pnpm 12 and uv with **7-day cooldowns**; lockfiles with hashes; frozen/locked installs.
- **No build or lifecycle scripts:** pnpm `allowBuilds: {}` and `strictDepBuilds`; none of our own lifecycle scripts; uv wheels-only with no exceptions and `package = false`.
- **No auto-installs or auto-downloads** (`verifyDepsBeforeRun: error`, `pmOnFail: error`, `UV_NO_SYNC`, `python-downloads = "never"`); fetch-and-run tools banned.
- **Every install goes through Socket Firewall via `make` targets, with no fallback.** The `sfw` binary is pinned with a trust-on-first-use hash.
- **Resolve → vet → human approval → install** for every new or bumped dependency.
- A lockfile policy check (registry-only sources, hashes, ≥7-day age on every entry); audits on every PR; a weekly clean-cache rescan.
- Verified toolchain and test downloads; hardened GitHub Actions; no CI secrets.

### Consequences

- Good: a strong defence for the primary exfiltration path (ADR 0012).
- Bad: slower dependency updates; security fixes need recorded, version-specific exceptions.

## References

- ENGINEERING §2; THREAT_MODEL T-601–T-604

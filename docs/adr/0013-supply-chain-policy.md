---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0013: Supply-chain policy

## Context and Problem Statement

In-process egress is an accepted risk (ADR 0012), so malicious dependencies are the main exfiltration path.

## Considered Options

1. Default package-manager behaviour with Dependabot alerts.
2. **A strict policy:** cooldowns, no install scripts, Socket Firewall on every install, vet-before-install, a lockfile policy check.
3. Vendoring all dependencies.

## Decision Outcome

Option 2. **ENGINEERING §2 (v0.2.1) is authoritative** for the concrete settings. This ADR records the principles:
- 7-day cooldowns in all ecosystems (pnpm ≥ 12, uv); hashed lockfiles; frozen/locked installs.
- No build or lifecycle scripts; wheels only.
- No auto-installs, auto-downloads, or fetch-and-run tools.
- **Every install goes through Socket Firewall via `make` targets, with no fallback.**
- **Resolve → vet → human approval → install** for every new or bumped dependency.
- A lockfile policy check, audits on every PR, and a weekly clean-cache rescan.
- Verified toolchain and test downloads; hardened GitHub Actions; no CI secrets.

### Consequences

- Good: a strong defence for the main exfiltration path.
- Bad:
  - slower dependency updates, and security fixes need recorded, version-specific exceptions
  - `sfw` itself is trusted on first use: Socket publishes no checksums
  - `sfw` sends the names and versions of installed packages to Socket (a build-time flow, THREAT_MODEL §6)

## References

- ENGINEERING §2; THREAT_MODEL T-601–T-604

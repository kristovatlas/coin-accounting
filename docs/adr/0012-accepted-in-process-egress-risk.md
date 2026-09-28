---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0012: Accept in-process egress risk (no OS-level network sandbox)

## Context and Problem Statement

Malicious code inside the backend process, such as a compromised dependency, could send user data to the internet. OS-level controls could block this, but they differ between Linux and macOS.

## Considered Options

1. OS sandbox (bwrap/nftables on Linux, sandbox-exec/pf on macOS) plus a separate price-fetch process without DB access
2. OS sandbox only
3. **Accept the risk**, and rely on supply-chain controls plus a test-time socket guard

## Decision Outcome

Option 3, chosen by the user for cross-platform simplicity. Supply-chain controls (ADR 0013) are the primary defence. The socket guard only catches accidental egress.

### Consequences

- Good: simpler, portable implementation.
- Bad: a successful supply-chain compromise could exfiltrate data while the volume is mounted (THREAT_MODEL R-4). To be revisited if the dependency count grows, or a portable sandbox becomes practical.

## References

- THREAT_MODEL R-4, T-305

---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0019: Import addresses and public descriptors only

## Decision Outcome

- v1 imports address lists and **public** descriptors/xpubs (moved into v1 by user decision). Addresses are derived with Core's `deriveaddresses`, and there is no node wallet.
- Descriptors are passed to Core as `{desc, range}` objects, and addresses are keyed by scripthash.
- **Any descriptor or key with private material (xprv, WIF) is rejected at import.** It is never stored, sent to the node, or logged. The user is shown how to export the public form (T-703).
- Imports arrive only by upload, with size limits and validation (TB6, T-701).

## References

- PLAN §1, §3; THREAT_MODEL T-701, T-703

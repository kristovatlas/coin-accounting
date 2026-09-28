---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0004: Node access via loopback JSON-RPC with a whitelisted rpcauth user

## Context and Problem Statement

All chain data must come from the user's own Bitcoin Core node, and the app must not be able to change node state or leak queries.

## Decision Outcome

- **Loopback only**, with no override. A remote node is reached through a user-managed SSH tunnel that terminates on loopback (THREAT_MODEL T-202).
- A dedicated **`rpcauth` user** restricted by Core's **`rpcwhitelist`** to read-only methods, plus a client-side allowlist. A startup **canary** (`uptime` must be refused) proves the whitelist is active. The docs recommend a generic username, because Core logs refused calls to `debug.log` (T-209).
- JSON-RPC only: no REST interface, no node wallet, no cookie-file auth.
- Node requirements: **Bitcoin Core ≥ 31.0**, unpruned, with `txindex`, `blockfilterindex` and `txospenderindex` synced to the tip. The user accepts any minimum version.

### Consequences

- Good: node-side enforcement even against code inside the app; no plaintext RPC over a network.
- Bad: the user must configure `rpcauth`/`rpcwhitelist` and three indexes (extra disk). The canary leaves a usage trace in `debug.log`.

## References

- PLAN §1; THREAT_MODEL T-201–T-204, T-209

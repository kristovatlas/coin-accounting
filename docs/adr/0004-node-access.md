---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0004: Node access via loopback JSON-RPC with a whitelisted rpcauth user

## Context and Problem Statement

All chain data must come from the user's own Bitcoin Core node. The app must not be able to change wallet or chain state, or leak queries.

## Decision Outcome

- **Loopback only**, with no override. A remote node is reached through a user-managed SSH tunnel that terminates on loopback (THREAT_MODEL T-202).
- A dedicated **`rpcauth` user** restricted by Core's **`rpcwhitelist`** to the method list in THREAT_MODEL **T-203**. The app also keeps its own client-side allowlist. **Changing that list is a security change and needs an ADR.**
- The whitelist allows **no wallet, broadcast or other chain-state RPCs**. The one intended exception is **`scanblocks`**: Core can't restrict its actions by argument, so this user can also `start` and `abort` the node's single, global scan slot. The app aborts only a scan it started itself (architecture §8.1, T-212).
- A startup **canary** (`uptime` must be refused) confirms that the whitelist is active. The docs recommend a generic RPC username, because Core logs refused calls to `debug.log` (T-209).
- **If any node check fails**, including a canary call that succeeds, the app **disables all chain RPC and starts in offline mode** (architecture §3).
- JSON-RPC only: no REST interface, no node wallet, no cookie-file auth.
- Node requirements:
  - **Bitcoin Core ≥ 31.0**, the first release with `txospenderindex`. The user accepted any minimum version, so the newest needed RPC sets it.
  - Unpruned.
  - `txindex`, `blockfilterindex` and `txospenderindex` synced to the tip.

### Consequences

- Good: the node enforces the rules even against code inside the app, and no plaintext RPC crosses a network.
- Bad:
  - The user must configure `rpcauth`/`rpcwhitelist` and three indexes, which cost extra disk.
  - The canary leaves a usage trace in `debug.log`.
  - The app can abort a scan it started.

## References

- PLAN §1; THREAT_MODEL T-201–T-204, T-209, T-212; architecture §3, §8.1

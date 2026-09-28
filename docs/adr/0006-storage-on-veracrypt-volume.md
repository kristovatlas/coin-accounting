---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0006: The app stores its data only on the VeraCrypt volume

## Decision Outcome

- The user DB (including the chain cache and settings), `config.toml` (RPC credentials), logs, exports and temp files live under the data directory `<data>` on the volume.
- **The one exception** is the short-lived bootstrap file (ADR 0015). On Linux it goes in `$XDG_RUNTIME_DIR`, which is RAM-backed. It is deleted within 60 s.
- `<data>` is passed with `--data-dir` or `COINACCT_DATA_DIR` each time. It is **never saved on plain disk**, because the path would reveal the volume. Any per-path confirmation (T-401, macOS) is stored inside `<data>`.
- On mainnet, the app refuses to start when `<data>` isn't on an encrypted volume. `--allow-unencrypted-storage` is accepted only on regtest, signet and testnet, for development and CI (T-401).
- `storage/` refuses any path outside `<data>`. The only other destination for an export is a browser download that the user confirms (T-108).
- The app adds no encryption of its own. SQLite side files and `TMPDIR` are on the volume, files are mode 0600, and a watchdog makes the backend exit when the volume disappears.

### Consequences

- Good: one well-understood at-rest protection; the app spills nothing to plain disk.
- Bad:
  - VeraCrypt refuses a normal dismount while files are open, so the v1 workflow is to quit and then dismount (an in-app lock is deferred).
  - The browser is not under the app's control (ADR 0016).

## References

- THREAT_MODEL §5.4; architecture §6

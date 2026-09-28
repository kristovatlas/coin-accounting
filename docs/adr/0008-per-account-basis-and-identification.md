---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0008: Per-account basis, timely identification, and the 2025 transition

## Decision Outcome

- Basis is tracked per **tax account** (a self-custody wallet or custodial exchange account). Disposals draw only from lots in the same account (Rev. Proc. 2024-28).
- **Specific identification counts only if recorded no later than the sale.** `identified_at` is stored. A late pick is flagged, and the account's standing order (what the broker has on file) or FIFO applies instead. From 2027 the UI warns that identification must go to the broker. Notice 2025-7 relief was extended through 12/31/2026 by Notice 2026-20.
- For on-chain disposals, the spent UTXO is itself the identification; fragments within it follow the account's standing method.
- `opening_allocation_2025` events record the user's allocation of unused pre-2025 basis to each account.
- Unknown basis, unconfirmed txs and late identifications block report generation until explicitly resolved.

### Consequences

- Good: matches current IRS rules and 1099-DA reporting; auditable.
- Bad: the user must record identifications in time, and supply the 2025 allocation.

## References

- PLAN §7; THREAT_MODEL T-508, T-509; Treas. Reg. §1.1012-1(j); Notices 2025-7 and 2026-20

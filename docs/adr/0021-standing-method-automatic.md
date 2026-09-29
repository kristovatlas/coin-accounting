---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
amends: 0008
---

# 0021: Accounts that use their standing method need no lot picking

_Written in Simplified Technical English (ASD-STE100 style). This ADR amends decisions 3 and 4 of [ADR 0008](0008-per-account-basis-and-identification.md); the rest of ADR 0008 stays in force._

## Context and Problem Statement

Some users do not want to choose lots. They want the method the IRS applies by default: FIFO in each account. ADR 0008 warns about every lot selection made after a sale. That warning is not useful when the selection gives the same result as the account's standing method.

## Decision Outcome

User decision, 2026-09-28:

1. **Automatic mode.** Each account has a setting: "Use the standing method automatically". When it is on:
   - The app selects the lots with the account's standing method (FIFO by default) for every sale, spend and withdrawal.
   - The app does not show the lot picker. It shows which lots it used.
   - These selections are never marked `late`, because the standing method was recorded before the sale.
2. **The late warning applies only when the result differs.** The app warns about a selection made after a sale or withdrawal **only if** its lots differ from the lots that the account's standing method (or FIFO, if there is none) gives. A late selection that gives the same lots is recorded as made, with no warning.
3. For self-custody wallets, the "strict FIFO for this wallet" switch in ADR 0008 is the same setting.

### Consequences

- Good: a user who follows the IRS default never picks lots and never sees warnings.
- Good: warnings appear only when a choice can change the tax result.
- Bad: none known.

## References

- ADR 0008; PLAN §7; THREAT_MODEL T-508

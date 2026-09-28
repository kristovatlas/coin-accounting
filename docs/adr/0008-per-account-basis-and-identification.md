---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0008: Lot assignment for exchange sales

_Written in Simplified Technical English (ASD-STE100 style): short sentences, active voice, one idea per sentence._

## Terms

| Term | Meaning in this ADR |
|---|---|
| **Account** | One place that holds coins. It is a self-custody wallet or an exchange account. The data model calls it a `tax_account`. |
| **Lot** | A quantity of coins with one acquisition date and one cost basis. |
| **Cost basis** | The USD amount that the user paid for a lot, including fees. |
| **Lot assignment** | The user's choice of which lots an exchange sale uses. The IRS calls this "identification". |

## Where tax law applies in this app

- **On-chain, tax law adds nothing.** The app follows each UTXO from its source. Each coin's history is its lot. This is already the most exact lot tracking.
- **Tax law applies at one step: when the user sells coins on an exchange.** Coins that go into an exchange lose their UTXO identity. The user tells the app which deposited lots each sale uses. This is lot assignment. US tax law has three rules for this step.

## The three rules

1. **Use only lots in the same exchange account.**
   - A sale on exchange X can use only lots that the user deposited into exchange X.
   - This rule starts on 1 January 2025. Before 2025, many people used one pool for all wallets and exchanges.
   - The app's model is per account, so this rule costs nothing extra.
2. **Assign the lots by the time of the sale.**
   - The IRS accepts a lot assignment only if the user made it before or at the time of the sale (Treas. Reg. §1.1012-1(j)).
   - If the assignment is late, the IRS can apply its default instead: FIFO (first in, first out) in that account.
   - Until 31 December 2026, the user's own records can hold the assignment (Notice 2025-7, extended by Notice 2026-20). From 1 January 2027, the user must give the assignment to the exchange.
3. **A one-time step for coins held before 2025.**
   - A person with coins at the end of 2024 must split the cost basis of the old pool across their accounts, as of 1 January 2025 (Rev. Proc. 2024-28).
   - The app cannot calculate this split. The user enters it.
   - A user with no coins before 2025 does not do this step.

These rules also make the user's numbers agree with Form 1099-DA. Exchanges send this form for sales from 2025.

## Decision

1. For an exchange sale, the app offers only lots from that exchange account.
2. The app records when the user makes each lot assignment (`identified_at`).
3. **If the assignment is after the sale, the app warns the user.** The warning says that the IRS can apply FIFO instead, and shows the FIFO result for comparison. The app marks the sale as `late` in the audit trail and on the report. **The app uses the user's assignment.** The app does not block the report. (User decision, 2026-09-27.)
4. For a sale in 2027 or later, the app reminds the user to give the assignment to the exchange.
5. The app has an optional **opening allocation** event for 1 January 2025 (`opening_allocation_2025`), for users who held coins before 2025.
6. The app does not make a report while a lot has an unknown cost basis or a transaction is not confirmed. The user must resolve each condition. The app records each resolution in the change log.

## Consequences

- Good: the app follows the IRS rules without blocking the user.
- Good: the report shows each late assignment, so the user or a tax preparer can review it.
- Bad: a late assignment can give a result that the IRS does not accept. The app warns, but it does not prevent this.

## References

- PLAN §7; THREAT_MODEL T-508, T-509
- Treas. Reg. §1.1012-1(j); Rev. Proc. 2024-28; Notice 2025-7; [Notice 2026-20](https://www.irs.gov/pub/irs-drop/n-26-20.pdf)
- [2025 Instructions for Form 8949](https://www.irs.gov/instructions/i8949)

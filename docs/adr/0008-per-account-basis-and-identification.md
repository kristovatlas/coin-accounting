---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0008: Lot identification in every account

_Written in Simplified Technical English (ASD-STE100 style): short sentences, active voice, one idea per sentence._

## Terms

| Term | Meaning in this ADR |
|---|---|
| **Account** | One place that holds coins: a self-custody wallet or an exchange account. The data model calls it a `tax_account`. |
| **Lot** | A quantity of coins with one acquisition date and one cost basis. ADR 0009 gives the cost basis for each type of acquisition. |
| **Identification** | The record of which lots a sale, spend or transfer uses. |
| **Standing order** | An account's default identification method, for example "FIFO" or "highest cost first". An exchange keeps one on file for each customer. |

## Context and Problem Statement

The app follows each UTXO from its source. In most cases the chain shows which coins moved. In three cases the chain does not show this, and US tax rules apply (Treas. Reg. §1.1012-1(j), in force from 1 January 2025):

1. **Exchange sales and withdrawals.** Coins in an exchange lose their UTXO identity. The user must tell the app which deposited lots a sale or withdrawal uses.
2. **Merged UTXOs in a self-custody wallet.** After a consolidation, one UTXO can hold several lots. The chain does not show which lot leaves in the next spend.
3. **The default rule.** For each account, the IRS accepts an identification only if the taxpayer records it **before or at the time** of the sale or transfer. If there is no valid identification, the IRS uses the account's standing order. If there is no standing order, the IRS uses FIFO (first in, first out) in that account. This rule applies to self-custody wallets as well as to exchanges (Notice 2026-20, §2).

Other facts:
- Until 31 December 2026, the taxpayer's own records can hold an exchange identification (Notice 2025-7, extended by Notice 2026-20). From 1 January 2027, the taxpayer must give it to the exchange. This relief does not apply to self-custody wallets; for them, the taxpayer's own records are the normal method.
- Form 1099-DA can show lots and basis that differ from the taxpayer's records (Notice 2026-20). The report must show these differences.
- Before 2025, many people used one pool for all accounts. For basis that was not already tied to an account ("unattached basis"), Rev. Proc. 2024-28 gives a safe harbor to assign it to accounts as of 1 January 2025. The taxpayer must make this assignment by their first 2025 sale.

## Considered Options

For a late identification:
1. **Warn only**: record it, warn, show the standing-order or FIFO result, and keep the user's choice.
2. **Replace it** with the standing order or FIFO, and block reports until the user resolves it.
3. **Ignore timing.**

For a merged self-custody UTXO:
1. **Follow the UTXO; inside a merged UTXO, oldest lot first (FIFO)**, unless the wallet has a different recorded method.
2. FIFO across the whole wallet, ignoring UTXOs.
3. Pro-rata split. This is not an IRS method, so it was rejected.

## Decision Outcome

1. **Per account.** A sale, spend or gift uses only lots from the same account. This is true for every account.
2. **Self-custody:**
   - The spent UTXO is the identification, and the chain is the timestamped record. **This is the app's stated tax position.** The docs tell the user to confirm it with a tax preparer.
   - Inside a UTXO that holds several lots, lots leave **oldest first** unless the wallet has a different recorded method.
   - The user can set a wallet to strict FIFO across the whole wallet. (User decision, 2026-09-28.)
3. **Exchanges:**
   - For each sale and each withdrawal, the user selects lots from that exchange account.
   - The app records **when** the user made each selection (`identified_at`).
   - Each exchange account records its standing order.
4. **Late selection (user decision, 2026-09-27):**
   - If a selection is after the sale or withdrawal, the app **warns**.
   - The warning shows the result under the account's standing order, or under FIFO if there is none.
   - The app marks the selection as `late` in the audit trail and on the report.
   - **The app uses the user's selection and does not block the report.**
5. For an exchange selection in 2027 or later, the app reminds the user to give the selection to the exchange.
6. **Opening allocation (optional):**
   - The `opening_allocation_2025` event is only for unattached pre-2025 basis. The user enters the allocation, and states the method and the date they made it.
   - If that date is after the user's first 2025 sale, the app warns.
   - The allocation replaces the derived lots for that account.
   - A user who already tracked coins per wallet, as UTXO tracing does, does not need it.
7. The report flags differences between the app's lots and the lots on Form 1099-DA.

### Consequences

- Good: the app follows the IRS rules and keeps the precision of UTXO tracing.
- Good: every late or uncertain choice is visible on the report.
- Bad: a late selection, or the self-custody UTXO position, can give a result that the IRS does not accept. The app warns about this, but it does not prevent it.

## References

- PLAN §7; THREAT_MODEL T-508, T-509
- Treas. Reg. §1.1012-1(j); Rev. Proc. 2024-28; Notice 2025-7; [Notice 2026-20](https://www.irs.gov/pub/irs-drop/n-26-20.pdf)
- [Instructions for Form 1099-DA](https://www.irs.gov/instructions/i1099da)

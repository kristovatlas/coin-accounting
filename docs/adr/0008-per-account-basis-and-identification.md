---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0008: Track cost basis per account, and record lot selection at the time of sale

_Written in Simplified Technical English (ASD-STE100 style): short sentences, active voice, one idea per sentence._

## Terms

| Term | Meaning in this ADR |
|---|---|
| **Account** | One place that holds coins. It is a self-custody wallet or an exchange account. The data model calls it a `tax_account`. |
| **Lot** | A quantity of coins with one acquisition date and one cost basis. |
| **Cost basis** | The USD amount that the user paid for a lot, including fees. |
| **Lot selection** | The choice of which lots a sale uses. The IRS calls this "identification". |

## Why the year 2025 is important

The app calculates US tax. US rules for digital assets changed on 1 January 2025. The app must use the correct rules for each tax year. Otherwise, the gains are wrong, and they do not agree with the forms from exchanges.

Three changes started in 2025:

1. **Cost basis is per account.**
   - Before 2025, the rules did not clearly say how to track cost basis. Many people put all their coins, in all wallets and exchanges, into one pool.
   - From 1 January 2025, Treasury regulations require cost basis for each account separately. A sale from account A can use only lots in account A.
2. **A one-time move from the pool to accounts.**
   - A person with coins at the end of 2024 must assign the cost basis from the old pool to each account.
   - Rev. Proc. 2024-28 gives the method for this assignment, and the person must document it.
   - The app cannot calculate this assignment. The user must supply it.
3. **Exchanges send Form 1099-DA.**
   - For sales in 2025 and later, exchanges report the sale proceeds to the IRS.
   - For coins bought in 2026 and later, exchanges also report the cost basis.
   - If the app uses different lots than the exchange, the user's return does not agree with the IRS copy.

## Rule for lot selection

The IRS rule (Treas. Reg. §1.1012-1(j)) is simple: **the user must select the lots before or at the time of the sale.** A selection made later does not count. If there is no valid selection, the IRS uses FIFO (first in, first out) for that account.

- Until 31 December 2026, the user can record the selection in their own records (Notice 2025-7, extended by Notice 2026-20).
- From 1 January 2027, the user must give the selection to the exchange for exchange sales.

## Decision

1. The app tracks cost basis **per account**. A sale uses only lots from the same account.
2. The app records **when** the user made each lot selection (`identified_at`).
3. If `identified_at` is after the sale, the app marks the selection as **late**. The app then uses the account's standing method (the method on file at the exchange) or FIFO.
4. For a sale in 2027 or later, the app shows a warning: the user must give the selection to the exchange.
5. For an on-chain spend, the spent coin (UTXO) is the selection. Inside that UTXO, the app uses the account's standing method.
6. The app has an **opening allocation** event for 1 January 2025 (`opening_allocation_2025`). The user enters it for each account, from their documented assignment.
7. The app does not make a report if one of these conditions is true:
   - A lot has an unknown cost basis.
   - A transaction is not yet confirmed.
   - A late selection is not resolved.

   The user must resolve each condition. The app records each resolution in the change log.

## Consequences

- Good: the gains agree with current IRS rules and with Form 1099-DA.
- Good: each number has a record of its source.
- Bad: the user must record lot selections at the time of the sale, not later.
- Bad: the user must enter the 1 January 2025 allocation.

## References

- PLAN §7; THREAT_MODEL T-508, T-509
- Treas. Reg. §1.1012-1(j); Rev. Proc. 2024-28; Notice 2025-7; [Notice 2026-20](https://www.irs.gov/pub/irs-drop/n-26-20.pdf)
- [2025 Instructions for Form 8949](https://www.irs.gov/instructions/i8949)

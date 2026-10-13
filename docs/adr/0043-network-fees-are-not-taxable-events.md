---
status: accepted
date: 2026-10-13
deciders: repository owner (human), drafted by Claude Code
amends: 0009, 0011, 0041
architecture_sha256: 986bdf48f8db503f353185a58a94fdeed6c37711e7d292e124a5fe98422fb4b3
---

# 0043: Network fees are never a taxable event

_This ADR amends ADR 0009's fee rules for every account kind, ADR 0011's list of Form 8949 rows, ADR 0041 §2.4's gift fee and its "fee is a disposal" setting, and architecture §6's settings list._

## Context and Problem Statement

- **A network fee** is the bitcoin a transaction pays to the miners who include it in a block. An exchange may also keep a small BTC fee when you withdraw.
- **Three earlier rules treat a fee as a small taxable disposal**, with its own gain or loss and its own Form 8949 row:
  - ADR 0009's default for an exchange's BTC withdrawal fee
  - ADR 0009's optional "fee is a disposal" setting for a transfer between the user's own accounts or a deposit
  - ADR 0041 §2.4's fee on a gift given from a wallet
- **The owner's view (2026-10-13):** a network fee is a cost inherent to the network, like a card fee. It isn't a payment for a service or a sale of bitcoin, and it is excluded for tax purposes. It is never a taxable event.

## Considered Options

1. **A fee is never its own taxable event.** Chosen.
2. **Keep the earlier rules** (fees as small disposals in those three cases). Rejected: the owner's position is that fees are not disposals.
3. **Keep a setting to treat fees as disposals.** Rejected: the owner wants fees excluded, not optional, and one rule is simpler to state on reports.

## Decision Outcome

Chosen option: 1, the owner's decision (2026-10-13).

**A network fee, or an exchange's BTC withdrawal fee, is never a taxable event, in any tax year, from any account.** It produces no gain or loss and no Form 8949 row of its own. What happens to the cost of the fee's sats depends on the event the fee belongs to:

- **A sale or spend**, from a wallet (ADR 0041 §2) or sent straight from an exchange or custodial account: the fee is a cost of the sale.
  - Its sats' basis joins the sale's basis: it is added to the sale's row for the same lot, or, if the sale has none, to the sale's oldest row, which keeps its own date and holding period.
  - Proceeds are what was received; the fee is not subtracted from them. This replaces ADR 0009's "network fees on a spend reduce its proceeds" for every account kind (ADR 0041 §2.5 already did so for wallets).
  - An exchange's trading fee on a sale (what the exchange charges for the trade, not a network fee) still reduces proceeds, as ADR 0009 says.
- **A transfer between the user's own accounts, or a deposit:** the fee's basis passes to the coins that arrive, as ADR 0009's default and ADR 0041 §2.4 say. The "fee is a disposal" setting is removed. In a consolidation, it passes to the change.
- **An exchange's BTC withdrawal fee:** like a transfer fee.
  - **Which lots:** the withdrawn sats take lots first, by the withdrawal's identified lots or the account's standing method; then the fee takes the next sats in the same order. The fee's lots are part of the withdrawal's identification, so the same lateness rule applies (ADR 0008 §4, ADR 0021).
  - **Where the basis goes:** to the withdrawn coins' fragment of the same lot, else to their oldest fragment, which keeps its own date.
  - **An account with no known lots** (ADR 0009's one exception): the created lot covers the gross sats, the withdrawn sats plus the fee. The basis the user supplies covers both, and the fee's share passes to the withdrawn coins. Without it, the lot is unknown basis and blocks reports, as before.
- **A gift given:** the fee's sats leave with no gain or loss. Their basis isn't added to the gifted coins, so it doesn't change the recipient's basis. The cost is simply not recovered.
- **One event bears the whole fee.** ADR 0009's rule that splits a fee by the role of each output is retired for every account kind. The fee belongs to the transaction's leaving event (ADR 0041 §2.2), or, with none, to the consolidation.
- **Which rule applies:** the event the fee belongs to decides where its cost goes. A withdrawal that is itself a sale, spend or gift (sent straight from an exchange to someone else) still picks the fee's lots by the withdrawal rules above (which lots, lateness, an account with no known lots); only where the fee's basis goes follows the sale, spend or gift rule.
- **Gift lots with two bases** (ADR 0009's dual basis for a gift received): when a fee's basis joins a row or fragment of its own lot, both bases move together. When it joins a different lot, the lower of the fee lot's two bases moves, the conservative choice; if the receiving lot has two bases, that amount is added to both. The audit trail records the move.
- **Not decided here:** a BTC fee an exchange charges that isn't tied to a withdrawal, sale or on-chain transaction (a custody or account fee paid in BTC). It is a payment for a service rather than a network cost, so it **blocks reports** as an unsupported case until a later ADR decides it.

**Stated tax position:** printed with every report, alongside ADR 0009's and ADR 0041's positions. No IRS guidance squarely addresses a network fee, or an exchange's BTC withdrawal fee, when bitcoin is sold, spent, moved between one's own accounts or given away. The app treats it as a non-taxable cost of moving bitcoin:
- on a sale or spend, its basis is part of the sale's basis; when the fee's lot has no row in the sale, its basis joins the sale's oldest row and takes that row's holding period
- on a transfer or withdrawal, its basis stays with the coins
- on a gift, its cost isn't recovered

The more common alternative treats the fee as a small disposal at its market value.

**What changes elsewhere:**
- **ADR 0011:** Form 8949 rows come only from `sell` and `spend`, in every tax year. There are no fee rows.
- **PLAN:** §2's `fee treatment` setting is removed, §7 and §8 follow this ADR, and §7's blocking conditions include the BTC custody or account fee.
- **Architecture §6:** the user DB's settings no longer include a fee treatment. This ADR records the architecture's new SHA-256 (ADR 0014).
- **Report settings (T-503):** a report's embedded settings no longer include a fee treatment, and the rule-set version changes with this ADR. Reports print the stated position above.
- **The lot engine:** open PRs #240, #242 and #278 still implement the old rules (a `fee_treatment="dispose"` option and the withdrawal-fee disposal). #285 removes them once the stack is on `main`, with hand-worked tests. It must land before the app produces reports for anyone.

### Consequences

- Good: a fee never creates a tax line, which matches the owner's view and is simpler to explain.
- Good: on a transfer or withdrawal, the fee's cost stays on the coins, so it still lowers the gain when they are sold.
- Bad: compared with treating the fee as a disposal at its market value, the app reports **less total gain, by the fee's market value**, and at a different time and holding period. In the owner's example (buy 1 BTC for $10, pay a 0.1 BTC fee to send it to an exchange while the price is still $10, sell 0.9 BTC for $20), the gain is $10 here; as a disposal, the fee adds its $1 market value, for $11.
- Bad: on a gift, the fee's cost is lost: no one recovers it. Treating it as a disposal would instead have reported a small gain or loss.
- Bad: a fee's basis that joins another lot's row can change holding period (from a short-term lot onto a long-term row, or the reverse). The amounts are small, and the stated position says so.
- Bad: if the IRS later says fees are disposals, the app's figures would be lower by the fees' market value (transfers, withdrawals, sales) and miss small gains or losses (gifts). The stated position on each report makes that visible.

## References

- ADR 0008 §4, ADR 0009 (Fees), ADR 0011, ADR 0014, ADR 0021, ADR 0041 §2.2–2.5
- PLAN §2, §7, §8; architecture §6; THREAT_MODEL T-501, T-503, T-509, T-510
- #240, #242, #278 (the lot engine), #285 (removing the old fee rules)

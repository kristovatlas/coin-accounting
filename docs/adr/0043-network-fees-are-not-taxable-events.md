---
status: accepted
date: 2026-10-13
deciders: repository owner (human), drafted by Claude Code
amends: 0009, 0011, 0041
architecture_sha256: 226270955c3966eb50b70bbaeb50333db79bbe90484ecba79b1e26b07b2fb37f
---

# 0043: Network fees are never a taxable event

_This ADR amends ADR 0009's fee rules for every account kind, ADR 0011's list of Form 8949 rows and its box for a sale sent straight from an exchange, ADR 0041 §2.4's gift fee and its "fee is a disposal" setting, and architecture §6's settings list._

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

**A network fee, or a BTC fee an exchange takes on a buy, deposit, sale, spend, gift or withdrawal, is never a taxable event, in any tax year, from any account.** It produces no gain or loss and no Form 8949 row of its own. One event bears the whole fee: ADR 0009's rule that splits a fee by the role of each output is retired for every account kind. A fee an exchange records on its own line is part of the event it belongs to, not a second event. What happens to the cost of the fee's sats depends on that event.

**Where the fee's basis goes:**
- **A buy** with its fee withheld from the bitcoin bought (the exchange credits 0.999 BTC of 1): the lot holds the sats credited, and its basis is the full cash paid, as ADR 0009's "acquisition fees add to basis" says. Nothing is disposed of.
- **A sale or spend**, from a wallet (ADR 0041 §2) or sent straight from an exchange or custodial account, including an exchange's trading fee taken in BTC: the fee is a cost of the disposal.
  - Its sats' basis joins the disposal's basis: it is added to the disposal's row for the same lot, or, if it has none, to its oldest row, which keeps its own date and holding period.
  - Proceeds are what was received; a fee paid in BTC is not subtracted from them. This replaces ADR 0009's "network fees on a spend reduce its proceeds" for every account kind (ADR 0041 §2.5 already did so for wallets).
  - A trading fee charged in dollars still reduces proceeds, as ADR 0009 says.
- **A transfer between the user's own accounts, or a deposit:** the fee's basis passes to the coins that arrive, as ADR 0009's default and ADR 0041 §2.4 say. The "fee is a disposal" setting is removed. In a consolidation, it passes to the change.
- **An exchange's BTC deposit fee** (the exchange credits less than arrived): the deposit's sats are the gross sats that arrived, matching ADR 0041 §2's check; the account is credited the net. The fee takes its sats from the arriving lots only, by the account's standing method applied to those lots, never from lots already in the account. Its basis joins the same arriving lot in the account, else the oldest arriving lot, which keeps its own date.
- **An exchange's BTC withdrawal fee:** like a transfer fee. Its basis goes to the withdrawn coins:
  - **into a coin-traced wallet, linked:** to the first withdrawn output, in ADR 0041's canonical output order, that holds a fragment of the same lot, else to the first withdrawn output's oldest fragment, which keeps its own date
  - **anywhere else** (an unlinked wallet's pool, ADR 0041 §4, or another exchange or custodial account): to the same moved lot there, else to the oldest moved lot, which keeps its own date
- **A gift given:** the fee's sats leave with no gain or loss. Their basis isn't added to the gifted coins, so it doesn't change the recipient's basis. The cost is simply not recovered.
- **A sale, spend or gift sent straight from an exchange to someone else** is one event (`sell`, `spend` or `gift_out`) that also moves the coins out. The fee's lots are picked as for a withdrawal (below); where its basis goes follows the sale, spend or gift rule.
- **Gift lots with two bases** (ADR 0009's dual basis for a gift received): when a fee's basis joins a row or fragment of its own lot, both bases move together. When it joins a different lot, the lower of the fee lot's two bases moves, the conservative choice; if the receiving lot has two bases, that amount is added to both. The audit trail records the move.

**Which lots an exchange fee takes** on a sale, spend, gift or withdrawal (a deposit fee is above; a wallet's fee follows ADR 0041 §2.2):
- The event's own sats take lots first, by its identified lots or the account's standing method; then the fee takes the next sats.
- A specific identification may name lots for the gross sats (the event's sats plus the fee). If it names lots only for the event's sats, the fee takes the account's standing method over the remaining lots, or FIFO if there is none, and that is recorded in the identification, so the lateness rule (ADR 0008 §4, ADR 0021) is judged on the whole.
- **An account with no known lots** (ADR 0009's one exception): the created lot covers the gross sats, and the basis the user supplies covers both. Without it, the lot is unknown basis and blocks reports, as before. On a withdrawal to the user's own account the fee's share passes to the withdrawn coins; on a sale, spend or gift it follows that event's rule.

**Form 8949 box:** a sale or spend sent straight from an exchange to a buyer or merchant is an on-chain disposal, not a sale the broker carried out. Its default box is I/L when no Form 1099-DA reports it; if one does, ADR 0011's inputs apply as usual (H/K, or G/J when basis is reported). The user can override it, with the reason recorded.

**Unsupported, so reports are blocked** (like ADR 0041 §3), each until a later ADR decides it. These have no user resolution in the meantime:
- a BTC fee an exchange charges that isn't tied to one of the events above (a custody or account fee paid in BTC), or a buy's fee paid from bitcoin already held. These are payments for a service rather than a cost of moving or selling bitcoin, or spend other coins
- a transaction from an exchange or custodial account with more than one leaving event (part to the user's wallet, part to someone else), not counting a fee recorded on its own line, as ADR 0041 §3 already does for wallets
- a transaction with no destination coins at all: every input goes to the fee (for example a sweep whose only output is a zero-value OP_RETURN)

**Stated tax position:** printed with every report, alongside ADR 0009's and ADR 0041's positions.
- **Sales and spends:** Treasury's 2024 broker regulations charge a sale's transaction costs to the sale (Treas. Reg. §1.1001-7(b)(2)). Their basis rules' example treats other digital assets used to pay such costs as themselves disposed of (§1.1012-1(h)). Instead, the app adds the fee's basis to the sale and reports no separate row. For a lot with one basis the total gain is the same; only the separate fee row is missing. It can differ when the fee's basis joins another lot's row (it takes that row's holding period) or comes from a gift lot with two bases (the lower basis moves).
- **Transfers, deposits and withdrawals:** no guidance squarely addresses a fee when bitcoin moves between one's own accounts. The app treats it as a non-taxable cost: its basis stays with the coins. When the fee's lot has no row or fragment at the destination, its basis joins the oldest one and takes that holding period.
- **Gifts:** the fee's cost isn't recovered.
- **Brokers:** a broker's proceeds on Form 1099-DA are net of transaction costs, and a 1099-DA may list a fee as a sale. On an exchange sale with a fee taken in BTC, the app's proceeds are gross, with the fee's basis added instead. The total gain is the same for a one-basis lot, but the row's proceeds and basis differ from the broker's, and the report's comparison with broker figures flags each such row, and any fee the broker lists, for review (PLAN §8).

**What changes elsewhere:**
- **ADR 0011:** Form 8949 rows come only from `sell` and `spend`, in every tax year. There are no fee rows.
- **PLAN:** §2's `fee treatment` setting is removed, §7 and §8 follow this ADR, and §7's blocking conditions include its unsupported cases. §2's `fee` event and the per-event fee role are redefined (below).
- **Architecture §6:** the user DB's settings no longer include a fee treatment, and the tax engine's "fees by role" becomes "fees". This ADR records the architecture's new SHA-256 (ADR 0014).
- **Report settings (T-503):** a report's embedded settings no longer include a fee treatment, and the rule-set version changes with this ADR. Reports print the stated position above.
- **ADR 0011 and T-510:** a sale or spend sent straight from an exchange defaults to I/L.
- **The lot engine:** open PRs #240, #242 and #278 still implement the old rules (a `fee_treatment="dispose"` option and the withdrawal-fee disposal). #285 removes them once the stack is on `main`, with hand-worked tests. It must land before the app produces reports for anyone.

### Consequences

- Good: a fee never creates a tax line, which matches the owner's view and is simpler to explain.
- Good: on a sale or spend from a lot with one basis, the total gain is the same as under the broker regulations' treatment.
- Good: on a transfer or withdrawal, the fee's cost stays on the coins, so it still lowers the gain when they are sold.
- Bad: on a transfer, deposit or withdrawal, compared with treating the fee as a disposal at its market value, the app reports **less total gain, by the fee's market value**, once the coins are sold, and at a different time and holding period. In the owner's example (buy 1 BTC for $10, pay a 0.1 BTC fee to send it to an exchange while the price is still $10, sell the 0.9 BTC for $20 in total), the gain is $10 here; as a disposal, the fee adds its $1 market value, for $11. For a gift lot with two bases the gap can be smaller, zero, or the other way, since a price between the two bases has no gain or loss and only the lower basis moves to another lot.
- Bad: on a gift, the fee's cost is lost: no one recovers it. Treating it as a disposal would instead have reported a small gain or loss.
- Bad: a fee's basis that joins another lot's row can change holding period (from a short-term lot onto a long-term row, or the reverse). The amounts are small, and the stated position says so.
- Bad: rare cases (a BTC custody or account fee, a buy's fee paid from bitcoin already held, a custodial transaction with more than one leaving event, a transaction that is all fee) block reports with no user resolution until a later ADR.
- Bad: exchange sales with a fee taken in BTC won't match the 1099-DA's proceeds and basis row by row, so each is flagged for review. Reporting the broker's net proceeds and correcting the basis in Form 8949's adjustment column is a possible later change.
- Bad: if the IRS later says fees are disposals, the app's figures would be lower by the fees' market value (transfers, withdrawals) and miss small gains or losses (gifts). The stated position on each report makes that visible.

## References

- ADR 0008 §4, ADR 0009 (Fees), ADR 0011, ADR 0014, ADR 0021, ADR 0041 §2.2–2.5
- PLAN §2, §7, §8; architecture §6; THREAT_MODEL T-501, T-503, T-509, T-510
- #240, #242, #278 (the lot engine), #285 (removing the old fee rules)

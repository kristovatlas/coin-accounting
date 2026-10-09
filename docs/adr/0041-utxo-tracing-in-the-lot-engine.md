---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2 and ADR 0009 where they describe self-custody movements, and adds to PLAN §2's data model. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied in v1. ADR 0040 is taken by open PR #278._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coins (UTXOs) a transaction spends identify the lots that leave (ADR 0008 §2).
  - **Inside a coin holding several lots,** they leave oldest first.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **The owner:** tracing must exist before anyone uses the app, because self-custody transfers are extremely common.
- **What's undecided:** how lots get onto coins, which lots leave when a transaction pays out and returns change, and what happens to wallet lots on no coin. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **Cover the ordinary cases well,** and block the rare ones rather than guess.
  - **The app is for estimates and tracking coins** (the owner, 2026-10-09). A transaction it doesn't support yet is refused with a clear message, never figured silently.
  - **Rules for rare cases** are added when someone actually needs them.
- **The stated method decides, never wallet software.** An output's position in a transaction changes no figure.
- **Nothing doubled, nothing lost.** Every output holds exactly its value. Anything impossible blocks until it's corrected.

## Considered Options

1. **A short design for the ordinary cases, refusing the rest.** Chosen.
2. **A full design with a rule for every case** (PayJoin, CoinJoin, multi-account transactions, identical outputs, method switching). Drafted in this PR's earlier rounds and rejected by the owner as too much for v1. It is in this PR's history if it's needed later.
3. **Keep account-wide FIFO for wallets.** This contradicts ADR 0008 §2; rejected.

## Decision Outcome

### 1. Links: where lots arrived
- **An arrival event names the wallet output it arrived in, with sats.** An arrival event is an acquisition into a wallet, or a withdrawal's moved lots.
  - **Several outputs:** an event can name several outputs. Its lots fill them in the canonical order (§2).
- **Who makes links:** the app proposes a link only from a record that names the transaction (an exchange export's txid, or the user's note). An amount-and-time match is a suggestion, never a link. The user confirms each link.
- **A withdrawal creates no lot.** Its moved fragments keep their basis and dates (ADR 0009). ADR 0009's one exception stays: a withdrawal from an exchange account with no lots creates one.
- **Blocks the report until corrected:**
  - a link to an output the account doesn't own
  - an event's links adding up to more than it delivered
  - an output whose lots would exceed its value

### 2. A transaction from a wallet
This covers a self-transfer, deposit, spend, sale or gift given that spends coins from one self-custody account.
1. **The spent coins' lots are taken oldest first,** across all the coins the transaction spends.
2. **What leaves the wallet takes lots first:** the payment, sale, gift or deposit. Then the fee. **The change takes what is left.**
3. **The canonical order:** where several outputs share a role, they are filled one after another, largest first, then by the output script. Output position never decides.
4. **The fee (ADR 0009):**
   - **A spend or sale:** the fee's sats are part of the disposal (their basis is in it).
   - **A self-transfer or deposit:** by default no disposal; the fee's basis moves to the coins that arrive, as ADR 0009 says. With the "fee is a disposal" setting, it is a small disposal.
   - **A gift:** the fee is a small disposal at its market value.
5. **Proceeds:**
   - **A sale:** what was received.
   - **A spend:** the value of what was received, or of the BTC paid if that isn't recorded.
   - **The network fee is not subtracted again.**

**A worked example** (a test in the implementing PR):
- **The coin:** 1 BTC, holding L1 (0.4, oldest, basis $10,000) and L2 (0.6, basis $30,000).
- **The spend:** 0.5 BTC for goods worth $50,000, with 0.4999 change and a 0.0001 fee.
- **Whether the change is first or second in the transaction:**
  - the payment gets L1 0.4 and L2 0.1
  - the fee gets L2 0.0001
  - the change keeps L2 0.4999
- **The disposal:** basis $15,005, proceeds $50,000, gain $34,995.

### 3. Not supported yet: the report blocks
Each of these blocks the report with a message naming the transaction, until a later ADR adds a rule:
- **Shared transactions:** a transaction with inputs the user doesn't own (PayJoin, CoinJoin, PLAN §3).
- **Several accounts:** a transaction spending coins from more than one of the user's accounts.
- **Identical outputs:** two outputs with the same value and script, where the lots they get would differ.

### 4. Lots on no coin: the pool
- **The pool** is wallet lots that aren't linked to any coin, such as a balance from before the wallet was tracked.
- **Short coins draw from it.** A coin whose linked lots don't cover it takes the rest from the pool, oldest first, when it arrives. Coins arriving in the same block are taken in chain order, and the canonical order applies within a transaction. The report notes each draw.
  - **Only lots in the pool when the coin arrived** count, with two hours' allowance for block-time drift.
  - **Linked sats never enter the pool.**
- **If the pool is short,** the rest becomes a **placeholder lot of unknown basis** on that coin. It blocks reports (ADR 0009) until the user links a purchase, or enters it as a typed acquisition (a buy, income, a gift or an inheritance).
- **Leftover pool lots,** while every coin is covered, show a warning: probably a duplicate purchase or an unrecorded sale.

### 5. Records and lateness
- **Links, and their edits and removals,** are kept in the append-only change log (T-408). The audit trail marks any made after their coin was spent.
- **A link is a fact,** not a choice of lots, so it is never flagged "late".
- **Figures use the current result.** If a later edit changes an earlier transaction's lots, the report shows it.
- **Whole-wallet FIFO** (ADR 0008 §2) stays available as an account setting. With it, the wallet ignores coins and takes lots by FIFO, as the engine does today.
  - **Changing the setting** after a transaction it affects is flagged late, like any late lot choice (T-508). The setting is recorded with an app-set time.
- **Only confirmed transactions count** (T-207). Links and draws on unconfirmed coins wait for confirmation. A reorged-out transaction's links and draws go to the review queue (T-506).
- **The 2025 opening allocation** works as ADR 0008 §6 says: it covers only basis not tied to coins (it replaces the pool on 2025-01-01). Its lateness is judged against the account's first 2025 sale.

### 6. Records in other documents
- **This is a stated tax position** (ADR 0008 §2), printed with the reports.
- **PLAN §2, PLAN §7, and THREAT_MODEL T-408, T-506, T-508 and T-509** record these rules in this PR. The implementing PR adds tests.
- **#271's refusal is lifted.**
- **The engine's input:** each wallet event carries its owned inputs and outputs with their roles, from the chain cache. `services/` builds them; the engine stays pure. Each account carries its kind (`self_custody | custodial`).

### Consequences

- **Good:** ordinary self-custody figures follow the chain. The release blocker on self-transfers goes. The design is small enough to build soon.
- **Good:** rare cases block with a clear message, and nothing is figured silently.
- **Bad:**
  - arrivals into wallets need their outputs linked
  - a user with a PayJoin, CoinJoin, multi-account or identical-output transaction can't produce a report until a rule for it is added
- **Unchanged:** exchange accounts.

## References

- ADR 0008 §1, §2, §6; ADR 0009; ADR 0011; ADR 0021; ADR 0038
- PLAN §2, §3, §7
- THREAT_MODEL T-207, T-408, T-506, T-508, T-509
- #243 and #271 (the refusal); #276's history (the full design, rejected for v1)

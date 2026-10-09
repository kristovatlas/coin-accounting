---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009, 0021
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2 and §6, ADR 0009 where it describes self-custody movements, and ADR 0021 §1 and §3 for self-custody wallets. It adds to PLAN §2's data model. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied in v1. ADR 0040 is taken by open PR #278._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coins (UTXOs) a transaction spends identify the lots that leave (ADR 0008 §2).
  - **Inside a coin holding several lots,** they leave oldest first.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **The owner:** tracing must exist before anyone uses the app, because self-custody transfers are extremely common.
- **What's undecided:** how lots get onto coins, which lots leave when a transaction pays out and returns change, where a transfer fee's basis goes, and what happens to wallet lots on no coin. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **Cover the ordinary cases well,** and block the rare ones rather than guess.
  - **The app is for estimates and tracking coins** (the owner, 2026-10-09). A transaction it doesn't support yet is refused with a clear message, never figured silently (T-507).
  - **Rules for rare cases** are added when someone actually needs them.
- **The stated method decides, never wallet software.** An output's position in a transaction changes no figure.
- **Nothing doubled, nothing lost.** Every output holds exactly its value. Anything impossible blocks until it's corrected.

## Considered Options

1. **A short design for the ordinary cases, refusing the rest.** Chosen.
2. **A full design with a rule for every case** (PayJoin, CoinJoin, multi-account transactions, identical outputs, mixed roles, method switching). It was drafted in this PR's earlier rounds and rejected by the owner as too much for v1. It stays in this PR's history if it's needed later.
3. **Keep account-wide FIFO for wallets.** This contradicts ADR 0008 §2; rejected.

## Decision Outcome

### 1. Links: where lots arrived
- **An arrival event names the wallet output it arrived in, with sats.** An arrival event is an acquisition into a wallet, or a withdrawal's moved lots.
  - **Several outputs:** an event can name several outputs. Its lots, oldest first (ties by event id), fill them in the canonical order (§2).
- **Who makes links:** the app proposes a link only from a record that names the transaction (an exchange export's txid, or the user's note). An amount-and-time match is a suggestion, never a link. The user confirms each link.
- **A withdrawal creates no lot.** Its moved fragments keep their basis and dates (ADR 0009). ADR 0009's one exception stays: a withdrawal from an exchange account with no lots creates one.
- **Blocks the report until corrected:**
  - a link to an output outside the account, or one the account doesn't own
  - an event's links adding up to more than it delivered (each output's share counts, so 0.75 BTC to each of two outputs from a 1 BTC event blocks)
  - an output whose lots would exceed its value
  - an event that names a txid other than its linked output's
  - a lot that entered the account more than two hours after its linked output's block time, the allowance for block-time drift (PLAN §7)

### 2. A transaction from a wallet
This covers a transaction that spends coins from one coin-traced self-custody account and has **at most one leaving event** (one sale, payment, gift, deposit or transfer, which may have several outputs):
- **the leaving roles:** a payment or sale, a gift, a deposit to an exchange, or a transfer to another of the user's wallets
- **the change:** outputs back to the same account

The rules:
1. **The spent coins' lots are taken oldest first,** across all the coins the transaction spends. Lots acquired at the same moment are ordered by their event id; this tie-break applies everywhere this ADR says "oldest first".
2. **The leaving outputs take lots first. Then the fee. The change takes what is left.**
3. **The canonical order:** where several outputs share a role (two change outputs, say), they are filled one after another, largest first, then by the output script. Output position never decides.
4. **The fee (ADR 0009):**
   - **A spend or sale:** the fee's sats are part of the disposal (their basis is in it).
   - **A gift:** the fee is a small disposal at its market value.
   - **A deposit or transfer to another wallet, by default:** no disposal. Each lot the fee consumes passes its basis to that lot's fragment on the first leaving output in the canonical order that holds it. If no leaving output holds that lot, the basis goes to the first leaving output's oldest fragment, which keeps its own date. With the "fee is a disposal" setting, the fee is a small disposal instead.
   - **A transaction with no leaving event** (a consolidation into the same account): the fee takes lots first, then the outputs; its basis follows the same rule, with the change outputs as the destination. With the "fee is a disposal" setting, it is a small disposal, as for a transfer.
5. **Proceeds:**
   - **A sale:** what was received.
   - **A spend:** the value of what was received. If that isn't recorded, the market value of the payment outputs, excluding the fee.
   - **The network fee is not subtracted again.**
6. **A coin created by such a transaction carries the lots it receives here.** It never draws from the pool (§4).

**A worked example:**
- **The coin:** 1 BTC, holding L1 (0.4, oldest, basis $10,000) and L2 (0.6, basis $30,000).
- **The spend:** 0.5 BTC for goods worth $50,000, with 0.4999 change and a 0.0001 fee.
- **Whether the change is first or second in the transaction:**
  - the payment gets L1 0.4 and L2 0.1
  - the fee gets L2 0.0001
  - the change keeps L2 0.4999
- **The disposal:** basis $15,005, proceeds $50,000, gain $34,995.

**A second worked example:** the same coin moves 0.5 BTC to another of the user's wallets, with 0.4999 change and a 0.0001 fee, by default.
- **The destination** gets L1 0.4 and L2 0.1. The fee's 0.0001 of L2 ($5 of basis) passes to the destination's L2 fragment, which holds 0.1 with $5,005 of basis.
- **The change** keeps L2 0.4999.
- **No disposal.**

Both examples are tests in the implementing PR, together with a third that permutes the outputs.

### 3. Not supported yet: the report blocks
Each of these blocks the report with a message naming the transaction or account, until a later ADR adds a rule:
- **More than one leaving event:** two payments, a payment plus a deposit, and so on.
- **Unclassified outgoing outputs:** a wallet transaction with an output to someone else that no recorded sale, spend, gift, deposit or transfer covers. This prevents a taxable disposal from going missing.
- **Shared transactions:** a transaction with inputs the user doesn't own (PayJoin, CoinJoin, PLAN §3).
- **Several accounts:** a transaction spending coins from more than one of the user's accounts.
- **Identical outputs:** two outputs with the same value and script, where the lots they get would differ.
- **Other methods:** a wallet with a recorded method other than oldest-first or whole-wallet FIFO (HIFO, say).
- **Switching back:** switching a wallet from whole-wallet FIFO back to coin tracing while it holds coins.
- **Mixed transfers:** a transfer between a whole-wallet FIFO wallet and a coin-traced wallet, in either direction.
- **No tracking start date:** a self-custody account without one (§4).
- **Unrecorded receipts:** the part of a coin sent from someone else, arriving after the wallet's tracking start date (§4), that its links don't cover. This applies to coin-traced wallets.

### 4. Lots on no coin: the pool
- **The pool** is wallet lots that aren't linked to any coin, such as a balance from before the wallet was tracked.
- **Each wallet has a tracking start date,** set by the user: the date from which every coin sent to it from outside is recorded. It can't be later than the time it is entered.
- **Which coins draw from the pool:** only the part, not covered by links, of a coin received from outside the user's wallets that arrived before the tracking start date. The same part of a later coin blocks (§3).
  - **Coins created by a traced wallet transaction** are covered by §2. They never draw.
- **How a coin draws:** it takes from the pool oldest first, when it arrives. Coins are taken in chain order: block height, then the transaction's position in its block (stored with each cached transaction), then the canonical order within a transaction.
  - **Only lots in the pool when the coin arrived** count, with the two-hour allowance.
  - **Linked sats never enter the pool.**
  - **The report notes each draw.**
- **If the pool is short,** the rest becomes a **placeholder lot of unknown basis** on that coin. It blocks reports (ADR 0009) until the user links a purchase, or enters it as a typed acquisition (a buy, income, a gift or an inheritance).
- **Leftover pool lots,** while every coin is covered, show a warning: probably a duplicate purchase or an unrecorded sale.

### 5. Records and lateness
- **Links, their edits and removals,** the tracking start date and the whole-wallet FIFO setting are kept in the append-only change log, each with an app-set time (T-408).
  - **After-the-fact edits:** the audit trail and the report mark a link edit made after its coin was spent, and a tracking start date set or changed after a coin it reclassifies arrived.
- **A link is a fact,** not a choice of lots, so it is never flagged "late".
- **Figures use the current result.** If a later edit changes an earlier transaction's lots, the report shows it.
- **A report's input hash** covers the engine's whole input as `services/` builds it (T-503): events, prices, the change-log records of links, tracking start dates and the whole-wallet FIFO setting (with edits, removals and times), and the chain data each wallet transaction carries (txid, block hash and height, position in block, block time, owned inputs and outputs with value, script and role).
- **Whole-wallet FIFO** (ADR 0008 §2; ADR 0021 §3's strict-FIFO switch) is an account setting with an effective-from time.
  - **What it does:** from that time, the wallet ignores coins and takes lots by FIFO, as the engine does today.
  - **A late change:** changing it after a transaction it affects is flagged late (T-508). This narrows ADR 0021 §1's "never late" for this setting.
- **Unconfirmed transactions:** any transaction below the confirmation threshold that touches a wallet coin blocks the report, as ADR 0009 and PLAN §7 say (T-207). That includes coins created by traced transactions. A reorged-out transaction's links and draws go to the review queue (T-506).
- **The 2025 opening allocation** (ADR 0008 §6) covers basis not tied to linked coins.
  - **On 2025-01-01, it replaces** the undrawn pool and every fragment on the coins then held that traces back to a pool draw or a placeholder, through traced transactions (change included). Fragments that trace back to a link are kept.
  - **It fills those coins** oldest coin first (chain order, then the canonical order), allocated lots oldest first, each coin to its value. A shortfall is a placeholder; a surplus shows the leftover warning.
  - **Lateness:** it is late if recorded after the account's first sale, disposition or transfer of BTC on or after 2025-01-01, or after the 2025 return's due date if that is earlier (Rev. Proc. 2024-28 §5.02).

### 6. Records in other documents
- **This is a stated tax position** (ADR 0008 §2), printed with the reports.
- **PLAN §2, §7 and §8, and THREAT_MODEL T-408, T-503, T-506, T-508 and T-509** record these rules in this PR. The implementing PR adds tests.
- **#271's refusal is lifted.**
- **The engine's input:** each wallet event carries its owned inputs and outputs with their roles and the transaction's chain position, from the chain cache. `services/` builds them; the engine stays pure. Each account carries its kind (`self_custody | custodial`) and its tracking start date.

### Consequences

- **Good:** ordinary self-custody figures follow the chain. The release blocker on self-transfers goes. The design is small enough to build soon.
- **Good:** rare cases block with a clear message, and nothing is figured silently.
- **Bad:**
  - arrivals into wallets after the tracking start date need their outputs linked
  - a user with a rare transaction can't produce a report until a rule for it is added
- **Unchanged:** exchange accounts.

## References

- ADR 0008 §1, §2, §6; ADR 0009; ADR 0011; ADR 0021; ADR 0038
- PLAN §2, §3, §7, §8
- THREAT_MODEL T-207, T-408, T-503, T-506, T-507, T-508, T-509
- Rev. Proc. 2024-28 §5.02 (https://www.irs.gov/pub/irs-drop/rp-24-28.pdf)
- #243 and #271 (the refusal); #276's history (the full design, rejected for v1)

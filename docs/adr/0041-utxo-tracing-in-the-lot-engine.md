---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2 and ADR 0009 where they describe self-custody movements, and PLAN §2's data model. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied. ADR numbers 0039 and 0040 are taken by open PRs #268 and #273._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coins (UTXOs) a transaction spends identify the lots that leave.
  - **Inside merged coins:** lots leave oldest first, unless the wallet records another method.
  - **Strict whole-wallet FIFO:** a wallet can choose it instead.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **Deposits, spends, sales and gifts from a wallet** still use the account's order.
  - **The owner:** self-custody transfers are extremely common, so tracing blocks release.
- **What isn't decided:** how lots get onto coins; which lots leave, and to which outputs, in a transaction with several outputs; fees; wallet lots on no coin; coins whose lots don't match them; transactions shared with others or spanning accounts. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **The stated method decides, never wallet software.** Output order (random, or BIP69-sorted) changes no figure, anywhere.
- **No guessing, no silent gaps, no double counting.** Where the chain doesn't say which lot moved, a stated rule decides and the report says so. A lot's sats are never placed twice. Anything impossible blocks until it's corrected.
- **Facts versus choices.** A link says where a purchase arrived (a fact). A method says which lots leave (a choice, which must be timely: Treas. Reg. §1.1012-1(j), T-508).
- **Recomputable, auditable, per account.** Fragments are recomputed from events, links and the chain cache. Every user edit is in the append-only change log (T-408). A coin's lots belong to the account holding it (ADR 0008 §1).

## Considered Options

**A. How lots reach a coin**
1. **Each acquisition into a wallet, and each withdrawal's moved lots, name the outputs they arrived in,** with sats for each.
   - **The app proposes links only from a record that names the transaction:** an exchange export's txid, or the user's own note.
   - **An amount-and-time match is shown as a suggestion, never a link.** The user confirms each link.
2. **Match automatically** by amount and date. This is guessing; rejected.

**B. Wallet lots on no coin:** they form the wallet's **unattached pool** (B1), rather than blocking every user with a gap in their history (B2, rejected).

**C. Which lots leave, and where they go**
1. **By role, in the identification order, with proportional shares among same-role outputs** (below).
2. **Fill outputs in vout order.** Figures would depend on wallet software; rejected.

## Decision Outcome

Proposed: **A1, B1, C1**, with the rules below. The ones marked **(owner)** are the owner's choice; the text gives the recommended default.

### Links (facts)
- **What a link says:** a lot, or a number of its sats, arrived in an output of the same account. An acquisition, or a withdrawal's moved lots, can name several outputs.
- **A withdrawal creates no lot:** the exchange fragments it moves (chosen as for any exchange withdrawal, ADR 0008 §3) attach to the outputs it names, keeping their basis and dates. ADR 0009's one exception stays: a withdrawal from an exchange account with no lots creates one.
- **Refused or blocking:**
  - **Refused:** a link to an output of another account, or one the account doesn't own.
  - **Blocks the report until corrected:**
    - any link that would place more of a lot's sats than the lot holds (no double counting)
    - an output whose linked sats exceed its value
    - a lot that entered the account after its output arrived. The tolerance is the block-timestamp drift, two hours (PLAN §7); a wider gap needs the timestamp or the link corrected.
- **Records:**
  - **Each link records** when it was made, and whether it came from a record (proposed) or from the user.
  - **The audit trail lists** every link and its source, and marks links recorded after their coin was spent.
  - **A link isn't an identification,** so it isn't flagged "late" (owner; see below).
  - **Every link create, edit and removal** is in the change log, old to new (T-408).
- **Reorgs (T-506):** links, placeholder lots and their resolutions, and pool draws on outputs of a reorged-out or replaced transaction go to the review queue. Nothing is carried over to a replacement silently.

### A wallet transaction
This applies to every event that draws from a self-custody account: self-transfer, deposit, spend, sell and gift given.

1. **One account per transaction.** If a transaction's owned inputs belong to several accounts, it blocks until the user states each account's part: which leaving outputs, which change, and what share of the fee. Each account's part then follows the rules below on its own fragments. A self-transfer between accounts is stated as such.
2. **The user's part only.** In a transaction shared with others (PayJoin, CoinJoin, PLAN §3), the event names the user's leaving outputs, the user's sats in each, and the user's fee share. Third-party inputs and outputs carry none of the user's lots. These must balance: owned inputs = owned outputs + the user's leaving sats + the user's fee share. Otherwise the transaction blocks (BIP 78 lets a receiver add to a payment output).
3. **The identification order:** the spent coins' fragments form one merged set, ordered by the account's method in force at the transaction (oldest first by default).
4. **Leaving roles first, in a fixed order (owner):** disposals (spend, sell) first, then gifts, then deposits and transfers to another own account. Recommended, because it keeps a disposal oldest first whatever else the transaction does.
   - **Several outputs of the same role** take each fragment in proportion to their sats.
   - **Rounding:** integer sats, with any remainder sat to the larger output. Equal outputs break the tie by txid:vout, which only decides single sats.
5. **The fee comes next, by role (ADR 0009):**
   - **A spend or sale:** the fee sats are part of the disposal. Proceeds are net of the fee.
   - **A self-transfer or deposit, by default:** no disposal. The fee sats' basis goes to the fragments of the same lot that arrive at the destination output, keeping that lot's date. If none arrive there, it goes to the same lot's change fragment. If neither exists, it goes to the next arriving fragment in the identification order.
   - **The "fee is a disposal" setting:** the fee sats are a small disposal, taken in the identification order.
   - **A gift given (owner):** the fee is a small disposal at its fair market value. Recommended, because BTC paid as a fee is property disposed of, and the gift itself keeps no gain or loss (ADR 0011).
   - **Several roles in one transaction:** the fee splits across roles in proportion to each role's sats. Rounding is in integer sats, with remainders to the earlier role in step 4's order.
6. **The owned change** takes the remaining fragments in proportion to each change output's sats, by the same rounding rule.

**A worked example** (a test in the implementing PR): a 1 BTC coin holds L1 (0.4, oldest) and L2 (0.6). It pays 0.5 BTC to a merchant, with 0.4999 change and a 0.0001 fee. Whether the change is vout 0 or vout 1:
- the payment gets L1 0.4 and L2 0.1
- the fee gets L2 0.0001
- the change keeps L2 0.4999

**A second test:** permuting all outputs of every role leaves every figure unchanged.

### The pool
- **The pool** is the wallet's lots on no coin.
- **When it is entered:** a lot or fragment enters the pool when it enters the account. That is the acquisition's time; for an unlinked withdrawal, the withdrawal's time; for a gift received, its receipt; for allocated lots, 2025-01-01. A holding-period date is not an entry time.
- **Short coins draw at their arrival, in chain order** (block height, position in the block, then vout). This includes unspent coins, so holdings are right too.
  - **Which pool lots count:** those in the pool by then, by the account's standing method.
  - **Each draw writes an identification** of the coin's draw (PLAN §2). It is never late (ADR 0021). The report and audit trail show it with the coin.
- **If the pool is short,** the rest becomes a **placeholder lot of unknown basis** on that coin:
  - **Its provisional date** is the coin's arrival.
  - **It blocks reports** (ADR 0009, T-509), as for a withdrawal from an account without lots (PLAN §7).
  - **The user resolves it** by entering its date and basis, or by linking an acquisition. The change log records it.
- **An edit that changes an earlier draw:** a new link, a removed link, or a back-dated acquisition can change what was in the pool at an earlier coin's arrival. A changed draw is shown on the report against the earlier result. It is a correction of facts, not a late identification.

### Methods over time (choices)
- **Each method record stores two times:** its effective-from time and the time it was recorded. The methods are oldest first, a recorded method, or whole-wallet FIFO. They are kept in the change log.
- **It applies to transactions from its effective-from time.** A transaction it reaches whose lots would differ is flagged late if the record was made after that transaction. The report shows both results (T-508, ADR 0021: no flag when the lots don't differ).
- **Under whole-wallet FIFO** the wallet's fragments are held by the account, not by coins (PLAN §2 allows either holder). Every event takes from them by FIFO, as the engine does today.
  - **Switching to it** moves every coin's fragments to the account at the effective-from time.
  - **Switching back** puts the account's fragments in the pool at that time.

### The 2025 opening allocation (ADR 0008 §6)
- **It replaces the account's derived fragment state as of 2025-01-01.**
- **Earlier links stay,** and figures before 2025 still use them.
- **The allocated lots go only onto coins the account held unspent on 2025-01-01,** up to each coin's value. The rest goes to the pool.
- **The report lists the links the allocation overrides.**

### Late links: facts, not choices (owner)
- **Recommended:** a link is a fact about where coins arrived, not a choice of lots, so it is never flagged late. The chain is the identification (ADR 0008 §2).
  - **What stays visible:** the audit trail marks every link recorded after its coin was spent, and the report shows any earlier figure an edit changed.
  - **Why:** the app is used to rebuild history after the fact, so a "late" flag on every honest link would hide the ones that matter.
- **The alternative:** flag a link recorded after its coin was spent, when its result differs from the result without it, and show both.

### Records
- **This is a stated tax position** (ADR 0008 §2), printed with the reports. That includes the role order, the proportional split, the fee rules, pool draws and placeholders.
- **PLAN §2, PLAN §7, and THREAT_MODEL T-408, T-506, T-508 and T-509** record these rules in this PR. The implementing PR adds tests for every case above.
- **#271's refusal is lifted.**
- **The engine's input:** each wallet event carries its owned inputs and outputs, with each output's role and the user's sats in it, from the chain cache and the event. `services/` builds them; the engine stays pure. Each account carries its kind (`self_custody | custodial`).

### Consequences

- **Good:** self-custody figures follow the chain and the stated method, never wallet software. The release blocker on self-transfers goes.
- **Good:** impossible states block, and gaps and corrections are visible.
- **Bad:**
  - acquisitions and withdrawals into wallets need their outputs linked
  - shared and multi-account transactions need the user's part stated
  - the engine gains a coin model with roles per output
- **Unchanged:** exchange accounts.

## References

- ADR 0008 §1, §2, §3 and §6; ADR 0009; ADR 0011; ADR 0021; ADR 0038; ADR 0040 (PR #273)
- PLAN §2, §3 (mixing), §7
- THREAT_MODEL T-408, T-501, T-506, T-508, T-509
- Treas. Reg. §1.1012-1(j); BIP 78 (PayJoin)
- #243 and #271 (the refusal); #238 and #273 (the late-choice replay)

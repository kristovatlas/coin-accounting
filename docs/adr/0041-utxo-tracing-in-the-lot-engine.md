---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009, 0021
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2, ADR 0009 where they describe self-custody movements, ADR 0021 §2 ("never late") for wallet method records, and PLAN §2's data model. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied. ADR numbers 0039 and 0040 are taken by open PRs #268 and #273; the THREAT_MODEL version is reassigned at merge._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coins (UTXOs) a transaction spends identify the lots that leave.
  - **Inside merged coins:** lots leave oldest first, unless the wallet records another method.
  - **Strict whole-wallet FIFO:** a wallet can choose it instead.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **The owner:** self-custody transfers are extremely common, so tracing blocks release.
- **What isn't decided:** how lots get onto coins; which lots leave, and to which outputs; fees; wallet lots on no coin; coins whose lots don't match them; transactions shared with others or spanning accounts; mode switches; the 2025 opening allocation. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **The stated method decides, never wallet software.** An output's position in the transaction changes no figure.
- **Every output holds exactly its value,** and every lot's sats are where the replay says, at every point in time: nothing doubled, nothing lost. Anything impossible blocks until it's corrected.
- **Facts versus choices.** A link says where a purchase arrived (a fact). A method says which lots leave (a choice, which must be timely: Treas. Reg. §1.1012-1(j), T-508).
- **Recomputable, auditable, per account.** Fragments are recomputed from events, links and the chain cache. Every user edit is in the append-only change log (T-408). A coin's lots belong to the account holding it (ADR 0008 §1).

## Considered Options

**A. How lots reach a coin**
1. **Each arrival event names the outputs it arrived in, with sats for each.** An arrival event is an acquisition into a wallet, or a withdrawal's moved lots.
   - **Proposed links:** the app proposes them only from a record that names the transaction (an exchange export's txid, or the user's note). An amount-and-time match is a suggestion, never a link. The user confirms each link.
2. **Match automatically** by amount and date. This is guessing; rejected.

**B. Wallet lots on no coin:** they form the wallet's **unattached pool** (B1), rather than blocking every user with a gap in their history (B2, rejected).

**C. Which lots leave, and where they go**
1. **By role, in the identification order, filling outputs one after another in a canonical order** that ignores their position (below).
2. **Fill outputs in vout order:** figures would depend on wallet software; rejected.
3. **Proportional shares across outputs:** rounding can't keep every output exact; rejected.

## Decision Outcome

Proposed: **A1, B1, C1**, with the rules below. The ones marked **(owner)** are the owner's choice; the text gives the recommended default.

### Links (facts)
- **A link belongs to one arrival event.** It says that some of the sats that event delivered (its lot, or the exchange fragments a withdrawal moved, chosen as ADR 0008 §3 says) arrived in an output of the same account.
  - **A withdrawal creates no lot:** its fragments keep their basis and dates. ADR 0009's one exception stays: a withdrawal from an exchange account with no lots creates one.
  - **The same lot can arrive in a wallet again later,** after a deposit and a second withdrawal. Each withdrawal is its own arrival event with its own links.
- **Refused:** a link to an output of another account, or one the account doesn't own.
- **Blocks the report until corrected:**
  - an arrival event whose links add up to more than it delivered
  - an output whose fragments, from every source (traced, linked, pooled, placeholder), would exceed its value
  - a lot linked to an output that arrived before the event that delivered it. The tolerance is the block-timestamp drift of two hours (PLAN §7); an event tied to the output's txid is in time by definition.
- **Records:**
  - **Each link records** when it was made, and whether it came from a record (proposed) or from the user.
  - **Every link create, edit and removal** is in the change log, old to new (T-408).
  - **The audit trail lists** every link with its source, and marks any create, edit or removal made after its coin was spent.
- **Unconfirmed coins:** links, pool draws, placeholders and identifications are computed and stored only for outputs at or above the confirmation threshold (T-207). Mempool coins are shown as provisional and never written (T-506).
- **Reorgs (T-506):** links, placeholder lots and their resolutions, and pool draws on outputs of a reorged-out or replaced transaction go to the review queue. Nothing is carried over to a replacement silently.

### A wallet transaction
This applies to every event that draws from a self-custody account: self-transfer, deposit, spend, sell and gift given.

1. **One account per transaction.** If a transaction's owned inputs belong to several accounts, it blocks until the user states each account's part: its leaving outputs, its change, and its fee share. Each part then follows these rules on its own fragments. A self-transfer between accounts is stated as such.
2. **The user's part only.** In a transaction shared with others (PayJoin, CoinJoin, PLAN §3), the event states, for each output the user pays into or receives from, the user's sats in it and their role.
   - **The balance:** owned inputs + sats received from others = owned outputs + the user's leaving sats + the user's fee share + any coordinator fee (a small disposal). Otherwise the transaction blocks.
   - **Sats received from others** (a PayJoin receiver's payment) are an arrival: an acquisition linked to that output, or a pool draw, or a placeholder.
   - **Third-party inputs and outputs** carry none of the user's lots (BIP 78).
3. **The identification order:** the spent coins' fragments form one merged set, ordered by the account's method in force at the transaction (oldest first by default).
4. **Roles take fragments in a fixed order (owner):** disposals (spend, sell) first, then gifts, then deposits and transfers to another own account, then the change. Recommended, because it keeps a disposal oldest first whatever else the transaction does.
   - **Each role's fee share** is taken right after that role's outputs.
   - **Within a role,** outputs are filled one after another, each to exactly its value, in a canonical order: largest value first, then by the output script's bytes. Two identical outputs (the same value and script) fall back to vout; they are the same coin twice, so only which of them holds which lot differs.
5. **The fee, by role (ADR 0009):**
   - **The split:** the transaction's fee (the user's share, in a shared one) is split across roles in proportion to each role's sats. Rounding is in integer sats, with the remainder to the earlier role in step 4's order.
   - **A spend or sale:** the role's fee sats are part of the disposal. Its proceeds are the fair market value of the sats paid to the recipient, which is the gross sats leaving the user less the fee, net of any other costs. The fee is not subtracted again.
   - **A self-transfer or deposit, by default:** no disposal. The fee sats' basis goes to the role's first destination output (in the canonical order) that receives a fragment of the same lot, keeping that lot's date. If none does, it goes to the same lot's fragment in the change. If none is there either, it goes to the role's first arriving fragment.
   - **The "fee is a disposal" setting:** the role's fee sats are a small disposal.
   - **A gift given (owner):** the role's fee is a small disposal at its fair market value. Recommended, because BTC paid as a fee is property disposed of, and the gift itself keeps no gain or loss (ADR 0011).

**A worked example** (a test in the implementing PR): a 1 BTC coin holds L1 (0.4, oldest, basis $10,000) and L2 (0.6, basis $30,000). It pays 0.5 BTC to a merchant at $100,000/BTC, with 0.4999 change and a 0.0001 fee. Whether the change is vout 0 or vout 1:
- the payment gets L1 0.4 and L2 0.1
- the fee gets L2 0.0001
- the change keeps L2 0.4999
- **the disposal:** basis $10,000 + $5,005 = $15,005; proceeds $50,000; gain $34,995

**Further tests:**
- permuting all outputs leaves every figure unchanged
- many small fragments over unequal outputs leave every output exactly at its value
- a PayJoin receive balances

### The pool
- **The pool** is the wallet's lots on no coin.
- **When a lot enters the pool:** when it entered the account. That is:
  - an acquisition: its time
  - an unlinked withdrawal: the withdrawal's time, or its transaction's chain position if its txid is known
  - a gift received: its receipt
  
  A holding-period date is not an entry time.
- **Short coins draw at their arrival, in chain order** (block height, position in the block, then vout). This includes unspent coins, so holdings are right.
  - **Which lots count:** lots in the pool by then, within the two-hour tolerance, by the account's standing method.
  - **Arrival time:** a coin's arrival is its block time, or the user's override of it (PLAN §7).
- **The first result is kept.** When a confirmed coin's draw, or a confirmed spend's lots, is first computed, `services/` records it as an append-only identification row (PLAN §2). It is never overwritten.
  - **A later recompute that differs** (after a link, an unlink, or a back-dated acquisition) adds a superseding row, change-logged with the edit that caused it.
  - **The report shows the first and current results.**
  - **Lateness:** a draw by the standing method is never late. A changed draw is a correction of facts, shown as such.
- **If the pool is short,** the rest becomes a **placeholder lot of unknown basis** on that coin:
  - **Its provisional date:** the coin's arrival.
  - **It blocks reports** (ADR 0009, T-509), as for a withdrawal from an account without lots (PLAN §7).
  - **The user resolves it** by entering its date and basis, or by linking an arrival event. The change log records it.

### Methods over time (choices)
- **Each method record stores two times:** its effective-from time and when it was recorded. The method is oldest first, a recorded method, or whole-wallet FIFO. Records are kept in the change log.
- **It applies from its effective-from time** to transactions and pool draws.
- **Late flag:** one it reaches whose lots would differ is flagged late if the record was made after it. The report shows both results.
- **This narrows ADR 0021 §2's "never late":** an automatic choice is never late, except under a wallet method record made after the transaction it changes.
- **Under whole-wallet FIFO** the wallet's fragments are held by the account, not by coins (PLAN §2 allows either holder). Every event takes from them by FIFO, and coins arriving meanwhile make no pool draws.
  - **Switching to it:** every coin's fragments move to the account at the effective-from time.
  - **Switching back:** at the effective-from time, the coins then held receive the account's fragments, coins in chain order of arrival and fragments in the new method's order, each coin filled to its value. Holdings equal the account's sats, so every coin is filled exactly.

### The 2025 opening allocation (ADR 0008 §6)
- **It must equal the account's sats held on 2025-01-01.** Otherwise it blocks.
- **It replaces the account's fragment state as of that date.** Earlier links stay, and figures before 2025 still use them.
- **Which lot goes onto which coin:** the user may assign allocated lots to coins held then. Otherwise they fill the coins held on 2025-01-01 in chain order of arrival, lots in the allocation's stated method order (oldest first), each coin to its value.
- **The report lists the links the allocation overrides.**

### Late links: facts, not choices (owner)
- **Recommended:** a link is a fact about where coins arrived, not a choice of lots, so it is never flagged late. The chain is the identification (ADR 0008 §2).
  - **What stays visible:** the audit trail marks every link edit made after its coin was spent, and the report shows the first and current results.
  - **Why:** the app is used to rebuild history after the fact, so a "late" flag on every honest link would hide the ones that matter.
- **The alternative:** flag a link edit made after its coin was spent, when its result differs from the result without it, and show both.

### Records
- **This is a stated tax position** (ADR 0008 §2), printed with the reports. That includes the role order, the canonical output order, the fee rules, pool draws and placeholders.
- **PLAN §2, PLAN §7, and THREAT_MODEL T-408, T-506, T-508 and T-509** record these rules in this PR. The implementing PR adds tests for every case above.
- **#271's refusal is lifted.**
- **The engine's input:** each wallet event carries its owned inputs and outputs, with each output's role and the user's sats in it, from the chain cache and the event. `services/` builds them and stores the first-result rows; the engine stays pure. Each account carries its kind (`self_custody | custodial`).

### Consequences

- **Good:** self-custody figures follow the chain and the stated method, never wallet software. The release blocker on self-transfers goes.
- **Good:** impossible states block, and gaps, corrections and late method records are visible.
- **Bad:**
  - arrival events need their outputs linked
  - shared and multi-account transactions need the user's part stated
  - the engine gains a coin model with roles per output
- **Unchanged:** exchange accounts.

## References

- ADR 0008 §1, §2, §3 and §6; ADR 0009; ADR 0011; ADR 0021; ADR 0038; ADR 0040 (PR #273)
- PLAN §2, §3 (mixing), §7
- THREAT_MODEL T-207, T-408, T-501, T-506, T-508, T-509
- Treas. Reg. §1.1012-1(j); BIP 78 (PayJoin)
- #243 and #271 (the refusal); #238 and #273 (the late-choice replay)

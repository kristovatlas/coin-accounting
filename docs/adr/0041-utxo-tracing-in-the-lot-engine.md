---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009, 0021
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2; ADR 0009 where it describes self-custody movements (on a self-custody spend the network fee's sats are in the disposal at no proceeds, rather than reducing proceeds); ADR 0021 §1 ("never late") for wallet method records; and PLAN §2's data model. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied. ADR numbers 0039 and 0040 are taken by open PRs #268 and #273; the THREAT_MODEL version is reassigned at merge._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coins (UTXOs) a transaction spends identify the lots that leave.
  - **Inside merged coins:** lots leave oldest first, unless the wallet records another method.
  - **Strict whole-wallet FIFO:** a wallet can choose it instead.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **The owner:** self-custody transfers are extremely common, so tracing blocks release.
- **What isn't decided:** how lots get onto coins; which lots leave, and to which outputs; fees and proceeds; wallet lots on no coin; coins whose lots don't match them; transactions shared with others or spanning accounts; mode switches; the 2025 opening allocation. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **The stated method decides, never wallet software.** An output's position in the transaction changes no figure.
- **Every output holds exactly its value,** and every lot's sats are where the replay says, at every point in time: nothing doubled, nothing lost. Anything impossible blocks until it's corrected.
- **Facts versus choices.**
  - **Facts:** a link says where a purchase arrived.
  - **Choices:** a method, or an assignment of allocated lots to coins, says which lots leave. A choice must be timely (Treas. Reg. §1.1012-1(j), T-508).
- **Recomputable, auditable, per account.**
  - **Recomputed:** fragments come from events, links and the chain cache.
  - **Change-logged:** every user edit is in the append-only change log (T-408).
  - **Per account:** a coin's lots belong to the account holding it (ADR 0008 §1).

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

### Records are append-only, with app-set times
- **Each record carries a recorded-at time:** links, wallet method records, assignments of allocated lots to coins, placeholder resolutions and identification rows.
  - **Set by `services/`** from the system clock when the record is inserted. Never taken from the user, never edited.
  - **An edit is a new, superseding record** with its own time. A removal is a superseding "none" record. Each is change-logged, old to new (T-408).
- **The implementing migration** gives these tables T-408's append-only triggers, with tests. ADR 0038 protects them like the change log's.
- **The system clock is trusted.** This is an accepted limit, noted in T-508.
- **Who writes identification rows:** only the writer path (architecture §3).
  - **When:** the sync job, as a coin or spend reaches the confirmation threshold, and the API write whose edit caused a recompute.
  - **Readers** (reports, holdings) compute but never record.
  - **Uniqueness:** at most one first row exists per account and coin or spend (a unique index), with any number of superseding rows.
- **Reproducibility (T-503, PLAN §8):** a report's input hash covers these records and their supersession state, not only events and prices.

### Links (facts)
- **A link belongs to one arrival event.** It says that some of the sats that event delivered arrived in an output of the same account. Those sats are the event's lot, or the exchange fragments a withdrawal moved (chosen as ADR 0008 §3 says).
  - **Which fragment reaches which output:** the event's fragments, in the identification order, fill its linked outputs in the canonical output order (below).
  - **A withdrawal creates no lot:** its fragments keep their basis and dates. ADR 0009's one exception stays: a withdrawal from an exchange account with no lots creates one.
  - **The same lot can arrive in a wallet again later,** after a deposit and a second withdrawal. Each withdrawal is its own arrival event with its own links.
  - **Linked sats never enter the pool.** Only the unlinked rest of an event's sats does, so no other coin can draw them before their own coin arrives.
- **Refused:** a link to an output of another account, or one the account doesn't own.
- **Blocks the report until corrected:**
  - an arrival event whose links add up to more than it delivered
  - an output whose fragments, from every source (traced, linked, pooled, placeholder), would exceed its value
  - a lot whose sats across every holder (coins, the account, the pool) would exceed the lot
  - a lot linked to an output that arrived before the event that delivered it. The tolerance is the block-timestamp drift of two hours (PLAN §7); an event tied to a txid is exempt only if that transaction creates the linked output, and its recorded time is still checked against the transaction's block time with the same tolerance.
- **The audit trail** lists every link with its source, and marks any link record made after its coin was spent.
- **Unconfirmed coins:** links, pool draws, placeholders and identifications are computed and stored only for outputs at or above the confirmation threshold (T-207). Mempool coins are shown as provisional and never written (T-506).
- **Reorgs (T-506):** these go to the review queue, and nothing is carried over to a replacement silently:
  - links, placeholder lots and their resolutions, and pool draws on outputs of a reorged-out or replaced transaction
  - a reorged-out spend's identification rows, which are superseded (invalidated), not kept as its first result
  - opening-allocation assignments naming a coin that was reorged out or replaced

### A wallet transaction
This applies to every event that draws from a self-custody account: self-transfer, deposit, spend, sell and gift given.

1. **One account per transaction.** If a transaction's owned inputs belong to several accounts, it blocks until the user states each account's part: its leaving outputs, its change, and its fee share. Each part then follows these rules on its own fragments. A self-transfer between accounts is stated as such.
2. **The user's part only.** In a transaction shared with others (PayJoin, CoinJoin, PLAN §3), the event states, for each output the user pays into or receives from, the user's sats in it and their role.
   - **The balance:** owned inputs + sats received from others = owned outputs + the user's leaving sats + the user's fee share + any coordinator fee (a small disposal). Otherwise the transaction blocks.
   - **Sats received from others** (a PayJoin receiver's payment) are an arrival: an acquisition linked to that output, or a pool draw, or a placeholder.
   - **Third-party inputs and outputs** carry none of the user's lots (BIP 78).
   - **Only the user's sats are filled.** An output's fill amount, and its size in the canonical order, is the user's assigned sats in it, not its on-chain value. The other participant's share is outside this replay.
3. **The identification order:** the spent coins' fragments form one merged set, ordered by the account's method in force at the transaction (oldest first by default).
4. **Leaving roles take fragments first, in a fixed order (owner):** disposals (spend, sell), then gifts, then deposits and transfers to another own account. The change takes what is left. Recommended, because it keeps a disposal oldest first whatever else the transaction does.
   - **Each leaving role's fee share** is taken right after that role's outputs. In a shared transaction, the user's coordinator fee comes right after the network fee shares, before the change.
   - **The canonical output order:** within a role, outputs are filled one after another, each to exactly its value: largest value first, then by the output script's bytes.
   - **The change, and every output of a transaction with no leaving role,** are filled the same way, in the canonical order.
   - **Identical owned outputs** (the same value and script) form a group that holds its fragments together. Whichever is spent first takes the group's fragments in the identification order. No position decides.
   - **Identical leaving outputs** are filled as one combined output. Its fragments are then divided between their events in proportion to each event's sats, with the integer remainder to the event recorded first. Their proceeds, gifts and fee shares follow from that.
5. **The fee (ADR 0009):** the transaction's fee (the user's share, in a shared one) is split across the leaving roles only, in proportion to each one's sats. **The change never takes a fee share.**
   - **Rounding:** in integer sats, with the remainder to the earlier role in step 4's order.
   - **Within a role,** a share covering several events (two sales, say) is split across them by sats, the remainder to the earlier output in the canonical order.
   - **Fee treatment by role:**
     - **A spend or sale:** the role's fee sats are part of the disposal: their basis is in the disposal's basis, and they add nothing to its proceeds.
     - **A self-transfer or deposit, by default:** no disposal. For each lot in the fee share, its basis goes to the first destination output (in the canonical order) that receives a fragment of that lot, keeping the lot's date. Else it goes to that lot's fragment in the change. Else it goes to the role's first arriving fragment.
     - **The "fee is a disposal" setting:** the role's fee sats are a small disposal.
     - **A gift given (owner):** the role's fee is a small disposal at its fair market value. Recommended, because BTC paid as a fee is property disposed of, and the gift itself keeps no gain or loss (ADR 0011).
   - **A transaction with no leaving role** (a consolidation, or a send within the same account): the network fee, then any coordinator fee, take fragments first in the identification order, before the outputs. The fee is handled as a self-transfer fee. By default, each lot's fee basis goes to that lot's fragment in the outputs (else the first output's first fragment); with the setting, it is a small disposal.
   - **The basis may cross lots.** Where a lot's fee basis has nowhere of its own lot to go, it joins another lot's fragment, which keeps its own date. This is part of the stated tax position.
6. **Proceeds (26 U.S.C. §1001(b), ADR 0009):** the amount realized, net of costs other than the network fee. The network fee is not subtracted again: its sats are in the disposal at no proceeds.
   - **A sell:** the money and the FMV of any property received, as recorded on the event, less costs.
   - **A spend:** the FMV of the goods or services received, as recorded on the event, less costs. If none is recorded, the FMV of the BTC paid to the recipient is used, and the report says so.

**A worked example** (a test in the implementing PR):
- **The coin:** 1 BTC, holding L1 (0.4, oldest, basis $10,000) and L2 (0.6, basis $30,000).
- **The spend:** 0.5 BTC for goods recorded at $50,000, with 0.4999 change and a 0.0001 fee.

Whether the change is vout 0 or vout 1:
- the payment gets L1 0.4 and L2 0.1
- the fee gets L2 0.0001
- the change keeps L2 0.4999
- **the disposal:** basis $10,000 + $5,005 = $15,005; proceeds $50,000; gain $34,995

**Further tests:**
- permuting all outputs leaves every figure unchanged
- many small fragments over unequal outputs leave every output exactly at its value
- a consolidation's fee, under both settings
- a PayJoin receive balances
- identical outputs hold their lots as a group, and identical leaving outputs split by event
- permuting a transaction's outputs leaves its pool draws unchanged
- a PayJoin send fills only the user's sats
- a consolidation's fee takes fragments first, under both settings

### The pool
- **The pool** is the wallet's lots on no coin.
- **When a lot enters the pool:** when it entered the account. That is:
  - an acquisition: its time
  - an unlinked withdrawal: the withdrawal's time, or its transaction's chain position if its txid is known
  - a gift received: its receipt
  
  A holding-period date is not an entry time.
- **Short coins draw at their arrival, in chain order:** by block height, then position in the block. Within one transaction, the user's outputs follow the canonical order (largest first, then script, identical outputs as a group), never vout. This includes unspent coins, so holdings are right.
  - **Position in the block** is stored with each cached transaction, so the order can be replayed offline.
  - **Which lots count:** lots in the pool by then, within the two-hour tolerance, by the account's standing method.
  - **Arrival time:** a coin's arrival is its block time. For pool purposes, a user override (PLAN §7) can't move it by more than that tolerance.
  - **The report shows each pool draw** with a note naming the coin.
- **Leftover pool lots:** pool sats that remain while every coin of the wallet is fully covered show as a warning on the report: a duplicate or wrong acquisition, or an unrecorded disposal.
- **The first result is kept.** When a confirmed coin's draw, or a confirmed spend's lots, is first computed, `services/` records an identification row (PLAN §2).
  - **A later recompute that differs** adds a superseding row, change-logged with the edit that caused it. The edit could be a link, an unlink, a back-dated acquisition or an arrival override.
  - **The report shows the first and current results.**
  - **Lateness:** a draw by the standing method is never late. A changed draw caused by a fact (a link, an unlink, an acquisition, an arrival override) is a correction of facts, shown as such. A change caused by a method record is judged by the method rule below. The record's recorded-at time decides.
- **If the pool is short,** the rest becomes a **placeholder lot of unknown basis** on that coin:
  - **Its provisional date:** the coin's arrival.
  - **It blocks reports** (ADR 0009, T-509), as for a withdrawal from an account without lots (PLAN §7).
  - **The user resolves it** by linking an arrival event, or by creating a typed acquisition for it (a buy, income, a gift or an inheritance) with that type's fields (PLAN §7). A bare date and basis is not enough.

### Methods over time (choices)
- **Each method record has an effective-from time and a recorded-at time.** The method is oldest first, a recorded method, or whole-wallet FIFO.
- **It applies from its effective-from time** to transactions and pool draws.
- **Late flag:** one it reaches whose lots differ from those under the record in force before it (oldest first if none) is flagged late if the record was made after it. The report shows both results.
- **This narrows ADR 0021 §1's "never late":** an automatic choice is never late, except under a wallet method record made after the transaction it changes.
- **ADR 0021's automatic and strict-FIFO settings,** on a self-custody wallet, are recorded as method records like any other, with their own times and the late flag.
- **Under whole-wallet FIFO** the wallet's fragments are held by the account, not by coins (PLAN §2 allows either holder). Every event takes from them by FIFO, and coins arriving meanwhile make no pool draws.
  - **The account's fragments are reconciled with its sats after every event, net.** If its sats exceed its fragments, the shortfall becomes a placeholder lot of unknown basis held by the account, dated at that event. It blocks reports. If its fragments exceed its sats, that shows as the leftover warning.
  - **Switching to it:** every coin's fragments, and the pool, move to the account at the effective-from time.
  - **Switching back:** at the effective-from time, the coins then held receive the account's fragments, coins in chain order of arrival (the canonical order within a transaction) and fragments in the new method's order, each coin filled to its value. Fragments left over form the pool (with the leftover warning). A coin left short gets a placeholder.

### The 2025 opening allocation (ADR 0008 §6)
- **It must equal the account's sats held on 2025-01-01.** Otherwise it blocks.
- **It replaces the account's fragment state as of that date.** Earlier links stay, and figures before 2025 still use them.
- **Which lot goes onto which coin:** the user may assign allocated lots to coins held then. Otherwise they fill the coins held on 2025-01-01 in chain order of arrival (the canonical order within a transaction), lots in the allocation's stated method order (oldest first), each coin to its value. Under whole-wallet FIFO the allocation goes to the account.
- **An assignment is a choice, not a fact.** It is a record like a method record. It is flagged late when it differs from the default fill and was recorded after a coin it covers was spent. The report shows both results.
- **The report lists the links the allocation overrides.**

### Late links: facts, not choices (owner)
- **Recommended:** a link is a fact about where coins arrived, not a choice of lots, so it is never flagged late. The chain is the identification (ADR 0008 §2).
  - **What stays visible:** the audit trail marks every link record made after its coin was spent, and the report shows the first and current results.
  - **Why:** the app is used to rebuild history after the fact, so a "late" flag on every honest link would hide the ones that matter.
- **The alternative:** flag a link record made after its coin was spent, when its result differs from the result without it, and show both.

### Records
- **This is a stated tax position** (ADR 0008 §2), printed with the reports. That includes the role order, the canonical output order, the fee and proceeds rules, pool draws and placeholders.
- **PLAN §2, PLAN §7, and THREAT_MODEL T-408, T-506, T-508 and T-509** record these rules in this PR. The implementing PR adds tests for every case above.
- **#271's refusal is lifted.**
- **The engine's input:** each wallet event carries its owned inputs and outputs, with each output's role and the user's sats in it, from the chain cache and the event.
  - **`services/`** builds them and stores the append-only rows.
  - **The engine** stays pure.
  - **Each account** carries its kind (`self_custody | custodial`).

### Consequences

- **Good:** self-custody figures follow the chain and the stated method, never wallet software. The release blocker on self-transfers goes.
- **Good:** impossible states block, and gaps, corrections, leftovers and late choices are visible.
- **Bad:**
  - arrival events need their outputs linked
  - shared and multi-account transactions need the user's part stated
  - the engine gains a coin model with roles per output
- **Unchanged:** exchange accounts.

## References

- ADR 0008 §1, §2, §3 and §6; ADR 0009; ADR 0011; ADR 0021; ADR 0038; ADR 0040 (PR #273)
- PLAN §2, §3 (mixing), §7
- THREAT_MODEL T-207, T-408, T-501, T-506, T-508, T-509
- 26 U.S.C. §1001(b); Treas. Reg. §1.1012-1(j); BIP 78 (PayJoin)
- #243 and #271 (the refusal); #238 and #273 (the late-choice replay)

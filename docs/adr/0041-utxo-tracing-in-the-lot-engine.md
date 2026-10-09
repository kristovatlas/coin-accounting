---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2 and ADR 0009 where they describe self-custody movements. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied. ADR numbers 0039 and 0040 are taken by open PRs #268 and #273._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coins (UTXOs) a transaction spends identify the lots that leave.
  - **Inside merged coins:** lots leave oldest first, unless the wallet records another method.
  - **Strict whole-wallet FIFO:** a wallet can choose it instead.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **Deposits, spends, sales and gifts from a wallet** still use the account's order.
  - **The owner:** self-custody transfers are extremely common, so tracing blocks release.
- **What isn't decided:** how lots get *onto* coins, which lots leave when a transaction both pays out and keeps change, how fees fit, what happens to wallet lots on no coin, and what the engine does when a coin's lots don't match it. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **The stated method decides, never wallet software.** Output order (random, or BIP69-sorted) must not change any figure.
- **No guessing, no silent gaps.** Where the chain doesn't say which lot moved, a stated rule decides, and the report says so.
- **On time.** A link or a method recorded after the coins moved is flagged, as late choices are (T-508, ADR 0021).
- **Recomputable and per account.** Fragments are recomputed from events and the chain cache (PLAN §2), and a coin's lots belong to the account that holds it (ADR 0008 §1).

## Considered Options

**A. How lots reach a coin**
1. **Each acquisition into a wallet, and each withdrawal's moved lots, name the output(s) they arrived in** (txid:vout, with sats for each when one event funds several outputs). The import and the event editor propose links; the user confirms them.
2. **Match automatically** by amount and date. This is guessing; rejected.

**B. Wallet lots on no coin** (an opening balance, a buy before the app tracked the wallet)
1. **They form the wallet's unattached pool,** which a coin short of lots draws from (below).
2. **Refuse** a spend from a coin without lots. This blocks every user with a gap in their history; rejected.

**C. Which lots leave in a transaction with change**
1. **The lots leave in the identification order,** over all the transaction's owned inputs as one merged set: oldest first, or the wallet's recorded method. They go first to the outputs that leave the wallet, then to the fee, then to the owned change. Output order only breaks ties between outputs of the same role.
2. **Fill outputs in vout order.** The figures would depend on where the wallet put the change; rejected.

## Decision Outcome

Proposed: **A1, B1, C1**, with these rules.

### Coins and links
- **A link** attaches a lot (or part of one) to an output of the same account. An acquisition, or a withdrawal's moved lots, can name several outputs, with sats for each.
- **A withdrawal creates no lot:** the exchange fragments it moves (chosen as for any exchange withdrawal, ADR 0008 §3) attach to the outputs it names, keeping their basis and dates. The only exception stays ADR 0009's: a withdrawal from an exchange account with no lots creates one for its sats.
- **A link is refused** if the output isn't owned by that account, or is already fully linked.
- **Over-covered output:** if an output's linked lots hold more sats than it does, the report is blocked until it's resolved. A common cause is an exchange withdrawal entered gross of its fee; that fee is a small disposal on the exchange side (ADR 0009).
- **Date check:** a lot acquired after its output's transaction confirmed gets a warning.
- **Each link records when it was made and whether the app proposed it.** A link made or changed after its coin was spent is flagged late (T-508), and the report shows it.
- **Reorgs:** links on outputs of a reorged-out or replaced transaction go to the review queue (T-506).

### A wallet transaction
This applies to every event that draws from a self-custody account: self-transfer, deposit, spend, sell and gift given.
1. **Its owned inputs' fragments are one merged set.** Third-party inputs (a PayJoin or CoinJoin, PLAN §3) carry none. Order: the wallet's method in force at the transaction (oldest first by default).
2. **Leaving first:** the outputs that leave the wallet (the payment, the deposit, the gift's recipient) take fragments first, in that order. Several leaving outputs are filled in vout order. Each leaving output's lots are disposed (spend, sell), moved to the exchange account (deposit), or given (gift; no gain or loss, ADR 0011).
3. **The fee comes next, by ADR 0009's roles:**
   - **On a spend or sale:** the fee sats are part of the disposal. Proceeds are net of the fee.
   - **On a self-transfer or deposit, by default:** the fee sats' basis is added to the fragments of the same lot that arrive, keeping that lot's date. If that lot doesn't arrive, it goes to the next fragment that arrives, as the engine's dust rule does today.
   - **With the "fee is a disposal" setting:** the fee sats are a small disposal, taken in the identification order.
   - **A transaction with outputs of several roles** splits the fee by role, as ADR 0009 says.
4. **The owned change** takes the rest, in vout order among owned outputs.

The rule is set by role, not by output order. A worked example in the implementing PR's tests: a 1 BTC coin holding lot L1 (0.4, oldest) and L2 (0.6) pays 0.5 BTC to a merchant with 0.4999 change and a 0.0001 fee. Whether the change is vout 0 or vout 1:
- the payment gets L1 0.4 and L2 0.1
- the fee gets L2 0.0001
- the change keeps L2 0.4999

### Coins short of lots, and the pool
- **The pool:** wallet lots on no coin.
- **A short coin draws from the pool:** a coin whose fragments don't cover it (the chain shows more sats) takes the rest from its own account's pool, by the wallet's standing method.
- **Which pool lots count:** only lots acquired at or before the coin's arrival (its funding transaction's time).
- **The record:** each draw writes an identification with the standing method, never late (ADR 0021). The report and the audit trail show it, with a note naming the coin.
- **If the pool is short:** the rest becomes a **placeholder lot of unknown basis** on that coin. Its provisional date is the coin's arrival. It is a blocking condition (ADR 0009, T-509), as for a withdrawal from an account without lots (PLAN §7).
- **Resolving a placeholder:** the user enters its date and basis, or links an acquisition. The change log records it.
- **Inputs from several accounts:** each owned input draws only from its own account's pool and fragments. Fragments move between accounts only by the event's roles (a deposit to an exchange; a self-transfer to another own account).

### Methods over time
- **Each wallet's method has an effective-from time:** oldest first, a recorded method, or whole-wallet FIFO. It is a `tax_account` setting, recorded in the change log.
- **Under whole-wallet FIFO** the wallet's fragments are held by the account, not by its coins (PLAN §2 allows either holder), and every event takes from them by FIFO, as the engine does today. Coins then carry no fragments.
  - **Switching a wallet to whole-wallet FIFO** moves every coin's fragments to the account at that time.
  - **Switching back to coin tracing** puts the account's fragments in the pool; coins received afterwards carry their linked lots, and coins already held draw from the pool.
- **It applies only to transactions after that time.**
- **A change that would alter an earlier transaction's lots** is flagged as a late identification (T-508, ADR 0021), with both results shown.

### The 2025 opening allocation
- **It replaces a wallet's fragments as of 2025-01-01** (ADR 0008 §6).
- **Where the allocated lots go:** onto the coins the user names, or into the pool. Links made before 2025 on that wallet are dropped, with a warning.

### Records
- **This is a stated tax position** (ADR 0008 §2), printed with the reports. That includes the leaving-first rule, the fee rule, pool draws and placeholders.
- **THREAT_MODEL T-508 and T-509 and PLAN §7** record these rules in this PR. The implementing PR adds tests for every case above.
- **#271's refusal is lifted.**
- **The engine's input:** each wallet event carries its owned inputs and outputs with their roles, from the chain cache. `services/` builds them; the engine stays pure. Each account carries its kind (`self_custody | custodial`).

### Consequences

- **Good:** self-custody figures follow the chain and the stated method, not wallet software. The release blocker on self-transfers goes.
- **Good:** gaps and late links are visible: pool notes, placeholders that block reports, late-link flags.
- **Bad:** acquisitions and withdrawals into wallets need their outputs linked. Import and discovery propose links; the user confirms them.
- **Bad:** the engine gains a coin model, with roles per output. Exchange accounts are unchanged.

## References

- ADR 0008 §1, §2 and §6; ADR 0009; ADR 0011; ADR 0021; ADR 0040 (PR #273)
- PLAN §2 (`lot_fragment`, `identification`), §3 (mixing), §7
- THREAT_MODEL T-506, T-508, T-509
- #243 and #271 (the refusal); #238 and #273 (the late-choice replay)

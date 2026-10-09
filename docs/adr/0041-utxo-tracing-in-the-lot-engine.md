---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0009
---

# 0041: How the lot engine traces lots through self-custody coins

_This ADR amends ADR 0008 §2 and ADR 0009 where they describe self-custody movements. It decides how the already-decided rule, "lots follow the spent coin" (ADR 0008 §2, PLAN §7), is applied._

## Context and Problem Statement

- **The decided rule:** in a self-custody wallet, the coin (UTXO) a transaction spends identifies the lots that leave.
  - **Inside a coin that holds several lots:** they leave oldest first, unless the wallet records another method.
  - **Strict whole-wallet FIFO:** a wallet can choose it instead.
- **What the engine does today:** it takes lots by the wallet account's FIFO order, as for an exchange.
  - **#271** refuses self-transfers until tracing exists.
  - **Deposits and spends from a wallet** still use the account's order.
  - **The owner:** self-custody transfers are extremely common, so tracing blocks release.
- **What isn't decided:** how lots get *onto* coins, what happens to wallet lots that sit on no coin, and what the engine does when a spent coin's lots don't add up. Each changes tax figures, so it needs an ADR (ENGINEERING §4.1).

## Decision Drivers

- **No guessing.** Where the chain doesn't say which lot moved, a stated rule decides, and the warning says so.
- **Recomputable.** Lot fragments are recomputed from events and the chain cache (PLAN §2, `lot_fragment`).
- **One engine.** Exchange accounts keep their current behaviour unchanged.

## Considered Options

**A. How an acquisition reaches a coin**
1. **Each acquisition into a wallet names the output it arrived in** (txid:vout): a withdrawal from an exchange, a P2P buy, income, a gift received. The import and the event editor link them.
2. **Match acquisitions to outputs automatically** by amount and date. This is guessing; rejected.

**B. Wallet lots on no coin** (acquisitions entered without an output, such as an opening balance or a buy before the app tracked the wallet)
1. **They are the wallet's "unattached" pool.** A spend whose coin holds no lots, or not enough, takes the rest from the pool by the wallet's standing method (FIFO), and the warning says so.
2. **Refuse** a spend from a coin without lots. This blocks every user with a gap in their history.
3. **The 2025 opening allocation** (ADR 0008 §6, Rev. Proc. 2024-28) assigns them. That covers pre-2025 basis only.

**C. A spent coin whose lots don't cover it** (the chain shows more sats than the recorded lots)
1. **The rest comes from the unattached pool (B1).** If the pool is short, the rest is missing basis, a blocking condition as today (ADR 0009).
2. Create an unknown-basis lot silently. Rejected: it hides the gap.

**D. How lots spread over a transaction's outputs** (a self-transfer to several own outputs, or a spend with change)
1. **In output order:** the spent coins' fragments, oldest first, fill the owned outputs in their order in the transaction (vout); the fee comes from the last ones (ADR 0009's fee roles).
2. **Pro rata across outputs.** Rejected in ADR 0008 as not an IRS method.

## Decision Outcome

Proposed: **A1, B1, C1, D1.**

- **Coins:** an acquisition into a wallet names its output. A lot then sits on that coin, as a `lot_fragment` held by the outpoint.
- **A wallet transaction** (self-transfer, deposit, spend) takes the fragments of exactly the coins it spends, oldest lot first inside each coin, or the wallet's recorded method. The owned outputs receive them in vout order. A deposit's fragments enter the exchange account; a spend's are disposed.
- **Unattached lots** form the wallet's pool. A coin without enough lots draws the rest from the pool by the standing method, with a warning naming the coin. If the pool is short, the rest is missing basis (blocking).
- **Whole-wallet FIFO** (ADR 0008 §2): the wallet ignores coins and takes from all its fragments, oldest first, as today.
- **The engine's input:** each wallet event carries the coins it spends and the owned outputs it creates, from the chain cache (`services/` builds them; the engine stays pure). Each account carries its kind (`self_custody | custodial`).
- **#271's refusal is lifted**, and deposits and spends from wallets follow the coins too.
- **This is a stated tax position** (ADR 0008 §2), printed with the reports.

### Consequences

- **Good:** self-custody figures follow the chain, as decided in ADR 0008. The release blocker on self-transfers goes.
- **Good:** gaps are visible (pool warnings, missing basis) instead of silent.
- **Bad:** acquisitions into wallets need their output linked. Import and discovery can propose links; the user confirms them.
- **Bad:** the engine gains a coin model. Exchange accounts are untouched.

## References

- ADR 0008 §2 and §6; ADR 0009; ADR 0021; ADR 0040
- PLAN §2 (`lot_fragment`), §7
- THREAT_MODEL T-508
- #243 and #271 (the refusal), #238 and #273 (the replay)

---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0009: Lots, gifts, fees and blocking conditions

## Context and Problem Statement

The tax engine needs fixed rules for how lots are created, moved and used, and for fees. Some of these rules are settled law. Others are **tax positions** where the law is unclear. Positions must be visible to the user.

## Decision Outcome

**Lots are created only by acquisitions:**

| Event | Basis | Holding period |
|---|---|---|
| `buy` | cost + fees | from purchase |
| `p2p_buy` | fiat actually paid + fees | from purchase |
| `income` / mining | USD FMV at receipt time (also ordinary income) | from receipt |
| `gift_in` | dual basis: donor basis for gains; FMV at the gift, if lower, for losses; no gain or loss between the two. If the donor's basis is unknown, it is *unknown basis* until the user enters one; zero is offered as the conservative choice, and the choice is recorded | donor's date for the gain basis; the gift date for the loss basis |
| `inherit` | FMV at death, or the basis the estate reported (e.g. §2032 alternate valuation) | always long-term |
| `opening_allocation_2025` | see ADR 0008 | carried from the allocated lots |

**Movements never create lots:**
- Self-transfers follow the UTXO. Inside a UTXO that holds several lots, lots leave oldest first unless the wallet has a recorded method (ADR 0008).
- A deposit moves lots into the exchange account. A withdrawal moves the selected lots out (ADR 0008).
- A lot is created on withdrawal only when no lots are known for that account. The user then supplies the basis; otherwise it is *unknown basis*.

**Disposals and gifts:**
- `sell` and `spend` are taxable.
- A **`gift_out` is not a sale.** It removes lots with no gain or loss, keeps the donor's basis and date for the recipient's statement, and produces no Form 8949 row (ADR 0011).

**Fees:**
- Acquisition fees are added to basis.
- Disposal fees reduce proceeds. Proceeds are net of costs, as on Form 1099-DA.
- Network fees on a spend reduce its proceeds.
- An exchange's BTC withdrawal fee is a small disposal (default).
- When one transaction has owned and third-party outputs, its fee is split by the role of each output.
- **Tax position: the network fee on a self-transfer or deposit.**
  - Default: the fee's basis moves to the remaining coins, and there is no disposal.
  - Alternative (a setting): the fee sats count as a small taxable disposal.
  - The law does not clearly settle this, so both are offered. The chosen treatment is printed on every report.

**Blocking conditions:**
- Reports are blocked while any lot has unknown basis, or a relevant transaction is unconfirmed.
- The user resolves each condition explicitly, and the change log records the resolution.
- Late lot selections only warn (ADR 0008).

**Arithmetic:** USD with `Decimal`, integer sats; the engine is pure and deterministic.

**Out of scope for v1** (the UI warns when they seem to apply):
- lost or stolen coins
- forks and airdrops (planned next, see PLAN "Later")
- state taxes
- §1015(d) gift-tax basis adjustments
- wash sales: as of 2026-09, §1091 applies only to stock and securities, not BTC. This is re-checked each tax year.

### Consequences

- Good: settled rules are applied consistently, and positions are visible on every report.
- Bad: users with unusual facts (gift-tax adjustments, lost coins) must handle them outside the app for now.

## References

- PLAN §7; THREAT_MODEL T-501–T-502, T-507, T-509; IRC §§1014, 1015, 2032

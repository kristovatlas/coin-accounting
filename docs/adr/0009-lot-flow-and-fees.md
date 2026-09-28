---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0009: Lot flow across on-chain hops, and fees by role

## Decision Outcome

- Movements never create lots:
  - Self-transfers move lot fragments **pro-rata by sats**, carrying holding periods over.
  - Deposits move fragments into the custodial account.
  - Withdrawals move identified fragments out. A lot is created only when no fragments are known, with user-supplied basis; otherwise the basis is *unknown*.
- Acquisition basis:

  | Event | Basis |
  |---|---|
  | `buy` | cost + fees |
  | `p2p_buy` | fiat actually paid + fees |
  | `income` | USD FMV at receipt time (also ordinary income) |
  | `gift_in` | dual-basis rules, including the holding period on the loss basis |
  | `inherit` | FMV at death (long-term) |

- Fees by role:
  - acquisition fees add to basis
  - disposal fees reduce proceeds (net, as on 1099-DA)
  - network fees on self-transfers and deposits: the fee's basis carries over to the remaining sats (default), or optionally the fee counts as a small disposal
  - network fees on spends reduce proceeds
  - an exchange's BTC withdrawal fee is a small disposal by default
- All tax arithmetic in USD with `Decimal` and integer sats; the engine is pure and deterministic.
- Out of scope for v1: lost/stolen coins, forks/airdrops, state taxes; wash-sale rules don't apply to BTC.

## References

- PLAN §7; THREAT_MODEL T-501–T-502, T-507

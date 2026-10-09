---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
amends: 0008, 0021
---

# 0040: A late choice is judged against a replay without the late choices

_This ADR amends decision 4 of [ADR 0008](0008-per-account-basis-and-identification.md) and decision 2 of [ADR 0021](0021-standing-method-automatic.md); the rest of both stays in force._

## Context and Problem Statement

- **What a late choice is:** a choice of lots made after the sale or transfer may not count with the IRS, which may apply the account's standing order, or FIFO if there is none (Treas. Reg. §1.1012-1(j); ADR 0008 §4).
- **What the app does:** it warns about such a choice when its lots differ from the standing method's (ADR 0021 §2), and shows the standing method's figures. It still uses the user's choice.
- **What neither ADR says:** *which lots* the standing method is applied to.
- **How the engine did it until now:** on the lots as the user's earlier choices left them.
- **The trouble:** if an earlier choice was itself late, the IRS would disregard it too, and the lots the standing method would really have had are different. So the engine could miss a later late choice, judging it to match FIFO, or show figures the IRS wouldn't compute (#238).

## Considered Options

1. **The lots as they stand.** Judge each late choice against the lots the user's earlier choices left. Simple, but it compounds: one late choice changes the baseline for every later one.
2. **A full replay.** Judge each late choice against the standing method in a replay of the account's history where every late choice is disregarded.

## Decision Outcome

Chosen option: 2, the owner's decision on #238 (2026-10-09).

- **The replay** is the same events, with every late choice replaced by the standing method. It runs in step with the real run.
- **When a late choice is warned about:** its lots differ from what the standing method takes in the replay at that event.
- **The figures shown:** the replay's.
- **What still stands:** the user's choice is still used, and the warning is still warn-only (ADR 0008 §4).
- **An on-time choice the replay can't follow:** it names lots the replay doesn't hold in full, so the replay uses the standing method for all of it.
- **The replay always holds the same sats as the real run.** It stops wherever the two would part, and the result names that event:
  - **When the runs disagree on creating a lot:** one counts an account's lots as recorded and the other doesn't, so a withdrawal would create a lot for unrecorded sats in only one of them.
  - **When the replay can't apply an event:** for example, a fee that would use up the last sats of a gift's part.
  - **Every later late choice:** judged and figured on the user's lots, and marked as such. A late choice that matches the user's lots, but not what the replay would have held, is then not warned about.
  - **Reports must show a stopped replay,** with the event it stopped at (M7).
- **Lot ids:** the warning's lot ids are the replay's. Each figure carries its own basis and dates.
- **This is a stated tax position** (ADR 0009). It is printed with the reports.

### Consequences

- **Good:** a warning shows what the IRS would most likely compute if it disregarded every late choice, not one that depends on which earlier late choices the user made.
- **Bad:** the engine processes the events twice. It is a pure in-memory computation, so the cost is CPU only.
- **Bad:** a warning may name a lot id (`lot@transfer`) that exists only in the replay. A report shows each figure's basis and dates, not a lookup of the id.

## References

- ADR 0008 §4; ADR 0021 §2; ADR 0009 (stated tax positions)
- THREAT_MODEL T-508
- Treas. Reg. §1.1012-1(j)
- #238 (the owner's decision); PR #273

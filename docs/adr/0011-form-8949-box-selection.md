---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0011: Form 8949 rows and box selection

## Decision Outcome

- **Rows come only from taxable disposals:** `sell`, `spend` and fee disposals. **`gift_out` never produces a row** (ADR 0009).
- The box is chosen **per disposal** from (tax year, disposal channel, 1099-DA received?, basis reported?). The user can override it, and the reason is recorded.
- **Tax year ≤ 2024:** A/B/C and D/E/F. Digital assets without a 1099-B go in C/F.
- **Tax year ≥ 2025:** digital assets go in **G/H/I** (short-term) and **J/K/L** (long-term); C/F may not be used for them.
  - On-chain disposals → I/L.
  - Exchange sales → H/K by default (proceeds reported, basis not). This includes coins **deposited from outside the exchange**, which brokers treat as noncovered.
  - Exchange sales of **covered** coins → G/J. Covered means bought inside that exchange account on or after 2026-01-01, with basis reported by the broker.
- Where broker-reported values differ from the app's lots, the report flags the rows for review (ADR 0008).
- Rule sets are versioned by tax year in `tax/rules/`, citing the IRS instructions.

## References

- PLAN §8; THREAT_MODEL T-510; [2025 Form 8949 instructions](https://www.irs.gov/instructions/i8949); [Form 1099-DA instructions](https://www.irs.gov/instructions/i1099da)

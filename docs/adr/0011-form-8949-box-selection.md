---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0011: Form 8949 box selection per disposal and tax year

## Decision Outcome

- The box is chosen **per disposal** from (tax year, disposal channel, 1099-DA received?, basis reported?). The user can override it, and the reason is recorded.
- **≤ 2024:** A/B/C and D/E/F. Digital assets without a 1099-B go in C/F.
- **≥ 2025:** digital assets go in **G/H/I** (short-term) and **J/K/L** (long-term); C/F may not be used for them.
  - On-chain disposals → I/L.
  - 2025 exchange sells default to H/K (proceeds reported, basis not).
  - Sells of 2026+ acquisitions default to G/J when the broker reports basis.
- Rule sets are versioned by tax year in `tax/rules/`, citing the IRS instructions.

## References

- PLAN §8; THREAT_MODEL T-510; [2025 Form 8949 instructions](https://www.irs.gov/instructions/i8949)

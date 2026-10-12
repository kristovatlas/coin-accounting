---
status: proposed
date: 2026-10-11
deciders: repository owner (human), drafted by Claude Code
amends: 0039
---

# 0042: The price sources' terms are the user's to accept

_This ADR amends ADR 0039's licence section, "The API's own terms". ADR numbers 0040 and 0041 are taken by open PRs #278 and #276; this one is renumbered at merge if they take it first._

## Context and Problem Statement

- **ADR 0039** made the Coin Metrics downloader's landing depend on two things: a check of Coin Metrics' terms for the community API, and the owner's confirmation that they allow this use (automated access, possibly through Tor).
- **The terms couldn't be found:** on 2026-10-09 the terms URL redirected elsewhere. The downloader (#277) has since passed review.
- **The owner's view (2026-10-11):** the terms aren't the project's to clear. Using the program presupposes the user agrees to its data sources' terms. Development shouldn't wait on it, and the app shouldn't add it to the user's workflow.

## Considered Options

1. **The user accepts the sources' terms; the README says so and links them.** Chosen.
2. **Keep ADR 0039's condition:** hold the downloader until the terms are found and confirmed.
3. **Ask the user in the app** (a prompt or setting) before the first download.

## Decision Outcome

Chosen option: 1, the owner's decision (2026-10-11).

- **No landing condition:** ADR 0039's "the downloader can't land until … the owner has confirmed" condition is removed. So is the early-days methodology check that rode on it.
  - The stated tax position already says each day's price is Coin Metrics' daily `PriceUSD`.
  - Differences in its earliest years (2010 to 2013) are the source's, as for any day.
- **The README states it:** using the app means the user agrees to the terms of the data sources it downloads from:
  - Coin Metrics (community API and data licence)
  - Bitstamp
  - the European Central Bank

  It links each source's site.
- **Nothing in the app:** no prompt, setting or check. The download flow is unchanged.
- **Unchanged from ADR 0039:**
  - the CC BY-NC 4.0 licence and its non-commercial limit (already in the README)
  - attribution wherever Coin Metrics' prices are shown or exported (#272)
  - the F3 hosts and the fixed, record-independent requests

### Consequences

- Good: the downloader (#277) can land without a terms check the project can't complete.
- Good: the app's flow stays as it is; there's no click-through.
- Bad: if a source's terms forbid automated or Tor access, using the app breaches them, and that is the user's responsibility. The README says so plainly.

## References

- ADR 0039 §Licence; ADR 0007 (superseded by 0039)
- #277 (the downloader), #272 (attribution)
- Coin Metrics community data and licence: https://github.com/coinmetrics/data

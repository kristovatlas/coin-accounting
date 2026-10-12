---
status: proposed
date: 2026-10-11
deciders: repository owner (human), drafted by Claude Code
amends: 0039
---

# 0042: The price sources' terms are the user's to accept

_This ADR amends ADR 0039's licence section, "The API's own terms"._

## Context and Problem Statement

- **ADR 0039** made the Coin Metrics downloader's landing depend on two things: a check of Coin Metrics' terms for the community API, and the owner's confirmation that they allow this use (automated access, possibly through Tor).
- **The terms couldn't be found:** on 2026-10-09 the terms URL redirected elsewhere. The downloader (#277) has since passed review.
- **The owner's view (2026-10-11):** the terms aren't the project's to clear. Using the program presupposes the user agrees to its data sources' terms. Development shouldn't wait on it, and the app shouldn't add it to the user's workflow.

## Considered Options

1. **The user accepts the sources' terms; the README says so and links them.** Chosen.
2. **Keep ADR 0039's condition:** hold the downloader until the terms are found and confirmed. Rejected: the community API's terms couldn't be found, so this could block the downloader indefinitely.
3. **Ask the user in the app** (a prompt or setting) before the first download. Rejected: it adds a step to every user's workflow, which the owner doesn't want.

## Decision Outcome

Chosen option: 1, the owner's decision (2026-10-11).

- **No landing condition:** ADR 0039's "the downloader can't land until … the owner has confirmed" condition is removed.
- **The early-years check stays, as a follow-up:** ADR 0039 also asked to confirm what Coin Metrics' methodology says about 2010 to 2013, and to mention any difference in the stated tax position. That is a tax-accuracy check, separate from the terms. It is kept, but no longer holds up the downloader: #283 tracks it.
- **The README states it:** using the app means the user agrees to the terms of the data sources it downloads from:
  - Coin Metrics (community API and data licence)
  - Bitstamp
  - the European Central Bank

  It links each source's site. Coin Metrics' community-API terms couldn't be found (2026-10-09), so the README links its community data licence and says so.
- **If a source refuses Tor:** the README says to use the CSV price upload (T-304) rather than switching to a direct connection, which would show the source the user's IP (T-302).
- **Nothing in the app:** no prompt, setting or check. The download flow is unchanged.
- **Unchanged from ADR 0039:**
  - the CC BY-NC 4.0 licence and its non-commercial limit (already in the README)
  - attribution wherever Coin Metrics' prices are shown or exported (#272)
  - the F3 hosts and the fixed, record-independent requests

### Consequences

- Good: the downloader (#277) can land without a terms check the project can't complete.
- Good: the app's flow stays as it is; there's no click-through.
- Bad: if a source's terms forbid automated or Tor access, using the app breaches them, and that is the user's responsibility. The README says so plainly.
- Bad: for privacy (T-302), the app's requests use a common browser User-Agent, so a source can't tell they come from this app. A source whose terms ask automated clients to identify themselves isn't told. The README says this too.

## References

- ADR 0039 §Licence; ADR 0007 (superseded by 0039)
- #277 (the downloader), #272 (attribution), #283 (the early-years methodology check)
- Coin Metrics community data and licence: https://github.com/coinmetrics/data

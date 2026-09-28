---
status: proposed
date: 2026-09-27
deciders: repository owner (human), drafted by Claude Code
---

# 0007: Fiat prices by bulk, date-independent download

## Decision Outcome

- USD valuation uses the **daily volume-weighted average price per UTC day** from Bitstamp history: the full trade dump for history plus paginated daily OHLCV. Where trade-level data is missing it falls back to the typical price, and the method is recorded per row.
- Requests never depend on user records, and **all supported fiat pairs are always fetched**.
- Fetches happen only when the user clicks refresh, optionally over SOCKS5/Tor, with TLS verified.
- Every valuation can be overridden per event (e.g. an exchange fill or a W-2 value).
- Other fiat currencies are display only; all tax figures are in USD.
- A second cross-check source is deferred (it would add an outbound flow and needs a new ADR).

### Consequences

- Good: the price source learns nothing about the user's dates, holdings or residency.
- Bad: daily averages can differ from intraday values; overrides cover this. The source's availability must be checked in M5.

## References

- PLAN §6; THREAT_MODEL T-301–T-304, F3

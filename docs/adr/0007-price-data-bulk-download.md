---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0007: Fiat prices by bulk, date-independent download

## Decision Outcome

- **USD valuation** uses the daily volume-weighted average price per UTC day. Sources:
  - the full bitstampUSD trade dump from the **bitcoincharts** archive, for history
  - **Bitstamp's** paginated daily OHLCV API, for recent days
  - where there is no trade-level data, the daily typical price (H+L+C)/3

  The method is recorded per row.
- **Other fiat currencies (display only):** Bitstamp EUR/GBP pairs, or USD × **ECB** historical FX. All supported pairs are always fetched, so the download doesn't reveal the user's currency.
- **These hosts are the F3 egress destinations.** Adding or changing one needs an ADR and a threat-model update.
- Requests never depend on user records. Fetches happen only when the user clicks refresh, over TLS with certificate checks, and optionally via a local SOCKS5/Tor proxy. The first run asks the user to choose proxy or direct (T-302).
- **CSV upload** is the fallback when a source disappears (T-304, TB6).
- Every valuation can be overridden per event, e.g. an exchange fill or a W-2 value. All tax figures are in USD.
- A second cross-check source is deferred, because it adds an outbound flow.

### Consequences

- Good: the price sources learn nothing about the user's dates, holdings or residency.
- Bad: daily averages can differ from intraday values; overrides cover this. Source availability is checked in M5.

## References

- PLAN §6; THREAT_MODEL T-301–T-304, F3

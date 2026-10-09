---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
supersedes: 0007
---

# 0039: USD prices from Coin Metrics' daily reference rate

## Context and Problem Statement

ADR 0007 takes USD tax prices from the full bitstampUSD trade dump in the bitcoincharts archive, from which the app computes a daily volume-weighted average price (VWAP). Bitstamp's daily candles fill the days the dump doesn't cover, using the typical price (H+L+C)/3. On 2026-10-09 the owner couldn't reach the archive (`https://api.bitcoincharts.com/v1/csv/`), and the site has been neglected for years. A refresh fails if any download fails, so a dead archive stops USD prices entirely.

Computing a daily average from hourly candles was considered and rejected by the owner: an approximation of our own invites mistakes. The owner asked for a source that publishes a ready-made, weighted daily price for every day, history and ongoing.

## Considered Options

1. **Coin Metrics' daily reference rate (`PriceUSD`), from its free community API.** Daily from 2010-07-18 to the present, with no account or API key.
2. **Kraken's daily candles,** which carry a Kraken-computed VWAP. The API serves only the last 720 days. Kraken's history downloads either have no VWAP column (candles) or are about 26 GB of trades for every pair.
3. **Bitstamp's typical price (H+L+C)/3 alone.** Already built, but not a weighted price.
4. **A daily VWAP approximated from Bitstamp's hourly candles.** Rejected by the owner (above).

## Decision Outcome

Chosen option: 1, because Coin Metrics publishes the weighted price itself, by a documented method, across a vetted set of exchanges, for every day since 2010. The app only downloads it.

- **USD tax price.** Each UTC day's price is Coin Metrics' daily `PriceUSD` for BTC.
  - **Method:** a volume-weighted median of trades within each minute, then time-weighted over the hour around the day's calculation time. The trades come from constituent exchanges reviewed every quarter, and venues with too little volume, or more than 3% off the median, are excluded ([methodology](https://gitbook-docs.coinmetrics.io/market-data/reference-rates-overview/reference_rate)).
  - **Recording:** each row has method `reference` and source `coinmetrics:PriceUSD`.
  - **The time it prices:** this is a robust price at one moment, midnight UTC, not an average over the whole day. Which UTC day a timestamp prices follows Coin Metrics' documented convention. The downloader's PR states it, with a test.
  - **Rounding:** the value is rounded half-even to whole cents. A day that rounds below one cent is a gap.
  - **Tax position:** valuing each day at this rate is a stated tax position (ADR 0009), printed with the reports.
- **The request** is the fixed URL `https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=PriceUSD&frequency=1d&paging_from=start&page_size=…`.
  - **Pages:** each next page comes from the response's `next_page_url`. It is followed only if it has the same scheme, host and path, so the server can't send the app elsewhere.
  - **No user data:** nothing in any request depends on the user's records (T-301).
  - **Rate limit:** the community API allows 10 requests per 6 seconds per IP. The downloader stays well below that.
- **Bitstamp stays as the second source.** Its daily typical price is still fetched and compared with Coin Metrics' rate day by day (`prices.mismatches`). The comparison is a review flag, never a refusal, and it covers every day both have. On a day Coin Metrics has no value for, the typical price fills in, recorded with method `typical`, as today.
- **bitcoincharts is dropped.** `api.bitcoincharts.com` leaves the F3 hosts, and the dump parser is removed.
- **Display currencies are unchanged:** Bitstamp's EUR and GBP pairs, or USD × ECB rates. Every pair is always fetched.
- **The F3 hosts** are now:
  - `community-api.coinmetrics.io`
  - `www.bitstamp.net`
  - `www.ecb.europa.eu`

  Adding or changing one still needs an ADR and a threat-model update.
- **Unchanged from ADR 0007:**
  - prices are fetched only when the user clicks refresh, never from user records
  - TLS with certificate checks, and the optional local SOCKS5/Tor proxy (T-302)
  - the CSV upload as the fallback when a source disappears (T-304, TB6)
  - a per-event override for any valuation
  - all tax figures in USD
- **Licence.** Coin Metrics' community data is under [CC BY-NC 4.0](https://github.com/coinmetrics/data): free for non-commercial use, with attribution.
  - **Attribution:** the app credits Coin Metrics wherever it shows or exports these prices.
  - **Commercial use:** using the app commercially would need a different source or a licence. The README says so.

### Consequences

- Good: USD prices no longer depend on an archive that may be gone, and the weighting is done by a specialist, by a published method, across several exchanges, not by the app.
- Good: history starts in July 2010, about a year earlier than Bitstamp.
- Good: the comparison with Bitstamp keeps two independent sources for every day they share.
- Bad: one price at midnight UTC is not a whole-day average. On a volatile day it can differ from one by a few percent. Per-event overrides cover a day where that matters, such as income received at a known price.
- Bad: one more third party sees the app's requests (directly, or through Tor). The request set stays fixed and independent of the user's records.
- Bad: the licence is non-commercial.

## References

- PLAN §6; ADR 0007 (superseded); ADR 0009; THREAT_MODEL T-301–T-304, F3; architecture §5 (F3: "price/FX hosts")
- Coin Metrics, Reference Rate methodology: https://gitbook-docs.coinmetrics.io/market-data/reference-rates-overview/reference_rate
- Coin Metrics community data and licence: https://github.com/coinmetrics/data, https://gitbook-docs.coinmetrics.io/packages/coin-metrics-community-data
- Kraken OHLC API (720 entries): https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/

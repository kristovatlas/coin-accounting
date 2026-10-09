---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
supersedes: 0007
architecture_sha256: 34de4533df17d271f51261f206c0de073f5435789cb47720f33f7672fbbb0a88
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
  - **Method:** the daily rate is Coin Metrics' hourly reference rate sampled at midnight UTC. Each hourly value is a volume-weighted median of trades within each minute, averaged over the 61 minutes up to that moment with weights rising toward it. The trades come from constituent exchanges reviewed every quarter, and venues with too little volume, or more than 3% off the median, are excluded ([methodology](https://gitbook-docs.coinmetrics.io/coin-metrics-prices/coin-metrics-prices/reference-rate-metrics)).
  - **Which day it prices:** the value dated D is "the price as of the end of the day in UTC time" ([PriceUSD](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/market/price)). It prices day D at its close, midnight UTC at the end of D, the same moment that ends Bitstamp's daily candle for D. The downloader's PR pins this with a test.
  - **Recording:** each row has method `reference` and source `coinmetrics:PriceUSD`.
  - **Not a whole-day average:** it is a robust price at one moment, the day's close.
  - **Rounding:** the value is rounded half-even to whole cents. A day that rounds below one cent is a gap.
  - **Tax position:** valuing each day at this rate is a stated tax position (ADR 0009), printed with the reports.
- **The request** is the fixed URL `https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=PriceUSD&frequency=1d&paging_from=start&page_size=…`.
  - **Pages:** each next page's URL is rebuilt from the fixed URL plus only the response's `next_page_token`, which must match a strict pattern and length. The server's `next_page_url` is never followed as given, so it can't send the app elsewhere.
  - **Bounds:** the number of pages is capped (the days since 2010-07-18 divided by the page size, plus a margin). Each page's dates must be later than the last page's. One deadline and one byte limit cover the whole download. Breaking any of these fails the refresh (T-304).
  - **No user data:** nothing in any request depends on the user's records (T-301).
  - **Rate limit:** Coin Metrics documents a per-IP limit for the community API (10 requests per 6 seconds). The downloader stays well below it. Through Tor, a shared exit may still be refused (HTTP 429): the downloader retries a bounded number of times, with backoff, on a fresh circuit (new SOCKS credentials, T-302), so a Tor user isn't pushed to go direct.
- **Bitstamp stays as the second source.** Its daily typical price is still fetched and compared with Coin Metrics' rate day by day (`prices.mismatches`). The comparison is a review flag, never a refusal, and it covers every day both have. On a day Coin Metrics has no value for, the typical price fills in, recorded with method `typical`, as today.
- **bitcoincharts is dropped, first.** `api.bitcoincharts.com` leaves the F3 hosts, and the dump download and parser are removed, in the first PR after this ADR is accepted, before the Coin Metrics downloader lands. A neglected domain that lapsed could be taken over and serve a well-formed dump with a valid certificate, and its prices would win (T-303). Until the downloader lands, USD days use Bitstamp's typical price.
- **Freshness.** The refresh reports the reference rate's last priced day and flags it stale if that day is more than 7 days back (replacing the dump's flag, #246). M5 still checks that every F3 source is reachable.
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
  - **The API's own terms:** before the downloader lands, its PR checks that Coin Metrics' terms for the community API allow this use: automated access, possibly through Tor.

### Consequences

- Good: USD prices no longer depend on an archive that may be gone, and the weighting is done by a specialist, by a published method, across several exchanges, not by the app.
- Good: history starts in July 2010, about a year earlier than Bitstamp.
- Good: the comparison with Bitstamp keeps two independent sources for every day they share.
- Bad: one price at midnight UTC is not a whole-day average. On a volatile day it can differ from one by a few percent. Per-event overrides cover a day where that matters, such as income received at a known price.
- Bad: one more third party sees the app's requests (directly, or through Tor). The request set stays fixed and independent of the user's records.
- Bad: the licence is non-commercial.
- Kept on purpose: a refresh is still all-or-nothing (T-304). If any host fails, nothing is stored, the cached prices stay, and the CSV upload remains the fallback. This ADR changes the source, not that rule.

## References

- PLAN §6; ADR 0007 (superseded); ADR 0009; ADR 0014; THREAT_MODEL T-301–T-304, F3, §10 question 3; architecture §5 (F3: "price/FX hosts") and §7
- Coin Metrics, PriceUSD (daily value as of the end of the UTC day): https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/market/price
- Coin Metrics, Reference Rate methodology: https://gitbook-docs.coinmetrics.io/market-data/reference-rates-overview/reference_rate
- Coin Metrics community data and licence: https://github.com/coinmetrics/data, https://gitbook-docs.coinmetrics.io/packages/coin-metrics-community-data
- Kraken OHLC API (720 entries): https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/

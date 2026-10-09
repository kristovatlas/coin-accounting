---
status: proposed
date: 2026-10-09
deciders: repository owner (human), drafted by Claude Code
supersedes: 0007
architecture_sha256: 507e531d879d313ec54cfe4bc2e69ecb5c22775875bb3a98ca8c025438d55490
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
  - **Rounding:** the value is rounded half-even to whole cents, as every stored price is (`DailyPrice`). A day that rounds below one cent is a gap. On the earliest days this is coarse: from July 2010 into early 2011 BTC traded around $0.05 to $0.30, so a rounded price can be off by up to about 10%. That loss is part of the stated tax position below; an event acquired on those days can be valued exactly by a per-event override.
  - **Tax position:** valuing each day at this rate is a stated tax position (ADR 0009), printed with the reports.
- **The request** is the fixed URL `https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=PriceUSD&frequency=1d&paging_from=start&page_size=…`.
  - **Pages:** each next page's URL is rebuilt from the fixed URL plus only the response's `next_page_token`, which must match a strict pattern and length. The server's `next_page_url` is never followed as given, so it can't send the app elsewhere.
  - **Bounds:** the number of pages is capped (the days since 2010-07-18 divided by the page size, plus a margin). Each page's dates must be later than the last page's. One deadline and one byte limit cover the whole download. Breaking any of these fails the refresh (T-304).
  - **Parsing:** each value is a decimal string parsed exactly (no floats, architecture §2); each row's asset is `btc`; dates strictly increase within and across pages. Anything else fails the refresh (T-304).
  - **No user data:** nothing in any request depends on the user's records (T-301).
  - **Rate limit:** Coin Metrics documents a per-IP limit for the community API (10 requests per 6 seconds). The downloader stays well below it. An HTTP 429 is honoured, not dodged: the downloader waits as `Retry-After` says (bounded, and shorter than Tor's usual circuit lifetime) or backs off, retries a bounded number of times, and then fails the refresh (T-304). It never deliberately asks Tor for a new circuit or identity, or isolates the retry differently, to get past the limit. Tor may still rotate circuits on its own.
- **Bitstamp stays as the second source.** Its daily typical price is still fetched and compared with Coin Metrics' rate day by day (`prices.mismatches`). The comparison is a review flag, never a refusal, and it covers every day both have. On a day Coin Metrics has no value for, the typical price fills in, recorded with method `typical`, as today.
- **bitcoincharts is dropped, now.** `api.bitcoincharts.com` leaves the F3 hosts, and the dump download and parser are removed, in the same PR as this ADR, so the code never allows a host the binding decision has dropped. A neglected domain that lapsed could be taken over and serve a well-formed dump with a valid certificate, and its prices would win (T-303). Until the Coin Metrics downloader lands, USD days use Bitstamp's typical price, flagged stale if its last day is more than 7 days back; the cross-check comes with the downloader. This doesn't wait for the terms check below: the archive is already unreachable, so removing it loses nothing.
- **Freshness.** The refresh reports the reference rate's last priced day and flags it stale if that day is more than 7 days back (replacing the dump's flag, #246). M5 still checks that every F3 source is reachable.
- **Display currencies are unchanged:** Bitstamp's EUR and GBP pairs, or USD × ECB rates. Every pair is always fetched.
- **The F3 hosts** are now:
  - `community-api.coinmetrics.io`
  - `www.bitstamp.net`
  - `www.ecb.europa.eu`

  Adding or changing one still needs an ADR and a threat-model update.
- **Unchanged from ADR 0007:**
  - prices are fetched only when the user clicks refresh, never from user records
  - TLS with certificate checks, and the optional local SOCKS5/Tor proxy (T-302); the first run asks the user to choose proxy or direct
  - the CSV upload as the fallback when a source disappears (T-304, TB6)
  - a per-event override for any valuation
  - all tax figures in USD
- **Licence.** Coin Metrics' community data is under [CC BY-NC 4.0](https://github.com/coinmetrics/data): free for non-commercial use, with attribution.
  - **Attribution:** the app credits Coin Metrics wherever it shows or exports these prices.
  - **Commercial use:** using the app commercially would need a different source or a licence. The README says so (added with this ADR).
  - **The API's own terms:** the downloader can't land until its PR has checked, and the owner has confirmed, that Coin Metrics' terms for the community API allow this use: automated access, possibly through Tor. (On 2026-10-09 Coin Metrics' API-terms URL redirected to Talos's data pages, so the current terms weren't found.) If the terms forbid automated or Tor access, the downloader doesn't land, and a new ADR decides the source. The same check confirms what the methodology says about the earliest days (2010 to 2013), and the stated tax position mentions any difference.

### Consequences

- Good: USD prices no longer depend on an archive that may be gone, and the weighting is done by a specialist, by a published method, across several exchanges, not by the app.
- Good: history starts in July 2010, about a year earlier than Bitstamp.
- Good: the comparison with Bitstamp keeps two separately served sources for every day they share. Bitstamp may be one of the exchanges behind Coin Metrics' rate, so the comparison catches a broken or tampered feed, not a distorted Bitstamp market.
- Bad: one price at midnight UTC is not a whole-day average. On a volatile day it can differ from one by a few percent. Per-event overrides cover a day where that matters, such as income received at a known price.
- Bad: one more third party sees the app's requests (directly, or through Tor). The request set stays fixed and independent of the user's records.
- Bad: the licence is non-commercial.
- Kept on purpose: a refresh is still all-or-nothing (T-304). If any host fails, nothing is stored, the cached prices stay, and the CSV upload remains the fallback. This ADR changes the source, not that rule.

## References

- PLAN §6; ADR 0007 (superseded); ADR 0009; ADR 0014; THREAT_MODEL T-301–T-304, F3, §10 question 3; architecture §5 (F3: "price/FX hosts") and §7
- Coin Metrics, PriceUSD (daily value as of the end of the UTC day): https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/market/price
- Coin Metrics, Reference Rate methodology (accessed 2026-10-09): https://gitbook-docs.coinmetrics.io/coin-metrics-prices/coin-metrics-prices/reference-rate-metrics
- Coin Metrics community data and licence: https://github.com/coinmetrics/data, https://gitbook-docs.coinmetrics.io/packages/coin-metrics-community-data
- Kraken OHLC API (720 entries): https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/

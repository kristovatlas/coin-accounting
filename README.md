# coin-accounting

A local web app for tracking Bitcoin coin ownership, for US cost-basis and income estimates and for privacy decisions ("which of my coins are linked to which identity-knowing party"). It reads chain data only from your own Bitcoin Core node, over loopback. What it does and why is in [`PLAN.md`](PLAN.md); how it's built and checked is in [`docs/`](docs/).

It produces estimates and records, not tax advice.

## Price data

USD prices are to come from [Coin Metrics](https://coinmetrics.io/)' daily reference rate ([ADR 0039](docs/adr/0039-coin-metrics-usd-reference-rate.md)). Until that downloader lands, USD is Bitstamp's daily typical price, (high + low + close) ÷ 3. Coin Metrics' community data is licensed under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): it may be used **only for non-commercial purposes**, with attribution. Using this app commercially needs a different price source or a licence from Coin Metrics.

Once it does, Bitstamp's daily prices become the cross-check. The European Central Bank's reference rates convert prices into other currencies for display.

**Data sources' terms.** The app downloads prices from [Bitstamp](https://www.bitstamp.net/) and the [European Central Bank](https://www.ecb.europa.eu/), and from [Coin Metrics](https://coinmetrics.io/) once its downloader lands. Using the app means you agree to each source's terms, including for automated downloads and downloads over Tor. The app doesn't check this for you ([ADR 0042](docs/adr/0042-price-source-terms-are-the-users.md)).
- **Coin Metrics:** its community-API terms couldn't be found when this was written (October 2026); its [community data licence](https://github.com/coinmetrics/data) is above.
- **How the app asks:** for privacy, its requests use a common browser User-Agent, so a source can't tell they come from this app.
- **If a source refuses downloads over Tor,** upload prices from a CSV file instead of switching to a direct connection: a direct connection shows the source your IP address.

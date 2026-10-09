"""A price refresh: every source downloaded, parsed, combined and checked (PLAN §6, ADR 0007; THREAT_MODEL
T-301, T-303, T-304).

The user starts it (manual trigger); it runs as a job on the job worker (architecture §3). Every
source and every pair is always fetched, the same requests for every user (T-301). Nothing is
stored here: the result carries the prices, the review flags and each source's content hash, for the
storage slice to write in one transaction, or not at all if any source failed (T-304).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from coinacct.prices import DailyPrice, Gap, Mismatch, Outlier, check, combine, fetch, mismatches
from coinacct.prices.bitstamp import DUMP_SOURCE, OHLC_SOURCE
from coinacct.prices.fetch import Cancelled, Proxy
from coinacct.prices.fx import DISPLAY, ECB_SOURCE, to_display

# The trade dump is an archive a third party keeps up. If its last complete day is more than a week
# before the refresh's, it has likely stopped, and the recent USD days rest on Bitstamp's typical price
# alone, with no second source to compare (ADR 0007's source check, #246). A review flag, not a refusal.
STALE_DUMP: Final = timedelta(days=7)


@dataclass(frozen=True)
class Refreshed:
    """One refresh's result. `usd` is the tax series; `display` holds each display currency's series,
    Bitstamp's own pair where it traded and the ECB conversion of USD elsewhere. `hashes` maps each
    source to the SHA-256 of what was fetched (T-303). `mismatches` are the USD days on which the
    trade VWAP and Bitstamp's typical price disagree: review flags, like gaps and outliers.
    `dump_through` is the trade dump's last priced day (None if it priced none), and `dump_stale` says
    it ends more than STALE_DUMP before the refresh's first incomplete day."""

    usd: list[DailyPrice]
    display: dict[str, list[DailyPrice]]
    gaps: dict[str, list[Gap]]
    outliers: dict[str, list[Outlier]]
    mismatches: list[Mismatch]
    hashes: dict[str, str]
    dump_through: date | None
    dump_stale: bool


def refresh(
    proxy: Proxy | None, cancelled: threading.Event, today: Callable[[], date] | None = None
) -> Refreshed | None:
    """Download and check everything. Days from `today` (the UTC date) on are partial, so left out.
    Any failure raises FetchError or PriceError before anything is returned (T-304). A cancel (asked
    before each download and during every read) returns None, so the job worker records the job as
    cancelled, not failed."""
    if cancelled.is_set():  # before anything: not even the first connection
        return None
    try:
        return _refresh(proxy, cancelled.is_set, (today or _utc_today)())
    except Cancelled:
        return None


def _refresh(proxy: Proxy | None, stop: Callable[[], bool], complete_before: date) -> Refreshed:
    vwap, dump_hash = fetch.download_dump(complete_before, proxy, stop)
    typical: dict[str, list[DailyPrice]] = {}
    hashes = {DUMP_SOURCE: dump_hash}
    for currency in ("USD", *DISPLAY):
        typical[currency], hashes[f"{OHLC_SOURCE}:btc{currency.lower()}"] = fetch.download_ohlc(
            currency, complete_before, proxy, stop
        )
    rates, hashes[ECB_SOURCE] = fetch.download_ecb(proxy, stop)
    usd = combine(vwap, typical["USD"])
    converted = to_display(usd, rates)
    display = {c: _prefer(typical[c], [p for p in converted if p.currency == c]) for c in DISPLAY}
    gaps: dict[str, list[Gap]] = {}
    outliers: dict[str, list[Outlier]] = {}
    for currency, series in (("USD", usd), *display.items()):
        gaps[currency], outliers[currency] = check(series)
    through = vwap[-1].day if vwap else None
    stale = through is None or complete_before - through > STALE_DUMP
    return Refreshed(usd, display, gaps, outliers, mismatches(vwap, typical["USD"]), hashes, through, stale)


def _prefer(market: Sequence[DailyPrice], converted: Sequence[DailyPrice]) -> list[DailyPrice]:
    """By day: the pair's own market price where there is one, else the converted one."""
    best = {p.day: p for p in converted}
    best.update({p.day: p for p in market})
    return [best[d] for d in sorted(best)]


def _utc_today() -> date:
    return datetime.now(UTC).date()

"""A price refresh: every source downloaded, parsed and checked (PLAN §6, ADR 0039; THREAT_MODEL
T-301, T-303, T-304). The downloads are fakes in memory: no test reaches the network."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from coinacct.prices import DailyPrice, PriceError, fetch
from coinacct.prices import check as prices_check
from coinacct.prices.fetch import FetchError
from coinacct.services import prices as service
from coinacct.services.jobs import JobWorker, State

TODAY = date(2011, 8, 22)  # four complete days on Bitstamp's grid: one OHLC page per pair
DAY = 86_400
AUG18 = int(datetime(2011, 8, 18, tzinfo=UTC).timestamp())


def candle(day: int, price: str, volume: str = "1") -> dict[str, str]:
    t = str(AUG18 + day * DAY)
    return {"timestamp": t, "open": price, "high": price, "low": price, "close": price, "volume": volume}


def ohlc(pair: str, *candles: dict[str, str]) -> bytes:
    return json.dumps({"data": {"pair": pair, "ohlc": list(candles)}}).encode()


def cm(*prices: str | None) -> bytes:
    """Coin Metrics' page: one row per day from Aug 18, as the API sends them."""
    rows = [
        {"asset": "btc", "time": f"2011-08-{18 + n}T00:00:00.000000000Z", "PriceUSD": v}
        for n, v in enumerate(prices)
    ]
    return json.dumps({"data": rows}).encode()


REFERENCE = fetch.REFERENCE_URL.format(end="2011-08-21")  # the day before TODAY


def ecb(*rows: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("eurofxref-hist.csv", "Date,USD,JPY,GBP,\n" + "".join(rows))
    return out.getvalue()


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bytes]]:
    files = {
        fetch.OHLC_URL.format(currency="usd", start=AUG18): ohlc(
            "BTC/USD", candle(0, "99"), candle(1, "99"), candle(2, "30"), candle(3, "33")
        ),
        fetch.OHLC_URL.format(currency="eur", start=AUG18): ohlc("BTC/EUR", candle(1, "18")),
        fetch.OHLC_URL.format(currency="gbp", start=AUG18): ohlc("BTC/GBP"),
        fetch.ECB_URL: ecb("2011-08-19,2.000000,1,1.000000,\n", "2011-08-18,2.000000,1,1.000000,\n"),
        # Aug 19 has no value (Bitstamp fills it); Aug 20's is a third above Bitstamp's (flagged)
        REFERENCE: cm("100.004", None, "40.005", "33.0149"),
    }
    asked: list[str] = []

    @contextmanager
    def fake(url: str, proxy: Any = None, **limits: Any) -> Iterator[io.BytesIO]:
        asked.append(url)
        yield io.BytesIO(files[url])

    monkeypatch.setattr(fetch, "open_url", fake)
    files["__asked__"] = asked  # type: ignore[assignment]  # the URLs requested, for the T-301 test
    yield files


def run(cancelled: threading.Event | None = None) -> service.Refreshed:
    got = service.refresh(None, cancelled or threading.Event(), today=lambda: TODAY)
    assert got is not None
    return got


def usd(day: int, price: str, method: str, source: str) -> DailyPrice:
    return DailyPrice(date(2011, 8, 18 + day), "USD", Decimal(price), method, source)  # type: ignore[arg-type]


def test_usd_is_the_reference_rate_with_bitstamp_filling_its_gaps(sources: dict[str, Any]) -> None:
    # ADR 0039: Coin Metrics' rate, rounded half to even to cents; Bitstamp's typical price on Aug 19
    assert run().usd == [
        usd(0, "100.00", "reference", "coinmetrics:PriceUSD"),
        usd(1, "99.00", "typical", "bitstamp:ohlc:btcusd"),
        usd(2, "40.00", "reference", "coinmetrics:PriceUSD"),
        usd(3, "33.01", "reference", "coinmetrics:PriceUSD"),
    ]


def test_days_where_the_two_usd_sources_part_are_flagged(sources: dict[str, Any]) -> None:
    # Aug 20: 40.00 against Bitstamp's 30.00 is a third apart; 100 v 99 and 33.01 v 33 are close
    assert [(m.day.day, m.reference, m.typical) for m in run().mismatches] == [
        (20, Decimal("40.00"), Decimal("30.00"))
    ]


def test_the_reference_rates_own_gaps_are_reported_though_bitstamp_fills_them(
    sources: dict[str, Any],
) -> None:
    got = run()  # Aug 19 has no reference value: Bitstamp prices it, and the gap is still reported
    # the fixture's rate starts in August 2011, not on 2010-07-18: that late start is a gap too
    assert [(g.first, g.last) for g in got.reference_gaps] == [
        (date(2010, 7, 18), date(2011, 8, 17)),
        (date(2011, 8, 19), date(2011, 8, 19)),
    ]
    assert got.gaps["USD"] == []


START, LATE = date(2010, 7, 18), date(2011, 8, 17)  # the rate's first day; the fixture's day before


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        # priced on the 18th and 20th: the late start, the 19th between, and the 21st at the end
        (("100", None, "40", None), [(START, LATE), (date(2011, 8, 19),) * 2, (date(2011, 8, 21),) * 2]),
        ((None, None, None, None), [(START, date(2011, 8, 21))]),  # nothing at all: every requested day
        (("100", "99", "40", "33"), [(START, LATE)]),  # complete in August: only the late start
    ],
)
def test_reference_gaps_cover_every_requested_day_to_the_last_complete_one(
    sources: dict[str, Any], prices: tuple[str | None, ...], expected: list[tuple[date, date]]
) -> None:
    sources[REFERENCE] = cm(*prices)
    assert [(g.first, g.last) for g in run().reference_gaps] == expected


def test_the_reference_rates_own_jumps_are_reported_though_a_fill_sits_between(
    sources: dict[str, Any],
) -> None:
    # 100 on the 18th, Bitstamp's 99 fills the 19th, 160 on the 20th: in the combined series the jump
    # is split by the fill (99 -> 160 is +62 %), but the rate's own 100 -> 160 must show as well
    sources[REFERENCE] = cm("100", None, "160", "161")
    got = run()
    assert [(o.day.day, o.previous, o.price) for o in got.reference_outliers] == [
        (20, Decimal("100.00"), Decimal("160.00"))
    ]


def test_a_current_reference_rate_is_not_stale(sources: dict[str, Any]) -> None:
    got = run()  # its last day is Aug 21; the refresh's first incomplete day is Aug 22
    assert (got.usd_through, got.usd_stale) == (date(2011, 8, 21), False)


@pytest.mark.parametrize(("today", "stale"), [(date(2011, 8, 28), False), (date(2011, 8, 29), True)])
def test_a_reference_rate_that_stops_early_is_flagged_stale(
    sources: dict[str, Any], today: date, stale: bool
) -> None:
    # its last day stays Aug 21: 7 days before the 28th is fine, 8 before the 29th is stale
    sources[fetch.REFERENCE_URL.format(end=(today - timedelta(days=1)).isoformat())] = sources[REFERENCE]
    got = service.refresh(None, threading.Event(), today=lambda: today)
    assert got is not None and (got.usd_through, got.usd_stale) == (date(2011, 8, 21), stale)


def test_a_stopped_reference_rate_is_stale_even_where_bitstamp_fills_in(sources: dict[str, Any]) -> None:
    # refreshing on Aug 27: Bitstamp's last day, Aug 21, is 6 days back and would pass; the rate's own
    # last day, Aug 18, is 9 back, so the series is stale although every day up to the 21st is priced
    sources[fetch.REFERENCE_URL.format(end="2011-08-26")] = cm("100", None, None, None)
    got = service.refresh(None, threading.Event(), today=lambda: date(2011, 8, 27))
    assert got is not None
    assert [p.method for p in got.usd] == ["reference", "typical", "typical", "typical"]
    assert (got.usd_through, got.usd_stale) == (date(2011, 8, 18), True)


def test_no_reference_day_at_all_is_stale(sources: dict[str, Any]) -> None:
    sources[REFERENCE] = cm(None, None, None, None)
    got = run()
    assert (len(got.usd), got.usd_through, got.usd_stale) == (4, None, True)


def test_the_price_hosts_are_adr_0039s() -> None:
    assert "api.bitcoincharts.com" not in fetch.HOSTS
    assert fetch.HOSTS == {"community-api.coinmetrics.io", "www.bitstamp.net", "www.ecb.europa.eu"}


def test_display_prefers_the_pairs_own_market_then_the_ecb_conversion(sources: dict[str, Any]) -> None:
    eur = {p.day.day: (p.price, p.method) for p in run().display["EUR"]}
    # Aug 19: Bitstamp's BTC/EUR traded; the 18th from USD / 2 (ECB); the 20th and 21st use the
    # 19th's rate, the latest within a week (33.01 / 2 = 16.505, half to even)
    assert eur == {
        18: (Decimal("50.00"), "fx"),
        19: (Decimal("18.00"), "typical"),
        20: (Decimal("20.00"), "fx"),
        21: (Decimal("16.50"), "fx"),
    }
    assert [p.method for p in run().display["GBP"]] == ["fx"] * 4  # no GBP trades: all converted


def test_every_source_and_pair_is_fetched_the_same_way_every_time(sources: dict[str, Any]) -> None:
    run()
    assert sources["__asked__"] == [
        fetch.OHLC_URL.format(currency="usd", start=AUG18),
        fetch.OHLC_URL.format(currency="eur", start=AUG18),
        fetch.OHLC_URL.format(currency="gbp", start=AUG18),
        fetch.ECB_URL,
        REFERENCE,
    ]  # T-301: nothing about the user's currency, records or dates; only the calendar


def test_each_source_has_its_content_hash(sources: dict[str, Any]) -> None:
    hashes = run().hashes
    assert set(hashes) == {
        "bitstamp:ohlc:btcusd",
        "bitstamp:ohlc:btceur",
        "bitstamp:ohlc:btcgbp",
        "ecb:eurofxref-hist",
        "coinmetrics:PriceUSD",
    }
    framed = b"0:%d\n" % len(sources[REFERENCE]) + sources[REFERENCE]  # its page number and length
    assert hashes["coinmetrics:PriceUSD"] == hashlib.sha256(framed).hexdigest()
    assert hashes["ecb:eurofxref-hist"] == hashlib.sha256(sources[fetch.ECB_URL]).hexdigest()
    page = sources[fetch.OHLC_URL.format(currency="usd", start=AUG18)]
    framed = f"{AUG18}:{len(page)}\n".encode() + page  # each page framed by its start and length
    assert hashes["bitstamp:ohlc:btcusd"] == hashlib.sha256(framed).hexdigest()


def test_gaps_and_outliers_are_reported_per_series(sources: dict[str, Any]) -> None:
    got = run()
    assert got.gaps == {"USD": [], "EUR": [], "GBP": []}
    # USD 99 -> 40 falls by more than a third; EUR 50.00 -> 18.00 too, but 18.00 -> 20.00 is a ninth
    assert [o.day.day for o in got.outliers["USD"]] == [20]
    assert [o.day.day for o in got.outliers["EUR"]] == [19]


def test_a_cancelled_refresh_returns_nothing_so_the_job_is_cancelled_not_failed(
    sources: dict[str, Any],
) -> None:
    stop = threading.Event()
    stop.set()
    assert service.refresh(None, stop, today=lambda: TODAY) is None
    assert sources["__asked__"] == []  # cancelled before the first download: nothing fetched


def test_a_cancel_during_a_download_also_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()

    def ohlc(*args: Any) -> Any:
        stop.set()
        raise fetch.Cancelled("www.bitstamp.net: the refresh was cancelled")

    monkeypatch.setattr(fetch, "download_ohlc", ohlc)
    assert service.refresh(None, stop, today=lambda: TODAY) is None


@pytest.mark.parametrize(
    "row",
    [
        "2011-08-19,2.000000,1,1.000000,\x0c\n",  # splitlines() would split it off and accept the file
        "2011-08-19,2.000000,\x0c1,1.000000,\n",  # in a column never parsed (JPY)
        "2011-08-19,2.000000,1\r,1.000000,\n",  # a lone carriage return
    ],
)
def test_a_control_character_anywhere_in_the_ecb_file_is_refused(sources: dict[str, Any], row: str) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("eurofxref-hist.csv", "Date,USD,JPY,GBP,\r\n" + row)
    sources[fetch.ECB_URL] = out.getvalue()
    with pytest.raises(FetchError, match="holds a control character"):
        run()


def test_a_refresh_cancelled_during_a_download_ends_cancelled_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = JobWorker()
    worker.start()
    inside = threading.Event()

    def ohlc(currency: str, complete_before: date, proxy: Any, cancelled: Any) -> Any:
        inside.set()
        while not cancelled():  # mid-download, until the user cancels
            time.sleep(0.01)
        raise fetch.Cancelled("www.bitstamp.net: the refresh was cancelled")

    monkeypatch.setattr(fetch, "download_ohlc", ohlc)
    job_id = worker.submit("prices", lambda cancelled: service.refresh(None, cancelled, today=lambda: TODAY))
    assert inside.wait(5)
    worker.cancel(job_id)
    for _ in range(500):  # the job's own end, before stop() would cancel everything anyway
        done = worker.job(job_id)
        if done is not None and done.state not in (State.QUEUED, State.RUNNING):
            break
        time.sleep(0.01)
    assert done is not None and done.state is State.CANCELLED
    worker.stop()


@pytest.mark.parametrize(
    ("url", "data", "error", "message"),
    [
        (fetch.ECB_URL, b"not a zip", FetchError, "valid zip"),
        (fetch.ECB_URL, b"PK\x05\x06" + b"\x00" * 18, FetchError, "valid zip"),  # empty: no member
        (fetch.OHLC_URL.format(currency="usd", start=AUG18), b"\xff", FetchError, "isn't ASCII"),
        (fetch.OHLC_URL.format(currency="usd", start=AUG18), b"{}", PriceError, "expected data"),
        (REFERENCE, b"\xff", FetchError, "isn't ASCII"),  # downloaded last: still all or nothing
        (REFERENCE, b"{}", PriceError, "expected data"),
    ],
)
def test_any_bad_source_fails_the_whole_refresh(
    sources: dict[str, Any], url: str, data: bytes, error: type[Exception], message: str
) -> None:
    sources[url] = data
    with pytest.raises(error, match=message):
        run()


def test_an_ecb_zip_with_another_file_too_is_refused(sources: dict[str, Any]) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("eurofxref-hist.csv", "Date,USD,GBP,\n")
        z.writestr("extra.txt", "x")
    sources[fetch.ECB_URL] = out.getvalue()
    with pytest.raises(FetchError, match="the one expected file"):
        run()


@pytest.mark.parametrize("bad", [candle(-1, "9"), candle(-1, "9", volume="0")])
def test_an_ohlc_page_answering_for_an_earlier_day_is_refused(sources: dict[str, Any], bad: Any) -> None:
    sources[fetch.OHLC_URL.format(currency="usd", start=AUG18)] = ohlc("BTC/USD", bad)
    with pytest.raises(PriceError, match="2011-08-17 is outside the requested page"):
        run()


def test_ohlc_pages_join_at_their_boundary_and_a_page_answering_for_later_days_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = AUG18 + 1000 * DAY
    pages = {
        fetch.OHLC_URL.format(currency="usd", start=AUG18): ohlc("BTC/USD", candle(999, "1")),
        fetch.OHLC_URL.format(currency="usd", start=second): ohlc(
            "BTC/USD", candle(1000, "2"), candle(1001, "3")
        ),
    }
    asked: list[str] = []

    @contextmanager
    def fake(url: str, proxy: Any = None, **limits: Any) -> Iterator[io.BytesIO]:
        asked.append(url)
        yield io.BytesIO(pages[url])

    monkeypatch.setattr(fetch, "open_url", fake)
    until = date(2011, 8, 18) + timedelta(days=1002)
    prices, digest = fetch.download_ohlc("USD", until, None, lambda: False)
    assert [(p.day - date(2011, 8, 18)).days for p in prices] == [999, 1000, 1001]  # once each, in order
    assert asked == list(pages)
    framed = b"".join(
        f"{s}:{len(pages[u])}\n".encode() + pages[u] for s, u in zip((AUG18, second), pages, strict=True)
    )
    assert digest == hashlib.sha256(framed).hexdigest()
    # a page answering with another page's days (a changed `start`) is refused, not trimmed, the
    # last page included: its range ends on the grid too
    pages[fetch.OHLC_URL.format(currency="usd", start=AUG18)] = ohlc("BTC/USD", candle(1000, "2"))
    with pytest.raises(PriceError, match="outside the requested page"):
        fetch.download_ohlc("USD", until, None, lambda: False)
    pages[fetch.OHLC_URL.format(currency="usd", start=AUG18)] = ohlc("BTC/USD", candle(999, "1"))
    pages[fetch.OHLC_URL.format(currency="usd", start=second)] = ohlc(
        "BTC/USD", candle(2000, "9", volume="0")
    )
    with pytest.raises(PriceError, match="outside the requested page"):
        fetch.download_ohlc("USD", until, None, lambda: False)


def test_the_pages_follow_bitstamps_grid_to_the_download_day() -> None:
    assert fetch.ohlc_pages(date(2011, 8, 18)) == []
    assert fetch.ohlc_pages(date(2011, 8, 19)) == [str(AUG18)]
    pages = fetch.ohlc_pages(date(2016, 1, 1))  # 1597 days: two pages of 1000
    assert pages == [str(AUG18), str(AUG18 + 1000 * DAY)]


def test_the_refresh_uses_the_utc_date_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[date] = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls(2026, 10, 8, 23, 59, 59, tzinfo=tz)  # a fixed instant: no race with midnight

    def ohlc(currency: str, complete_before: date, *args: Any) -> Any:
        seen.append(complete_before)
        raise FetchError("stop here")

    monkeypatch.setattr(service, "datetime", Clock)
    monkeypatch.setattr(fetch, "download_ohlc", ohlc)
    with pytest.raises(FetchError):
        service.refresh(None, threading.Event())
    assert seen == [date(2026, 10, 8)]


@pytest.mark.parametrize("how", ["encrypted", "bzip2", "patched", "strong"])
def test_an_ecb_zip_packed_an_unexpected_way_is_refused(sources: dict[str, Any], how: str) -> None:
    out = io.BytesIO()
    method = zipfile.ZIP_BZIP2 if how == "bzip2" else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(out, "w", compression=method) as z:
        z.writestr("eurofxref-hist.csv", "Date,USD,GBP,\n")
    data = bytearray(out.getvalue())
    flag = {"encrypted": 0x1, "patched": 0x20, "strong": 0x40}.get(how, 0)
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # the flag in the local and central headers
        at = data.index(sig) + (6 if sig == b"PK\x03\x04" else 8)
        data[at] |= flag
    sources[fetch.ECB_URL] = bytes(data)
    with pytest.raises(FetchError, match="encrypted or packed an unexpected way"):
        run()


def test_the_downloads_stop_once_cancelled_and_refuse_an_unknown_pair() -> None:
    with pytest.raises(fetch.Cancelled, match=r"www\.bitstamp\.net: the refresh was cancelled"):
        fetch.download_ohlc("USD", TODAY, None, lambda: True)
    with pytest.raises(fetch.Cancelled, match=r"www\.ecb\.europa\.eu: the refresh was cancelled"):
        fetch.download_ecb(None, lambda: True)
    with pytest.raises(fetch.Cancelled, match=r"community-api\.coinmetrics\.io: the refresh was cancelled"):
        fetch.download_reference(TODAY, None, lambda: True)  # the real open_url: before it connects
    with pytest.raises(FetchError, match="before Coin Metrics' first day"):
        fetch.download_reference(date(2010, 7, 18), None, lambda: False)
    with pytest.raises(FetchError, match="unsupported currency"):
        fetch.download_ohlc("JPY", TODAY, None, lambda: False)


def test_an_ecb_file_too_large_or_not_ascii_is_refused(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fetch.ECB_CSV_MAX
    monkeypatch.setattr(fetch, "ECB_CSV_MAX", 10)
    with pytest.raises(FetchError, match="larger than allowed"):
        run()
    monkeypatch.setattr(fetch, "ECB_CSV_MAX", real)  # only this: the fake downloads stay
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("eurofxref-hist.csv", b"Date,USD,GBP,\n\xff")
    sources[fetch.ECB_URL] = out.getvalue()
    with pytest.raises(FetchError, match="isn't ASCII"):
        run()


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        ((18, 19, 20, 21), []),  # from the first requested day to the last: no gap at either end
        ((19, 20, 21), [(18, 18)]),  # one day late: a one-day head gap
        ((18, 19, 20), [(21, 21)]),  # one day short: a one-day tail gap
        ((18, 21), [(19, 20)]),  # only between
    ],
)
def test_covering_gaps_are_exact_at_both_ends(days: tuple[int, ...], expected: list[tuple[int, int]]) -> None:
    series = [usd(d - 18, "1.00", "reference", "coinmetrics:PriceUSD") for d in days]
    inner, _ = prices_check(series)
    got = service._covering(inner, series, date(2011, 8, 18), date(2011, 8, 21))
    assert [(g.first.day, g.last.day) for g in got] == expected

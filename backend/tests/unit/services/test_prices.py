"""A price refresh: every source downloaded, parsed, combined and checked (PLAN §6, ADR 0007; THREAT_MODEL
T-301, T-303, T-304). The downloads are fakes in memory: no test reaches the network."""

from __future__ import annotations

import gzip
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


def ecb(*rows: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("eurofxref-hist.csv", "Date,USD,JPY,GBP,\n" + "".join(rows))
    return out.getvalue()


DUMP = gzip.compress(
    f"{AUG18},10,1\n{AUG18 + 60},12,1\n{AUG18 + DAY},20,1\n{AUG18 + 3 * DAY},40,1\n".encode()
)  # VWAPs: Aug 18 = 11.00, Aug 19 = 20.00; Aug 21 is the dump's last day, dropped as partial


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bytes]]:
    files = {
        fetch.DUMP_URL: DUMP,
        fetch.OHLC_URL.format(currency="usd", start=AUG18): ohlc(
            "BTC/USD", candle(0, "99"), candle(1, "99"), candle(2, "30"), candle(3, "33")
        ),
        fetch.OHLC_URL.format(currency="eur", start=AUG18): ohlc("BTC/EUR", candle(1, "18")),
        fetch.OHLC_URL.format(currency="gbp", start=AUG18): ohlc("BTC/GBP"),
        fetch.ECB_URL: ecb("2011-08-19,2.000000,1,1.000000,\n", "2011-08-18,2.000000,1,1.000000,\n"),
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


def test_a_refresh_combines_the_trade_average_with_the_typical_price(sources: dict[str, Any]) -> None:
    got = run()
    assert got.usd == [
        usd(0, "11.00", "vwap", "bitcoincharts:bitstampUSD"),  # (10 + 12) / 2: VWAP wins the day
        usd(1, "20.00", "vwap", "bitcoincharts:bitstampUSD"),
        usd(2, "30.00", "typical", "bitstamp:ohlc:btcusd"),  # no trades in the dump: the typical price
        usd(3, "33.00", "typical", "bitstamp:ohlc:btcusd"),  # the dump's partial last day is dropped
    ]


def test_display_prefers_the_pairs_own_market_then_the_ecb_conversion(sources: dict[str, Any]) -> None:
    eur = {p.day.day: (p.price, p.method) for p in run().display["EUR"]}
    # Aug 19: Bitstamp's BTC/EUR traded; the 18th from USD / 2 (ECB); the 20th and 21st use the
    # 19th's rate, the latest within a week
    assert eur == {
        18: (Decimal("5.50"), "fx"),
        19: (Decimal("18.00"), "typical"),
        20: (Decimal("15.00"), "fx"),
        21: (Decimal("16.50"), "fx"),
    }
    assert [p.method for p in run().display["GBP"]] == ["fx"] * 4  # no GBP trades: all converted


def test_every_source_and_pair_is_fetched_the_same_way_every_time(sources: dict[str, Any]) -> None:
    run()
    assert sources["__asked__"] == [
        fetch.DUMP_URL,
        fetch.OHLC_URL.format(currency="usd", start=AUG18),
        fetch.OHLC_URL.format(currency="eur", start=AUG18),
        fetch.OHLC_URL.format(currency="gbp", start=AUG18),
        fetch.ECB_URL,
    ]  # T-301: nothing about the user's currency, records or dates; only the calendar


def test_each_source_has_its_content_hash(sources: dict[str, Any]) -> None:
    hashes = run().hashes
    assert hashes["bitcoincharts:bitstampUSD"] == hashlib.sha256(DUMP).hexdigest()
    assert hashes["ecb:eurofxref-hist"] == hashlib.sha256(sources[fetch.ECB_URL]).hexdigest()
    page = sources[fetch.OHLC_URL.format(currency="usd", start=AUG18)]
    framed = f"{AUG18}:{len(page)}\n".encode() + page  # each page framed by its start and length
    assert hashes["bitstamp:ohlc:btcusd"] == hashlib.sha256(framed).hexdigest()


def test_gaps_and_outliers_are_reported_per_series(sources: dict[str, Any]) -> None:
    got = run()
    assert got.gaps == {"USD": [], "EUR": [], "GBP": []}
    # USD 11 -> 20 is a rise over half; EUR 5.50 -> 18.00 too, but 18.00 -> 15.00 falls only a sixth
    assert [o.day.day for o in got.outliers["USD"]] == [19]
    assert [o.day.day for o in got.outliers["EUR"]] == [19]


def test_days_where_the_two_usd_sources_disagree_are_flagged(sources: dict[str, Any]) -> None:
    # the VWAPs are 11 and 20; Bitstamp's typical price is 99 on both days. Aug 20 and 21 have no VWAP.
    got = run().mismatches
    assert [(m.day.day, m.vwap, m.typical) for m in got] == [
        (18, Decimal("11.00"), Decimal("99.00")),
        (19, Decimal("20.00"), Decimal("99.00")),
    ]


def test_a_cancelled_refresh_returns_nothing_so_the_job_is_cancelled_not_failed(
    sources: dict[str, Any],
) -> None:
    stop = threading.Event()
    stop.set()
    assert service.refresh(None, stop, today=lambda: TODAY) is None
    assert sources["__asked__"] == []  # cancelled before the first download: nothing fetched


def test_a_cancel_during_a_download_also_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()

    def dump(*args: Any) -> Any:
        stop.set()
        raise fetch.Cancelled("api.bitcoincharts.com: the refresh was cancelled")

    monkeypatch.setattr(fetch, "download_dump", dump)
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

    def dump(complete_before: date, proxy: Any, cancelled: Any) -> Any:
        inside.set()
        while not cancelled():  # mid-download, until the user cancels
            time.sleep(0.01)
        raise fetch.Cancelled("api.bitcoincharts.com: the refresh was cancelled")

    monkeypatch.setattr(fetch, "download_dump", dump)
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
        (fetch.DUMP_URL, b"not gzip", FetchError, "isn't a valid gzip"),
        (fetch.DUMP_URL, gzip.compress(b"\xff\n"), FetchError, "line 1 of the dump isn't ASCII"),
        (fetch.DUMP_URL, gzip.compress(b"1" * 300), FetchError, "longer than any trade"),  # no newline
        (fetch.DUMP_URL, gzip.compress(b"junk\n"), PriceError, "line 1"),
        (fetch.ECB_URL, b"not a zip", FetchError, "valid zip"),
        (fetch.ECB_URL, b"PK\x05\x06" + b"\x00" * 18, FetchError, "valid zip"),  # empty: no member
        (fetch.OHLC_URL.format(currency="usd", start=AUG18), b"\xff", FetchError, "isn't ASCII"),
        (fetch.OHLC_URL.format(currency="usd", start=AUG18), b"{}", PriceError, "expected data"),
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

    def dump(complete_before: date, *args: Any) -> Any:
        seen.append(complete_before)
        raise FetchError("stop here")

    monkeypatch.setattr(service, "datetime", Clock)
    monkeypatch.setattr(fetch, "download_dump", dump)
    with pytest.raises(FetchError):
        service.refresh(None, threading.Event())
    assert seen == [date(2026, 10, 8)]


def test_a_dump_that_unzips_past_its_limit_is_refused(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch, "DUMP_TEXT_MAX", 20)
    with pytest.raises(FetchError, match="unzips to more than allowed"):
        run()


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
    with pytest.raises(fetch.Cancelled, match=r"api\.bitcoincharts\.com: the refresh was cancelled"):
        fetch.download_dump(TODAY, None, lambda: True)
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

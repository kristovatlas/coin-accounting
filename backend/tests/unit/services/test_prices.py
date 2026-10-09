"""A price refresh: every source downloaded, parsed, combined and checked (PLAN §6, ADR 0007; THREAT_MODEL
T-301, T-303, T-304). The downloads are fakes in memory: no test reaches the network."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from coinacct.prices import DailyPrice, PriceError, fetch
from coinacct.prices.fetch import FetchError
from coinacct.services import prices as service

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
    return service.refresh(None, cancelled or threading.Event(), today=lambda: TODAY)


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
    assert hashes["bitstamp:ohlc:btcusd"] == hashlib.sha256(page).hexdigest()


def test_gaps_and_outliers_are_reported_per_series(sources: dict[str, Any]) -> None:
    got = run()
    assert got.gaps == {"USD": [], "EUR": [], "GBP": []}
    # USD 11 -> 20 is a rise over half; EUR 5.50 -> 18.00 too, but 18.00 -> 15.00 falls only a sixth
    assert [o.day.day for o in got.outliers["USD"]] == [19]
    assert [o.day.day for o in got.outliers["EUR"]] == [19]


def test_a_cancelled_refresh_stops_with_an_error(sources: dict[str, Any]) -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(FetchError, match="cancelled"):
        run(stop)


@pytest.mark.parametrize(
    ("url", "data", "error", "message"),
    [
        (fetch.DUMP_URL, b"not gzip", FetchError, "isn't a valid gzip"),
        (fetch.DUMP_URL, gzip.compress(b"\xff\n"), FetchError, "valid gzip of ASCII"),
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


def test_an_ohlc_page_holding_an_earlier_day_is_refused(sources: dict[str, Any]) -> None:
    sources[fetch.OHLC_URL.format(currency="usd", start=AUG18)] = ohlc("BTC/USD", candle(-1, "9"))
    with pytest.raises(FetchError, match="holds an earlier day"):
        run()


def test_the_pages_follow_bitstamps_grid_to_the_download_day() -> None:
    assert fetch.ohlc_pages(date(2011, 8, 18)) == []
    assert fetch.ohlc_pages(date(2011, 8, 19)) == [str(AUG18)]
    pages = fetch.ohlc_pages(date(2016, 1, 1))  # 1597 days: two pages of 1000
    assert pages == [str(AUG18), str(AUG18 + 1000 * DAY)]


def test_the_refresh_uses_the_utc_date_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[date] = []

    def dump(complete_before: date, *args: Any) -> Any:
        seen.append(complete_before)
        raise FetchError("stop here")

    monkeypatch.setattr(fetch, "download_dump", dump)
    with pytest.raises(FetchError):
        service.refresh(None, threading.Event())
    assert seen == [datetime.now(UTC).date()]


def test_the_downloads_stop_once_cancelled_and_refuse_an_unknown_pair() -> None:
    with pytest.raises(FetchError, match=r"www\.bitstamp\.net: the refresh was cancelled"):
        fetch.download_ohlc("USD", TODAY, None, lambda: True)
    with pytest.raises(FetchError, match=r"www\.ecb\.europa\.eu: the refresh was cancelled"):
        fetch.download_ecb(None, lambda: True)
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

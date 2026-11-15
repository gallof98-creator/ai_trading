"""Each data source against responses of the documented shape: normalisation, timestamps, limits, errors."""
import asyncio
from datetime import date

import httpx
import pandas as pd
import pytest

from app.datahub.http import SourceAuthError, SourceError, SourceNotFound
from app.datahub.lake import now_utc
from app.datahub.sources.alpaca import AlpacaBars
from app.datahub.sources.coingecko import CoinGeckoSource
from app.datahub.sources.crypto import CryptoVenue
from app.datahub.sources.fmp import FmpSource
from app.datahub.sources.fred import FredSource
from app.datahub.sources.sec import SecSource

from . import fakes

UTC = "UTC"


def run(coro):
    return asyncio.run(coro)


def ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=UTC)


async def _no_sleep(_seconds: float) -> None:
    return None


def with_client(source, handler, call):
    async def go():
        async with source.client(transport=httpx.MockTransport(handler), sleep=_no_sleep) as client:
            return await call(client)

    return run(go())


# ---------------------------------------------------------------- FRED / ALFRED
def test_fred_vintages_keep_every_published_figure_with_its_publication_time():
    api = fakes.Fred()
    source = FredSource(api.key)
    frame, notes = with_client(source, api, lambda c: source.observations(c, "CPIAUCSL"))
    assert api.calls[0]["realtime_start"] == "1776-07-04" and api.calls[0]["realtime_end"] == "9999-12-31" and api.calls[0]["file_type"] == "json"
    assert notes["pit"] == "vintage" and len(frame) == 5 and set(frame["availability"]) == {"vintage"}
    january = frame[frame["date"] == ts("2024-01-01")].sort_values("realtime_start")
    assert list(january["value"]) == [308.417, 309.685]  # first print and revision are both kept
    # Published on 13 February 2024: known from 18:00 New York that day (23:00 UTC in winter), not from 1 January.
    assert january["available_at"].iloc[0] == ts("2024-02-13 23:00") and january["available_at"].iloc[1] == ts("2024-03-12 22:00")
    assert january["realtime_end"].iloc[0] == ts("2024-03-11") and pd.isna(january["realtime_end"].iloc[1])  # still the current figure
    assert (frame["available_at"] > frame["date"]).all() and str(frame["date"].dtype) == "datetime64[us, UTC]"


def test_fred_daily_series_use_the_next_business_day_rule_and_drop_missing_values():
    api = fakes.Fred()
    source = FredSource(api.key)
    frame, notes = with_client(source, api, lambda c: source.observations(c, "DGS10", start=date(2024, 3, 1)))
    assert "realtime_start" not in api.calls[0] and api.calls[0]["observation_start"] == "2024-03-01"
    assert notes == {"pit": "lag", "rows_received": 4, "missing_values": 1} and len(frame) == 3  # "." is a missing value, not a zero
    by_date = frame.set_index("date")["available_at"]
    assert by_date[ts("2024-03-08")] == ts("2024-03-11 22:00")  # Friday's yield: Monday 18:00 New York (daylight time by then)
    assert by_date[ts("2024-03-13")] == ts("2024-03-14 22:00")
    assert set(frame["availability"]) == {"lag_rule"} and frame["realtime_end"].isna().all()


def test_fred_falls_back_to_the_lag_rule_when_vintages_are_refused_and_reports_bad_keys():
    api = fakes.Fred()
    api.refuse_vintages.add("CPIAUCSL")
    source = FredSource(api.key)
    frame, notes = with_client(source, api, lambda c: source.observations(c, "CPIAUCSL"))
    assert notes["pit"] == "lag" and "vintages unavailable" in notes["fallback"] and set(frame["availability"]) == {"lag_rule"}
    assert len(frame) == 4  # the current figure of each observation only
    with pytest.raises(SourceAuthError):
        FredSource("")
    wrong = FredSource("w" * 32)
    with pytest.raises(SourceError) as info:
        with_client(wrong, api, lambda c: wrong.observations(c, "DGS10"))
    assert "w" * 32 not in str(info.value)


def test_fred_reads_every_page():
    api = fakes.Fred()
    api.series["BIG"] = [{"realtime_start": "2026-01-01", "realtime_end": "2026-01-01", "date": str(d.date()), "value": "1.0"}
                         for d in pd.date_range("2020-01-01", periods=250)]
    source = FredSource(api.key)

    async def paged(client):
        rows, offset = [], 0
        while True:
            body = await client.get_json("series/observations", {"series_id": "BIG", "limit": 100, "offset": offset})
            rows += body["observations"]
            offset += len(body["observations"])
            if offset >= body["count"]:
                return rows

    assert len(with_client(source, api, paged)) == 250
    frame, _ = with_client(source, api, lambda c: source.observations(c, "BIG"))
    assert len(frame) == 250


# ---------------------------------------------------------------- SEC EDGAR
def test_sec_requires_a_declared_user_agent_and_sends_it():
    for bad in ("", "just a name"):
        with pytest.raises(SourceAuthError):
            SecSource(bad)
    api = fakes.Sec()
    source = SecSource("Research Desk desk@example.com")
    tickers = with_client(source, api, source.tickers)
    assert tickers["AAPL"] == {"cik": "0000320193", "title": "Apple Inc."}
    assert api.calls[0] == ("https://www.sec.gov/files/company_tickers.json", "Research Desk desk@example.com")


def test_sec_filings_carry_the_acceptance_time_read_conservatively():
    api = fakes.Sec()
    source = SecSource("Research Desk desk@example.com")
    frame = with_client(source, api, lambda c: source.filings(c, "320193", "aapl"))
    assert list(frame["form"]) == ["10-K", "10-Q", "10-Q"] and set(frame["cik"]) == {"0000320193"} and set(frame["ticker"]) == {"AAPL"}
    by_accession = frame.set_index("accession")["ts"]
    # "2024-08-01T18:03:35.000Z" is read as 18:03 New York (22:03 UTC), the later of the two possible readings.
    assert by_accession["0000320193-24-000081"] == ts("2024-08-01 22:03:35")
    assert by_accession["0000320193-24-000123"] == ts("2024-11-01 10:01:36")
    assert (frame["available_at"] == frame["ts"]).all() and frame["is_xbrl"].all()
    assert frame["filing_date"].iloc[0] == ts("2024-11-01")


def test_sec_facts_become_available_when_their_filing_was_accepted():
    api = fakes.Sec()
    source = SecSource("Research Desk desk@example.com")

    async def both(client):
        filings = await source.filings(client, "320193", "AAPL")
        return await source.facts(client, "320193", "AAPL", filings.set_index("accession")["ts"])

    facts = with_client(source, api, both)
    assert len(facts) == 5  # the fact with no value is dropped
    revenue = facts[(facts["concept"] == "Revenues") & (facts["period_end"] == ts("2023-12-30"))].sort_values("available_at")
    assert list(revenue["value"]) == [119575000000, 119600000000] and list(revenue["availability"]) == ["accepted", "filing_date_rule"]
    assert revenue["available_at"].iloc[0] == ts("2024-02-01 23:03:22")  # the 10-Q's acceptance time, the evening before its filing date
    assert revenue["available_at"].iloc[1] == ts("2025-01-31 23:00")  # filing not in the index: 18:00 New York on the filing date
    assert (revenue["duration_days"] == 90).all()
    assets = facts[facts["concept"] == "Assets"].iloc[0]
    assert assets["period_start"] == assets["period_end"] and assets["duration_days"] == 0  # an instant, not a period
    assert (facts["available_at"] > facts["period_end"]).all()
    with pytest.raises(SourceNotFound):
        with_client(source, api, lambda c: source.facts(c, "1"))


# ---------------------------------------------------------------- crypto venues (CCXT)
def test_crypto_bars_are_paged_to_the_end_and_tagged_with_their_venue():
    exchange = fakes.Exchange(history=2500, page=1000)
    venue = CryptoVenue("binance", exchange)
    start = ts("2023-11-14 22:14")
    frame, notes = run(venue.bars("BTC/USDT", "1m", start, start + pd.Timedelta(minutes=2499)))
    assert len(frame) == 2500 and frame["ts"].is_unique and frame["ts"].is_monotonic_increasing and notes["pages"] == 3
    assert frame["ts"].iloc[0] == start and set(frame["source"]) == {"binance"} and set(frame["symbol"]) == {"BTC/USDT"} and set(frame["timeframe"]) == {"1m"}
    assert [c[3] for c in exchange.calls] == [fakes.T0, fakes.T0 + 1000 * fakes.MINUTE, fakes.T0 + 2000 * fakes.MINUTE]  # each page resumes after the last bar
    assert venue.stats.requests == 3 and frame["received_at"].notna().all()
    window, _ = run(venue.bars("BTC/USDT", "1m", start + pd.Timedelta(minutes=10), start + pd.Timedelta(minutes=19)))
    assert len(window) == 10


def test_crypto_bars_report_a_venue_that_only_serves_recent_history():
    """Kraken's OHLC endpoint answers with its newest 720 candles whatever start is asked for: that must be visible, not silent."""
    exchange = fakes.Exchange(history=5000, page=720, recent_only=True)
    venue = CryptoVenue("kraken", exchange)
    frame, notes = run(venue.bars("BTC/USD", "1m", ts("2023-11-14 22:14")))
    assert len(frame) == 720 and frame["ts"].iloc[0] == ts("2023-11-14 22:14") + pd.Timedelta(minutes=4280)
    assert notes["history_truncated_at"] == frame["ts"].iloc[0].isoformat()


def test_crypto_bars_never_include_the_bar_still_forming():
    class Live(fakes.Exchange):
        async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            minute = int(now_utc().floor("min").timestamp() * 1000)
            return [[minute - 2 * fakes.MINUTE, 1, 2, 0.5, 1.5, 9], [minute - fakes.MINUTE, 1.5, 2, 1, 1.8, 7], [minute, 1.8, 1.9, 1.7, 1.85, 1]]

    frame, notes = run(CryptoVenue("binance", Live()).bars("BTC/USDT", "1m", now_utc() - pd.Timedelta(minutes=5)))
    assert len(frame) == 2 and notes["forming_dropped"] == 1 and (frame["ts"] + pd.Timedelta(minutes=1) <= frame["received_at"]).all()


def test_crypto_trades_order_book_and_errors():
    exchange = fakes.Exchange()
    venue = CryptoVenue("kraken", exchange)
    since = now_utc().floor("s") - pd.Timedelta(minutes=5)
    trades, _ = run(venue.trades("BTC/USD", since))
    assert len(trades) == 20 and trades["trade_id"].is_unique and set(trades["side"]) == {"buy", "sell"} and trades["ts"].iloc[0] == since
    book = run(venue.order_book("BTC/USD", 2))
    row = book.iloc[0]
    assert (row["best_bid"], row["best_ask"], row["levels"]) == (100.0, 100.5, 2) and row["bid_sz"] == [1.0, 2.0] and row["ask_px"] == [100.5, 101.0]
    assert row["ts_origin"] == "received" and row["ts"] == row["received_at"]  # the venue gave no timestamp: say so
    with pytest.raises(SourceNotFound):
        run(venue.bars("NOPE/USD", "1m", ts("2023-11-14 22:14")))
    run(venue.close())
    assert exchange.closed


def test_crypto_connection_is_keyless_spot_only_and_binance_uses_the_market_data_host():
    """Goes through the real CCXT classes with their HTTP layer replaced, to check which URLs are requested."""
    with_key = fakes.Exchange()
    with_key.apiKey = "abc"
    with pytest.raises(SourceError, match="keyless"):
        CryptoVenue("binance", with_key)

    async def go():
        venue = CryptoVenue("binance")
        urls = []

        async def fetch(url, method="GET", headers=None, body=None):
            urls.append(url)
            assert method == "GET" and not (headers or {}).get("X-MBX-APIKEY")
            if "exchangeInfo" in url:
                return {"timezone": "UTC", "serverTime": 1, "rateLimits": [], "symbols": [
                    {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "baseAssetPrecision": 8, "quotePrecision": 8,
                     "quoteAssetPrecision": 8, "orderTypes": [], "isSpotTradingAllowed": True, "isMarginTradingAllowed": False, "filters": [],
                     "permissions": ["SPOT"], "permissionSets": [["SPOT"]]}]}
            if "klines" in url:
                return [[fakes.T0, "100.0", "101.0", "99.0", "100.5", "12.5", fakes.T0 + 59999, "1250", 42, "6", "600", "0"]]
            if "depth" in url:
                return {"lastUpdateId": 5, "bids": [["100.0", "2"]], "asks": [["100.1", "3"]]}
            raise AssertionError(f"unexpected request {url}")

        venue.exchange.fetch = fetch
        try:
            bars, _ = await venue.bars("BTC/USDT", "1m", pd.Timestamp(fakes.T0, unit="ms", tz="UTC"), pd.Timestamp(fakes.T0, unit="ms", tz="UTC"))
            book = await venue.order_book("BTC/USDT", 5)
        finally:
            await venue.close()
        return venue, urls, bars, book

    venue, urls, bars, book = run(go())
    assert venue.exchange.apiKey in (None, "") and venue.exchange.secret in (None, "")
    assert urls and all(u.startswith("https://data-api.binance.vision/api/v3/") for u in urls)  # never the trading or account hosts
    assert (bars.iloc[0]["open"], bars.iloc[0]["close"], bars.iloc[0]["volume"]) == (100.0, 100.5, 12.5)
    assert book.iloc[0]["best_ask"] == 100.1


# ---------------------------------------------------------------- CoinGecko
def test_coingecko_history_and_venue_snapshot():
    now_ms = int(now_utc().timestamp() * 1000)
    hour = 3_600_000
    calls = []

    def api(request):
        calls.append((request.url.path, dict(request.url.params), request.headers.get("x-cg-demo-api-key")))
        if request.url.path.endswith("/market_chart"):
            stamps = [now_ms - 3 * hour, now_ms - 2 * hour, now_ms]  # the last point is "now", still moving
            return httpx.Response(200, json={"prices": [[t, 60000 + i] for i, t in enumerate(stamps)], "market_caps": [[t, 1.2e12] for t in stamps],
                                             "total_volumes": [[t, 3e10] for t in stamps]})
        return httpx.Response(200, json={"name": "Bitcoin", "tickers": [
            {"base": "BTC", "target": "USDT", "market": {"name": "Binance", "identifier": "binance"}, "last": 60010.0, "volume": 25000.0,
             "converted_volume": {"usd": 1.5e9}, "trust_score": "green", "bid_ask_spread_percentage": 0.01, "last_traded_at": "2026-11-14T10:00:00+00:00",
             "is_anomaly": False, "is_stale": False},
            {"base": "BTC", "target": "USD", "market": {"name": "Kraken", "identifier": "kraken"}, "last": 60020.0, "volume": 3000.0,
             "converted_volume": {"usd": 1.8e8}, "trust_score": "green", "bid_ask_spread_percentage": None, "last_traded_at": None},
            {"base": "BTC", "target": "EUR", "market": {}, "last": 1.0}]})

    source = CoinGeckoSource("demo-key")
    chart = with_client(source, api, lambda c: source.market_chart(c, "bitcoin", days=4000))
    assert calls[0] == ("/api/v3/coins/bitcoin/market_chart", {"vs_currency": "usd", "days": "365"}, "demo-key")  # capped at the free plan's year
    assert len(chart) == 2 and list(chart["price"]) == [60000, 60001] and set(chart["coin_id"]) == {"bitcoin"} and chart["market_cap"].notna().all()
    venues = with_client(source, api, lambda c: source.venues(c, "bitcoin"))
    assert list(venues["venue"]) == ["binance", "kraken"] and list(venues["pair"]) == ["BTC/USDT", "BTC/USD"]  # the entry without a venue is dropped
    assert venues["spread_pct"].iloc[0] == 0.01 and pd.isna(venues["spread_pct"].iloc[1]) and venues["ts"].nunique() == 1
    keyless = CoinGeckoSource("")
    with_client(keyless, api, lambda c: keyless.venues(c, "bitcoin"))
    assert calls[-1][2] is None and keyless.client().pacer.interval > source.client().pacer.interval  # no key: slower, never faster


# ---------------------------------------------------------------- Financial Modeling Prep
def test_fmp_profile_and_refusals():
    def api(request):
        q = dict(request.url.params)
        assert q["apikey"] == "fmp-key" and request.url.path == "/stable/profile"
        if q["symbol"] == "AAPL":
            return httpx.Response(200, json=[{"symbol": "AAPL", "companyName": "Apple Inc.", "marketCap": 3.4e12, "beta": 1.24, "sector": "Technology",
                                              "industry": "Consumer Electronics", "exchange": "NASDAQ", "currency": "USD", "country": "US",
                                              "fullTimeEmployees": "164000", "ipoDate": "1980-12-12", "cik": "0000320193", "isEtf": False}])
        if q["symbol"] == "PAID":
            return httpx.Response(200, json={"Error Message": "Exclusive Endpoint : This endpoint is not available under your current subscription"})
        return httpx.Response(200, json=[])

    source = FmpSource("fmp-key")
    row = with_client(source, api, lambda c: source.profile(c, "aapl")).iloc[0]
    assert (row["symbol"], row["sector"], row["employees"], row["market_cap"], row["cik"]) == ("AAPL", "Technology", 164000, 3.4e12, "0000320193")
    assert row["ts"] == now_utc().floor("D")
    with pytest.raises(SourceError, match="free plan"):
        with_client(source, api, lambda c: source.profile(c, "PAID"))
    with pytest.raises(SourceNotFound):
        with_client(source, api, lambda c: source.profile(c, "ZZZZ"))
    with pytest.raises(SourceAuthError):
        FmpSource("")


# ---------------------------------------------------------------- Alpaca intraday history
def test_alpaca_minute_bars_follow_pages_and_stay_behind_the_delayed_tape():
    calls = []

    def api(request):
        q = dict(request.url.params)
        calls.append((request.url.path, q, request.headers.get("apca-api-key-id")))
        if "page_token" not in q:
            return httpx.Response(200, json={"bars": {"SPY": [{"t": "2024-03-01T14:30:00Z", "o": 510.0, "h": 510.5, "l": 509.8, "c": 510.2, "v": 120000, "n": 900, "vw": 510.15}]},
                                             "next_page_token": "abc"})
        return httpx.Response(200, json={"bars": {"SPY": [{"t": "2024-03-01T14:31:00Z", "o": 510.2, "h": 510.9, "l": 510.1, "c": 510.7, "v": 80000, "n": 640, "vw": 510.5}]},
                                         "next_page_token": None})

    source = AlpacaBars("key-id", "secret")
    frame, notes = with_client(source, api, lambda c: source.bars(c, "SPY", "1m", ts("2024-03-01 14:30"), ts("2024-03-01 15:00")))
    path, params, key = calls[0]
    assert path == "/v2/stocks/bars" and key == "key-id" and params["timeframe"] == "1Min" and params["feed"] == "sip" and params["adjustment"] == "all"
    assert calls[1][1]["page_token"] == "abc" and notes["pages"] == 2
    assert list(frame["close"]) == [510.2, 510.7] and list(frame["trades"]) == [900, 640] and list(frame["vwap"]) == [510.15, 510.5]
    assert set(frame["source"]) == {"alpaca"} and "delayed" in frame["venue"].iloc[0]

    calls.clear()
    with_client(source, api, lambda c: source.bars(c, "SPY", "1m", now_utc() - pd.Timedelta(hours=2)))
    assert pd.Timestamp(calls[0][1]["end"]) <= now_utc() - pd.Timedelta(minutes=15)  # the free plan may not query the last 15 minutes
    empty, _ = with_client(source, api, lambda c: source.bars(c, "SPY", "1m", now_utc() - pd.Timedelta(minutes=5)))
    assert empty.empty  # everything asked for is still inside the delay: no request is sent
    calls.clear()
    with_client(source, api, lambda c: source.bars(c, "BTC/USD", "5m", ts("2024-03-01"), ts("2024-03-02"), asset="crypto"))
    assert calls[0][0] == "/v1beta3/crypto/us/bars" and "feed" not in calls[0][1] and calls[0][1]["timeframe"] == "5Min"
    with pytest.raises(SourceAuthError):
        AlpacaBars("key-id", "")

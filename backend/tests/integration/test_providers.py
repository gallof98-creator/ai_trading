"""Data-provider connector, tested against responses shaped like the vendor's documented API.
Nothing here contacts the real service."""
import json
import time
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.database.settings_store import UserSettings
from app.instruments.registry import make_equity
from app.market_data.providers import (DownloadJob, EodhdProvider, ProviderAuthError, ProviderError, ProviderRateLimited, SymbolNotFound)
from app.market_data.sources import synthetic_bars
from tests.conftest import VIEWER, login

KEY = "secret-key-ABCD"


def eod_rows(seed, n=700, split_at=None):
    f = synthetic_bars("1d", n, seed=seed, start="2021-01-04", start_price=100, annual_vol=0.3).bars
    f = f[f.index.day_of_week < 5]
    rows = []
    for i, (ts, r) in enumerate(f.iterrows()):
        k = 2.0 if split_at is not None and i < split_at else 1.0  # before a 2:1 split, raw prices are twice as high
        rows.append({"date": ts.strftime("%Y-%m-%d"), "open": r.open * k, "high": r.high * k, "low": r.low * k, "close": r.close * k,
                     "adjusted_close": r.close, "volume": int(r.volume)})
    return f, rows


class FakeHttp:
    def __init__(self):
        self.calls, self.responses, self.fail = [], {}, {}

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params)))
        self.headers = headers
        path = url.split("/api/")[1]
        if path in self.fail:
            outcome = self.fail[path].pop(0) if isinstance(self.fail[path], list) else self.fail[path]
            if isinstance(outcome, Exception):
                raise outcome
            if outcome != 200:
                return SimpleNamespace(status_code=outcome, json=lambda: {"error": "x"})
        if path not in self.responses:
            return SimpleNamespace(status_code=404, json=lambda: [])
        body = self.responses[path]
        return SimpleNamespace(status_code=200, json=lambda: body)


def provider(http):
    return EodhdProvider(KEY, http_get=http)


def test_symbol_mapping_per_market():
    p = provider(FakeHttp())
    assert p.symbol(make_equity("AAPL", "US")) == "AAPL.US" and p.symbol(make_equity("BRK.B", "US")) == "BRK-B.US"
    assert p.symbol(make_equity("ENI", "IT")) == "ENI.MI" and p.symbol(make_equity("SAP", "DE")) == "SAP.XETRA"
    assert p.symbol(make_equity("MC", "FR")) == "MC.PA" and p.symbol(make_equity("NESN", "CH")) == "NESN.SW"
    from app.instruments.registry import default_instruments
    btc = next(i for i in default_instruments() if i.id.startswith("CRYPTO"))
    with pytest.raises(SymbolNotFound):
        p.symbol(btc)
    with pytest.raises(ProviderAuthError):
        EodhdProvider("")


def test_daily_bars_are_adjusted_for_corporate_actions_and_request_is_well_formed():
    http = FakeHttp()
    truth, rows = eod_rows(1, split_at=300)
    http.responses["eod/AAPL.US"] = rows + [{"date": "2030-01-01", "open": None, "close": 0}]  # a malformed row is dropped
    ds = provider(http).daily_bars(make_equity("AAPL", "US"), date(2021, 1, 1), date(2024, 1, 1))
    url, params = http.calls[0]
    assert url == "https://eodhd.com/api/eod/AAPL.US" and params == {"period": "d", "order": "a", "from": "2021-01-01", "to": "2024-01-01",
                                                                      "api_token": KEY, "fmt": "json"}
    assert ds.kind.value == "historical" and ds.source == "eodhd" and str(ds.bars.index.tz) == "UTC"
    assert (ds.bars.index == truth.index).all()
    # Raw prices halve at the split; the adjusted series is continuous and equals the true one.
    raw = pd.Series([r["close"] for r in rows], index=truth.index)
    assert abs(raw.iloc[300] / raw.iloc[299] - 1) > 0.4
    pd.testing.assert_series_equal(ds.bars.close, truth.close, check_names=False, rtol=1e-9)
    pd.testing.assert_series_equal(ds.bars.high, truth.high, check_names=False, rtol=1e-9)
    assert ds.bars.close.pct_change().abs().max() < 0.2


def test_errors_are_classified_and_never_leak_the_key():
    http = FakeHttp()
    inst = make_equity("AAPL", "US")
    for code, exc in ((401, ProviderAuthError), (403, ProviderAuthError), (402, ProviderRateLimited), (429, ProviderRateLimited),
                      (404, SymbolNotFound), (500, ProviderError)):
        http.fail["eod/AAPL.US"] = code
        with pytest.raises(exc) as info:
            provider(http).daily_bars(inst)
        assert KEY not in str(info.value)
    http.fail["eod/AAPL.US"] = ConnectionError(f"GET https://eodhd.com/api/eod/AAPL.US?api_token={KEY} failed")
    with pytest.raises(ProviderError) as info:
        provider(http).daily_bars(inst)
    assert KEY not in str(info.value) and info.value.__cause__ is None and info.value.__suppress_context__
    http.fail.clear()
    http.responses["eod/AAPL.US"] = {"unexpected": "object"}
    with pytest.raises(ProviderError):
        provider(http).daily_bars(inst)
    http.responses["eod/AAPL.US"] = []
    with pytest.raises(SymbolNotFound):
        provider(http).daily_bars(inst)


def test_symbol_list_filters_types_venues_and_currencies():
    http = FakeHttp()
    http.responses["exchange-symbol-list/US"] = [
        {"Code": "AAPL", "Name": "Apple Inc", "Exchange": "NASDAQ", "Currency": "USD", "Type": "Common Stock"},
        {"Code": "BRK-B", "Name": "Berkshire", "Exchange": "NYSE", "Currency": "USD", "Type": "Common Stock"},
        {"Code": "SPY", "Name": "SPDR S&P 500", "Exchange": "NYSE ARCA", "Currency": "USD", "Type": "ETF"},
        {"Code": "XYZQ", "Name": "Penny", "Exchange": "PINK", "Currency": "USD", "Type": "Common Stock"},
        {"Code": "VFIAX", "Name": "Fund", "Exchange": "NMFQS", "Currency": "USD", "Type": "FUND"},
    ]
    http.responses["exchange-symbol-list/MI"] = [
        {"Code": "ENI", "Name": "Eni SpA", "Exchange": "MI", "Currency": "EUR", "Type": "Common Stock"},
        {"Code": "XUSD", "Name": "USD line", "Exchange": "MI", "Currency": "USD", "Type": "ETF"},
    ]
    us = provider(http).list_symbols("US")
    assert [(r["ticker"], r["type"]) for r in us] == [("AAPL", "stock"), ("BRK.B", "stock"), ("SPY", "etf")]
    assert [r["ticker"] for r in provider(http).list_symbols("IT")] == ["ENI"]
    with pytest.raises(ProviderError):
        provider(http).list_symbols("ZZ")


def test_download_job_isolates_failures_retries_rate_limits_and_stops_on_bad_key():
    http = FakeHttp()
    instruments = [make_equity(t, "US") for t in ("AAA", "BBB", "CCC", "DDD")]
    http.responses["eod/AAA.US"] = eod_rows(1)[1]
    http.responses["eod/CCC.US"] = eod_rows(3)[1]
    http.responses["eod/DDD.US"] = eod_rows(4)[1]
    http.fail["eod/CCC.US"] = [429, 200]  # rate limited once, then fine
    stored, sleeps = {}, []

    def store(inst, ds):
        if inst.symbol == "DDD":
            raise RuntimeError("disk full")
        stored[inst.id] = len(ds.bars)
        return len(ds.bars)

    job = DownloadJob(provider(http), instruments, store, years=5, min_interval=0.25, sleep=sleeps.append, today=lambda: date(2024, 6, 3))
    job.run()
    st = job.snapshot()
    assert st["status"] == "finished" and (st["total"], st["done"], st["stored"], st["failed_count"]) == (4, 4, 2, 2)
    assert set(stored) == {"STOCK:AAA", "STOCK:CCC"} and st["bars"] == sum(stored.values())
    assert {f["instrument_id"]: f["reason"] for f in st["failed"]} == {"STOCK:BBB": "EODHD has no data for this symbol", "STOCK:DDD": "RuntimeError: disk full"}
    assert 30.0 in sleeps and sleeps.count(0.25) == 3  # backed off after the 429, paced between symbols (not after the last)
    assert DownloadJob(provider(http), instruments, store).min_interval == EodhdProvider.min_interval  # default pacing comes from the provider
    assert http.calls[0][1]["from"] == "2019-06-03" and http.calls[0][1]["to"] == "2024-06-03"

    http.fail = {"eod/AAA.US": 401}
    bad = DownloadJob(provider(http), instruments, store, sleep=lambda s: None)
    bad.run()
    assert bad.snapshot()["status"] == "failed" and bad.snapshot()["done"] == 1 and "API key" in bad.snapshot()["message"]

    http.fail = {}
    cancelled = DownloadJob(provider(http), instruments, store, sleep=lambda s: None)
    cancelled.cancel()
    cancelled.run()
    assert cancelled.snapshot()["status"] == "cancelled" and cancelled.snapshot()["done"] == 0


# ---------------------------------------------------------------- through the API
@pytest.fixture
def api(client):
    admin = login(client)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000).model_dump())
    http = FakeHttp()
    rt = client.app.state.container.runtime
    rt.provider_factory = lambda name, key: EodhdProvider(key, http_get=http)
    rt.download_options = {"sleep": lambda s: None, "min_interval": 0}
    return client, admin, http, rt


def wait(client, admin):
    for _ in range(200):
        st = client.get("/api/market/download", headers=admin).json()
        if st["status"] not in ("pending", "running"):
            return st
        time.sleep(0.05)
    raise AssertionError("download did not finish")


def test_provider_key_is_encrypted_never_returned_and_role_protected(api, db):
    client, admin, http, rt = api
    def eodhd():
        return next(p for p in client.get("/api/market/providers", headers=admin).json() if p["name"] == "eodhd")

    assert [p["name"] for p in client.get("/api/market/providers", headers=admin).json()] == ["tiingo", "alphavantage", "eodhd"]
    assert (eodhd()["configured"], eodhd()["key_hint"], eodhd()["source"]) == (False, None, None) and "IT" in eodhd()["markets"]
    assert "No API key" in client.post("/api/market/providers/eodhd/test", headers=admin).json()["detail"]
    viewer = login(client, VIEWER)
    assert client.put("/api/market/providers/eodhd/key", headers=viewer, json={"api_key": KEY}).status_code == 403
    assert client.put("/api/market/providers/nope/key", headers=admin, json={"api_key": KEY}).status_code == 400
    assert client.put("/api/market/providers/eodhd/key", headers=admin, json={"api_key": KEY}).status_code == 204
    status = client.get("/api/market/providers", headers=admin)
    assert (eodhd()["configured"], eodhd()["key_hint"], eodhd()["source"]) == (True, "…ABCD", "stored")
    from sqlalchemy import select

    from app.database.models import AppSetting, AuditEntry
    with db.session() as s:
        stored = json.dumps(s.get(AppSetting, "provider:eodhd").value)
        audit = json.dumps([a.detail for a in s.scalars(select(AuditEntry))])
    for text in (status.text, stored, audit, client.get("/api/audit", headers=admin).text):
        assert KEY not in text
    http.responses["eod/AAPL.US"] = eod_rows(1, n=30)[1]
    ok = client.post("/api/market/providers/eodhd/test", headers=admin).json()
    assert ok["ok"] is True and ok["bars"] > 15 and "AAPL" in ok["message"]
    http.fail["eod/AAPL.US"] = 401
    assert client.post("/api/market/providers/eodhd/test", headers=admin).json() == {"ok": False, "message": "EODHD rejected the API key, or the plan does not include this data"}
    assert client.delete("/api/market/providers/eodhd/key", headers=admin).status_code == 204
    assert eodhd()["configured"] is False


def test_import_listing_download_and_backtest_end_to_end(api):
    client, admin, http, rt = api
    client.put("/api/market/providers/eodhd/key", headers=admin, json={"api_key": KEY})
    http.responses["exchange-symbol-list/US"] = [{"Code": c, "Name": c, "Exchange": "NASDAQ", "Currency": "USD", "Type": "Common Stock"} for c in ("AAPL", "MSFT", "NVDA", "GONE")] + \
        [{"Code": "QQQ", "Name": "ETF", "Exchange": "NASDAQ", "Currency": "USD", "Type": "ETF"}]
    http.responses["exchange-symbol-list/MI"] = [{"Code": "ENI", "Name": "Eni", "Exchange": "MI", "Currency": "EUR", "Type": "Common Stock"}]
    assert client.post("/api/market/providers/eodhd/symbols", headers=admin, json={"market": "US", "types": ["stock"]}).json() == \
        {"listed": 5, "created": 3, "existing": 1, "invalid": 0}
    assert client.post("/api/market/providers/eodhd/symbols", headers=admin, json={"market": "IT", "types": ["stock", "etf"]}).json()["created"] == 1
    assert client.post("/api/market/providers/eodhd/symbols", headers=admin, json={"market": "US", "types": ["bond"]}).status_code == 422
    stats = client.get("/api/instruments/stats", headers=admin).json()
    assert stats["by_market"]["US"]["instruments"] == 5 and stats["by_market"]["IT"] == {"instruments": 1, "with_data": 0} and stats["with_data"] == 0

    for i, sym in enumerate(("AAPL.US", "MSFT.US", "NVDA.US", "ENI.MI"), start=1):
        http.responses[f"eod/{sym}"] = eod_rows(i)[1]
    assert client.post("/api/market/download", headers=admin, json={"market": "US"}).json()["total"] == 5  # 4 stocks + built-in SPY
    st = wait(client, admin)
    assert st["status"] == "finished" and st["stored"] == 3 and {f["instrument_id"] for f in st["failed"]} == {"STOCK:GONE", "ETF:SPY"}
    again = client.post("/api/market/download", headers=admin, json={"market": "US"}).json()
    assert again["total"] == 2  # only the two without data are retried
    wait(client, admin)
    client.post("/api/market/download", headers=admin, json={"instrument_ids": ["STOCK:ENI.MI"]})
    assert wait(client, admin)["stored"] == 1
    assert "Nothing to download" in client.post("/api/market/download", headers=admin, json={"instrument_ids": ["STOCK:ENI.MI"]}).json()["detail"]
    assert client.post("/api/market/download", headers=login(client, VIEWER), json={}).status_code == 403

    listed = client.get("/api/instruments", headers=admin, params={"has_data": True}).json()
    assert [i["id"] for i in listed] == ["STOCK:AAPL", "STOCK:ENI.MI", "STOCK:MSFT", "STOCK:NVDA"]
    assert listed[0]["series"][0]["kind"] == "historical" and listed[0]["series"][0]["bars"] > 400
    assert [i["id"] for i in client.get("/api/instruments", headers=admin, params={"q": "eni"}).json()] == ["STOCK:ENI.MI"]
    assert len(client.get("/api/instruments", headers=admin, params={"limit": 2}).json()) == 2
    cov = {c["instrument_id"]: c for c in client.get("/api/market/coverage", headers=admin).json()}
    assert cov["STOCK:ENI.MI"]["source"] == "eodhd" and cov["STOCK:ENI.MI"]["first"].startswith("2021-01-04")

    scan = client.post("/api/backtests/scan", headers=admin, json={"strategy": "ema_crossover", "params": {"fast": 10, "slow": 30}, "timeframe": "1d",
                                                                 "static_fx": {"USD": 0.92}}).json()
    assert scan["summary"]["instruments_tested"] == 4 and {r["data_kind"] for r in scan["rows"]} == {"historical"}

    # Re-downloading replaces the whole adjusted history instead of appending to it.
    http.responses["eod/AAPL.US"] = eod_rows(9, n=300)[1]
    client.post("/api/market/download", headers=admin, json={"instrument_ids": ["STOCK:AAPL"], "only_missing": False})
    wait(client, admin)
    cov = {c["instrument_id"]: c for c in client.get("/api/market/coverage", headers=admin).json()}
    assert cov["STOCK:AAPL"]["bars"] == len(eod_rows(9, n=300)[1])
    # Removing an instrument removes its series and its summary row.
    assert client.delete("/api/instruments/STOCK:NVDA", headers=admin).status_code == 204
    assert "STOCK:NVDA" not in {c["instrument_id"] for c in client.get("/api/market/coverage", headers=admin).json()}


def test_download_refuses_to_overwrite_simulated_data(api):
    client, admin, http, rt = api
    client.put("/api/market/providers/eodhd/key", headers=admin, json={"api_key": KEY})
    client.post("/api/market/generate-simulated", headers=admin, json={"instrument_id": "STOCK:AAPL", "timeframe": "1d", "bars": 500, "seed": 1})
    http.responses["eod/AAPL.US"] = eod_rows(1)[1]
    client.post("/api/market/download", headers=admin, json={"instrument_ids": ["STOCK:AAPL"]})
    st = wait(client, admin)
    assert st["stored"] == 0 and "simulated" in st["failed"][0]["reason"]
    assert client.get("/api/market/coverage", headers=admin).json()[0]["kind"] == "simulated"


# ---------------------------------------------------------------- free providers
from app.market_data.providers import AlphaVantageProvider, TiingoProvider  # noqa: E402


class Resp(SimpleNamespace):
    pass


def tiingo_rows(seed, n=400):
    f = synthetic_bars("1d", n, seed=seed, start="2021-01-04", start_price=100, annual_vol=0.3).bars
    f = f[f.index.day_of_week < 5]
    return f, [{"date": ts.strftime("%Y-%m-%dT00:00:00.000Z"), "open": r.open * 4, "high": r.high * 4, "low": r.low * 4, "close": r.close * 4, "volume": int(r.volume),
                "adjOpen": r.open, "adjHigh": r.high, "adjLow": r.low, "adjClose": r.close, "adjVolume": int(r.volume) * 4, "divCash": 0.0, "splitFactor": 1.0}
               for ts, r in f.iterrows()]


def test_tiingo_uses_adjusted_prices_sends_key_in_header_and_classifies_errors():
    seen = {}
    truth, rows = tiingo_rows(2)

    def get(url, params=None, timeout=None, headers=None):
        seen.update(url=url, params=params, headers=headers)
        return Resp(status_code=seen.get("code", 200), json=lambda: seen.get("body", rows))

    p = TiingoProvider(KEY, http_get=get)
    ds = p.daily_bars(make_equity("BRK.B", "US"), date(2021, 1, 1), date(2022, 6, 1))
    assert seen["url"] == "https://api.tiingo.com/tiingo/daily/brk-b/prices" and seen["headers"]["Authorization"] == f"Token {KEY}"
    assert KEY not in json.dumps(seen["params"]) and seen["params"] == {"format": "json", "startDate": "2021-01-01", "endDate": "2022-06-01"}
    assert ds.source == "tiingo" and (ds.bars.index == truth.index).all()
    pd.testing.assert_series_equal(ds.bars.close, truth.close, check_names=False)  # adjusted values, not the raw ones
    for code, exc in ((401, ProviderAuthError), (403, ProviderAuthError), (404, SymbolNotFound), (429, ProviderRateLimited), (500, ProviderError)):
        seen["code"] = code
        with pytest.raises(exc):
            p.daily_bars(make_equity("AAPL", "US"))
    seen.update(code=200, body={"detail": "Error: You have run over your hourly request allocation."})
    with pytest.raises(ProviderRateLimited):
        p.daily_bars(make_equity("AAPL", "US"))
    with pytest.raises(SymbolNotFound, match="US stocks only"):
        p.daily_bars(make_equity("ENI", "IT"))
    assert TiingoProvider.markets == {"US"} and TiingoProvider.min_interval >= 72  # 50 requests an hour


AV_DAILY = {"Meta Data": {"2. Symbol": "IBM", "4. Output Size": "Full size"}, "Time Series (Daily)": {
    "2026-11-12": {"1. open": "248.3000", "2. high": "248.7700", "3. low": "244.3700", "4. close": "244.6700", "5. volume": "2816978"},
    "2026-11-11": {"1. open": "244.7200", "2. high": "249.3100", "3. low": "244.0000", "4. close": "248.1000", "5. volume": "3100000"},
    "2026-11-10": {"1. open": "0", "2. high": "0", "3. low": "0", "4. close": "0", "5. volume": "0"}}}
AV_LISTING = """symbol,name,exchange,assetType,ipoDate,delistingDate,status
-P-HIZ,Presurance Holdings Inc,NASDAQ,Stock,2023-08-30,null,Active
A,Agilent Technologies Inc,NYSE,Stock,1999-11-18,null,Active
BRK-B,Berkshire Hathaway Inc - Class B,NYSE,Stock,1996-05-09,null,Active
ABR-P-D,Arbor Realty Trust Inc,NYSE,Stock,2021-06-02,null,Active
AACTWS,Ares Acquisition Corp II Warrant,NYSE,Stock,2023-06-12,null,Active
SPY,SPDR S&P 500 ETF Trust,NYSE ARCA,ETF,1993-01-29,null,Active
OLD,Gone Corp,NYSE,Stock,1990-01-01,2020-01-01,Delisted
"""
AV_LIMIT = {"Information": "We have detected your API key as XXXX and our standard API rate limit is 25 requests per day. Please subscribe to any of the premium plans."}
AV_PREMIUM = {"Information": "Thank you for using Alpha Vantage! The outputsize=full parameter value is a premium feature for the TIME_SERIES_DAILY endpoint."}
AV_DEMO = {"Information": "The **demo** API key is for demo purposes only. Please claim your free API key at (https://www.alphavantage.co/support/#api-key)."}


def test_alpha_vantage_parsing_free_plan_fallback_and_message_classification():
    """Payload shapes below were captured from the live service with its public demo key."""
    script, calls = [], []

    def get(url, params=None, timeout=None, headers=None):
        calls.append(dict(params))
        body = script.pop(0)
        return Resp(status_code=200, json=lambda: body, text=body if isinstance(body, str) else json.dumps(body))

    p = AlphaVantageProvider(KEY, http_get=get)
    script[:] = [AV_DAILY]
    ds = p.daily_bars(make_equity("IBM", "US"))
    assert calls[-1] == {"function": "TIME_SERIES_DAILY", "symbol": "IBM", "outputsize": "full", "apikey": KEY}
    assert list(ds.bars.index.strftime("%Y-%m-%d")) == ["2026-11-11", "2026-11-12"] and ds.bars.close.iloc[-1] == 244.67  # sorted; the all-zero row is dropped
    assert ds.source == "alphavantage:unadjusted"
    script[:] = [AV_PREMIUM, AV_DAILY]  # free keys: full history refused -> falls back to the latest 100 days and says so
    assert p.daily_bars(make_equity("IBM", "US")).source == "alphavantage:unadjusted:last100" and calls[-1]["outputsize"] == "compact"
    for body, exc in ((AV_LIMIT, ProviderRateLimited), (AV_DEMO, ProviderAuthError), ({"Error Message": "Invalid API call."}, SymbolNotFound), ({}, ProviderError)):
        script[:] = [body]
        with pytest.raises(exc) as info:
            p.daily_bars(make_equity("IBM", "US"))
        assert KEY not in str(info.value)
    script[:] = [AV_LISTING]
    listed = p.list_symbols("US")
    assert [(r["ticker"], r["type"], r["exchange"]) for r in listed] == [("A", "stock", "NYSE"), ("BRK.B", "stock", "NYSE"), ("SPY", "etf", "NYSE ARCA")]
    script[:] = [json.dumps(AV_LIMIT)]
    with pytest.raises(ProviderRateLimited):
        p.list_symbols("US")
    with pytest.raises(ProviderError):
        p.list_symbols("IT")


def test_exhausted_allowance_stops_the_job_and_download_missing_resumes(api):
    client, admin, http, rt = api
    state = {"allowed": 2}

    class Limited(EodhdProvider):
        label, markets, listing_markets = "Limited", frozenset({"US"}), frozenset()

        def daily_bars(self, instrument, start=None, end=None):
            if state["allowed"] <= 0:
                raise ProviderRateLimited("daily limit reached")
            state["allowed"] -= 1
            return super().daily_bars(instrument, start, end)

    rt.provider_factory = lambda name, key: Limited(key, http_get=http)
    client.put("/api/market/providers/tiingo/key", headers=admin, json={"api_key": KEY})
    client.post("/api/instruments", headers=admin, json={"tickers": ["AAA", "BBB", "CCC", "DDD"], "market": "US"})
    client.post("/api/instruments", headers=admin, json={"tickers": ["ENI"], "market": "IT"})
    for i, t in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1):
        http.responses[f"eod/{t}.US"] = eod_rows(i, n=300)[1]
    ids = [f"STOCK:{t}" for t in ("AAA", "BBB", "CCC", "DDD")]
    assert client.post("/api/market/download", headers=admin, json={"provider": "tiingo", "instrument_ids": ids}).json()["total"] == 4
    st = wait(client, admin)
    assert st["status"] == "stopped" and st["stored"] == 2 and st["failed_count"] == 0 and "resumes" in st["message"]
    state["allowed"] = 10  # the next day
    assert client.post("/api/market/download", headers=admin, json={"provider": "tiingo", "instrument_ids": ids}).json()["total"] == 2
    assert wait(client, admin)["status"] == "finished"
    assert len([c for c in client.get("/api/market/coverage", headers=admin).json() if c["instrument_id"] in ids]) == 4
    # A provider is never asked for markets it does not cover.
    r = client.post("/api/market/download", headers=admin, json={"provider": "tiingo", "instrument_ids": ["STOCK:ENI.MI"]})
    assert r.status_code == 400 and "does not cover" in r.json()["detail"]
    r = client.post("/api/market/providers/tiingo/symbols", headers=admin, json={"market": "US"})
    assert r.status_code == 400 and "cannot list" in r.json()["detail"]
    assert client.post("/api/market/download", headers=admin, json={"provider": "nope"}).status_code == 400

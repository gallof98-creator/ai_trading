"""Phase 2 of the global module: bulk price downloads, incremental updates, every registered exchange."""
import json
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.database.settings_store import UserSettings
from app.instruments.registry import MARKETS, make_equity, market_for_exchange, market_of
from app.market_data.calendars import get_calendar
from app.market_data.fx import FxRateProvider
from app.market_data.providers import (AlpacaProvider, DownloadJob, EodhdProvider, NeedsFullHistory, ProviderAuthError, ProviderError,
                                       ProviderRateLimited, SymbolNotFound)
from app.market_data.sources import synthetic_bars
from tests.conftest import login

UTC = timezone.utc


def frame(seed, n=420, start="2024-01-02"):
    f = synthetic_bars("1d", n, seed=seed, start=start, start_price=50 + seed, annual_vol=0.3).bars
    return f[f.index.day_of_week < 5]


def alpaca_bars(f):
    # Alpaca stamps a daily bar at midnight New York time: 05:00Z in winter, 04:00Z in summer.
    out = []
    for ts, r in f.iterrows():
        local = pd.Timestamp(ts.date(), tz="America/New_York")
        out.append({"t": local.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": int(r.volume), "n": 10, "vw": r.close})
    return out


# ---------------------------------------------------------------- every registered exchange
def test_every_registered_exchange_can_hold_instruments_with_the_right_currency_unit():
    assert len(MARKETS) == 43 and market_for_exchange("MI") == "IT" and market_for_exchange("T") == "T"
    cases = {("7203", "T"): ("STOCK:7203.T", "JPY"), ("VOD", "L"): ("STOCK:VOD.L", "GBX"), ("0700", "HK"): ("STOCK:0700.HK", "HKD"),
             ("M&M", "NS"): ("STOCK:M&M.NS", "INR"), ("SHOP", "TO"): ("STOCK:SHOP.TO", "CAD"), ("BHP", "AX"): ("STOCK:BHP.AX", "AUD"),
             ("NPN", "JO"): ("STOCK:NPN.JO", "ZAC"), ("ENI", "IT"): ("STOCK:ENI.MI", "EUR"), ("SAP", "F"): ("STOCK:SAP.F", "EUR")}
    for (ticker, market), (iid, ccy) in cases.items():
        inst = make_equity(ticker, market)
        assert (inst.id, inst.quote_currency, market_of(inst)) == (iid, ccy, market)
    assert make_equity("SAP", "DE").id == "STOCK:SAP.DE" != make_equity("SAP", "F").id  # Xetra and Frankfurt listings stay distinct
    tokyo = get_calendar(make_equity("7203", "T").calendar)
    assert tokyo.is_trading_day(date(2026, 11, 13)) and not tokyo.is_trading_day(date(2026, 11, 14))  # Friday / Saturday
    assert EodhdProvider("k", http_get=lambda *a, **k: None).symbol(make_equity("VOD", "L")) == "VOD.LSE"
    assert EodhdProvider("k", http_get=lambda *a, **k: None).symbol(make_equity("7203", "T")) == "7203.TSE"
    for bad in ("VOLV B", "", "X" * 20):
        with pytest.raises(ValueError):
            make_equity(bad, "ST")


def test_prices_quoted_in_pence_are_converted_with_the_unit_factor():
    fx = FxRateProvider(conversion_cost_bps=0)
    t = datetime(2026, 1, 5, 12, tzinfo=UTC)
    fx.add_rate("EUR", "GBP", t, 0.85, "test")
    assert fx.quote("GBX", "GBP", t).rate == pytest.approx(0.01)
    assert fx.convert(25_000, "GBX", "EUR", t) == pytest.approx(250 / 0.85)  # 25,000 pence = 250 pounds
    assert fx.convert(100, "EUR", "GBX", t) == pytest.approx(8500)
    assert fx.quote("GBX", "GBX", t).rate == 1.0
    fx.add_rate("EUR", "ZAR", t, 20.0, "test")
    assert fx.convert(2000, "ZAC", "EUR", t) == pytest.approx(1.0)


# ---------------------------------------------------------------- Alpaca
def test_alpaca_bulk_request_pagination_dates_and_errors():
    a, b = frame(1), frame(2)
    pages = [{"bars": {"AAPL": alpaca_bars(a.iloc[:200]), "MSFT": alpaca_bars(b.iloc[:150])}, "next_page_token": "p2"},
             {"bars": {"AAPL": alpaca_bars(a.iloc[200:]), "MSFT": alpaca_bars(b.iloc[150:]), "ZZZZ": []}, "next_page_token": None}]
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params), headers))
        return SimpleNamespace(status_code=200, json=lambda: pages[len(calls) - 1])

    p = AlpacaProvider("KEYID", "SECRET", http_get=get)
    insts = [make_equity(t, "US") for t in ("AAPL", "MSFT", "NODATA")] + [make_equity("ENI", "IT")]
    out = p.daily_bars_bulk(insts, date(2024, 1, 1), date(2030, 1, 1))
    url, params, headers = calls[0]
    assert url == "https://data.alpaca.markets/v2/stocks/bars" and headers == {"APCA-API-KEY-ID": "KEYID", "APCA-API-SECRET-KEY": "SECRET"}
    assert params["symbols"] == "AAPL,MSFT,NODATA" and params["timeframe"] == "1Day" and params["adjustment"] == "all" and params["start"] == "2024-01-01"
    assert date.fromisoformat(params["end"]) < datetime.now(UTC).date()  # the free plan cannot ask for today's bar
    assert "KEYID" not in json.dumps(params) and calls[1][1]["page_token"] == "p2" and len(calls) == 2
    assert set(out) == {"STOCK:AAPL", "STOCK:MSFT"}  # no data and non-US instruments are simply absent
    assert (out["STOCK:AAPL"].bars.index == a.index).all() and out["STOCK:AAPL"].source == "alpaca"  # New York dates, summer and winter time alike
    assert (out["STOCK:MSFT"].bars.index == b.index).all() and out["STOCK:MSFT"].bars.close.to_numpy() == pytest.approx(b.close.to_numpy())

    for code, exc in ((401, ProviderAuthError), (403, ProviderAuthError), (429, ProviderRateLimited), (500, ProviderError)):
        bad = AlpacaProvider("KEYID", "SECRET", http_get=lambda *a, code=code, **k: SimpleNamespace(status_code=code, json=lambda: {}))
        with pytest.raises(exc) as info:
            bad.daily_bars(insts[0])
        assert "SECRET" not in str(info.value)
    empty = AlpacaProvider("KEYID", "SECRET", http_get=lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"bars": {}, "next_page_token": None}))
    with pytest.raises(SymbolNotFound):
        empty.daily_bars(insts[0])
    with pytest.raises(ProviderAuthError):
        AlpacaProvider("KEYID", "")
    with pytest.raises(ValueError):
        p.daily_bars_bulk([make_equity(f"T{i}", "US") for i in range(51)])


class BulkFake(EodhdProvider):
    """Provider double with batch support and a scriptable history per symbol."""

    label, markets, bulk_size, min_interval = "Bulk", frozenset({"US"}), 3, 0.0

    def __init__(self, data):
        self.data, self.bulk_calls, self.single_calls = data, [], []

    def daily_bars_bulk(self, instruments, start=None, end=None):
        self.bulk_calls.append(([i.symbol for i in instruments], start))
        out = {}
        for i in instruments:
            if i.symbol in self.data:
                f = self.data[i.symbol]
                out[i.id] = SimpleNamespace(bars=f[f.index.date >= start] if start else f, source="bulk")
        return out

    def daily_bars(self, instrument, start=None, end=None):
        self.single_calls.append((instrument.symbol, start))
        f = self.data[instrument.symbol]
        return SimpleNamespace(bars=f[f.index.date >= start] if start else f, source="bulk")


def test_bulk_job_batches_requests_and_reports_missing_symbols():
    data = {t: frame(i) for i, t in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE"), start=1)}
    insts = [make_equity(t, "US") for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "GONE", "HHH")]
    del_ = data.pop("EEE")
    stored = {}
    provider = BulkFake(data)
    job = DownloadJob(provider, insts, lambda inst, ds, mode: stored.setdefault(inst.id, len(ds.bars)) or len(ds.bars), years=5, sleep=lambda s: None,
                      today=lambda: date(2026, 6, 1))
    job.run()
    st = job.snapshot()
    assert [c[0] for c in provider.bulk_calls] == [["AAA", "BBB", "CCC"], ["DDD", "EEE", "GONE"], ["HHH"]] and job.requests == 3  # 3 requests for 7 instruments
    assert (st["status"], st["done"], st["stored"], st["failed_count"]) == ("finished", 7, 4, 3)
    assert {f["instrument_id"] for f in st["failed"]} == {"STOCK:EEE", "STOCK:GONE", "STOCK:HHH"} and "no data" in st["failed"][0]["reason"]
    assert del_ is not None


# ---------------------------------------------------------------- through the API
@pytest.fixture
def api(client):
    admin = login(client)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000).model_dump())
    rt = client.app.state.container.runtime
    rt.download_options = {"sleep": lambda s: None, "min_interval": 0}
    rt.auto_metrics = False  # the scanner's automatic refresh is tested separately
    return client, admin, rt


def wait(client, admin):
    """Wait for the background job by looking at the job object itself: polling the HTTP API here would
    share the single in-memory SQLite connection with the job's thread."""
    rt = client.app.state.container.runtime
    for _ in range(600):
        if rt.download is not None and rt.download.completed.is_set():  # job and its bookkeeping are both finished
            return client.get("/api/market/download", headers=admin).json()
        time.sleep(0.02)
    raise AssertionError("download did not finish")


def coverage(client, admin):
    return {c["instrument_id"]: c for c in client.get("/api/market/coverage", headers=admin).json()}


def test_incremental_update_appends_new_bars_and_redownloads_when_history_was_readjusted(api):
    client, admin, rt = api
    full = {"AAA": frame(1), "BBB": frame(2), "CCC": frame(3)}
    data = {t: f.iloc[:250] for t, f in full.items()}
    provider = BulkFake(data)
    rt.provider_factory = lambda name, key: provider
    assert client.put("/api/market/providers/alpaca/key", headers=admin, json={"api_key": "KEYID-1234", "api_secret": "the-secret"}).status_code == 204
    client.post("/api/instruments", headers=admin, json={"tickers": ["AAA", "BBB", "CCC", "NEW"], "market": "US"})
    ids = ["STOCK:AAA", "STOCK:BBB", "STOCK:CCC"]
    assert "Nothing to update" in client.post("/api/market/download", headers=admin, json={"provider": "alpaca", "instrument_ids": ids, "mode": "update"}).json()["detail"]
    client.post("/api/market/download", headers=admin, json={"provider": "alpaca", "instrument_ids": ids, "years": 30})
    assert wait(client, admin)["stored"] == 3 and coverage(client, admin)["STOCK:AAA"]["bars"] == 250

    # Later: 30 new days for everyone; BBB had a 2:1 split, so the vendor now reports its whole past at half the price.
    data.update({"AAA": full["AAA"].iloc[:280], "CCC": full["CCC"].iloc[:280]})
    split = full["BBB"].iloc[:280].copy()
    split[["open", "high", "low", "close"]] *= 0.5
    data["BBB"] = split
    provider.bulk_calls.clear()
    r = client.post("/api/market/download", headers=admin, json={"provider": "alpaca", "mode": "update", "market": "US"})
    assert r.status_code == 200 and r.json()["total"] == 3 and r.json()["mode"] == "update"  # NEW has no data yet: not part of an update
    st = wait(client, admin)
    assert (st["status"], st["stored"], st["readjusted"], st["failed_count"]) == ("finished", 3, 1, 0)
    asked_from = provider.bulk_calls[0][1]
    assert asked_from == full["AAA"].index[249].date() - timedelta(days=7)  # only the recent days were requested
    assert provider.single_calls == [("BBB", provider.single_calls[0][1])] and provider.single_calls[0][1] < asked_from  # BBB alone was re-downloaded in full
    cov = coverage(client, admin)
    assert cov["STOCK:AAA"]["bars"] == 280 and cov["STOCK:BBB"]["bars"] == 280 and cov["STOCK:AAA"]["last"][:10] == str(full["AAA"].index[279].date())
    with rt.db.session() as s:
        aaa, bbb = rt.store.load(s, "STOCK:AAA", "1d").bars, rt.store.load(s, "STOCK:BBB", "1d").bars
    pd.testing.assert_frame_equal(aaa, full["AAA"].iloc[:280], check_freq=False)  # appended, nothing duplicated or lost
    assert bbb.close.iloc[0] == pytest.approx(split.close.iloc[0]) and bbb.close.pct_change().abs().max() < 0.2  # one consistent adjustment basis, no jump
    # Nothing new: an update stores no bars and changes nothing.
    client.post("/api/market/download", headers=admin, json={"provider": "alpaca", "mode": "update", "instrument_ids": ["STOCK:AAA"]})
    assert wait(client, admin)["bars"] == 0 and coverage(client, admin)["STOCK:AAA"]["bars"] == 280
    assert client.post("/api/market/download", headers=admin, json={"provider": "alpaca", "mode": "sideways"}).status_code == 422


def test_usage_is_counted_and_secret_is_stored_encrypted(api, db):
    client, admin, rt = api
    provider = BulkFake({t: frame(i) for i, t in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1)})
    rt.provider_factory = lambda name, key: provider
    client.put("/api/market/providers/alpaca/key", headers=admin, json={"api_key": "KEYID-1234", "api_secret": "the-secret"})
    client.post("/api/instruments", headers=admin, json={"tickers": ["AAA", "BBB", "CCC", "DDD"], "market": "US"})
    client.post("/api/market/download", headers=admin, json={"provider": "alpaca", "instrument_ids": [f"STOCK:{t}" for t in ("AAA", "BBB", "CCC", "DDD")]})
    wait(client, admin)
    for _ in range(100):  # usage is written by the job's thread right after it finishes
        if rt.provider_usage("alpaca")["requests_today"]:
            break
        time.sleep(0.02)
    alpaca = next(p for p in client.get("/api/market/providers", headers=admin).json() if p["name"] == "alpaca")
    assert alpaca["usage"] == {"requests_today": 2, "requests_this_month": 2, "symbols_this_month": 4}  # 4 instruments in 2 batched requests
    assert alpaca["needs_secret"] is True and alpaca["symbols_per_request"] == 50 and alpaca["key_hint"] == "…1234"
    from app.database.models import AppSetting
    with db.session() as s:
        stored = json.dumps(s.get(AppSetting, "provider:alpaca").value)
    assert "the-secret" not in stored and "KEYID-1234" not in stored
    assert "the-secret" not in client.get("/api/market/providers", headers=admin).text and "the-secret" not in client.get("/api/audit", headers=admin).text
    creds, source = rt._provider_credentials("alpaca")
    assert creds == {"api_key": "KEYID-1234", "api_secret": "the-secret"} and source == "stored"


def test_coverage_shows_which_provider_can_price_each_exchange(api):
    client, admin, rt = api
    rep = {e["code"]: e for e in client.get("/api/global/coverage", headers=admin).json()["exchanges"]}
    assert rep["US"]["price_providers"] == ["alpaca", "alphavantage", "eodhd", "tiingo"]
    assert rep["T"]["price_providers"] == ["eodhd"] and rep["L"]["price_providers"] == ["eodhd"]  # outside the US only the paid provider has prices
    assert rep["US"]["stale_price_series"] == 0

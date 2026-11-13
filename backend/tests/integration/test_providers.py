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

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
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
    assert 30.0 in sleeps and sleeps.count(0.25) == 4  # backed off after the 429, paced between symbols
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
    rt.provider_factory = lambda key: EodhdProvider(key, http_get=http)
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
    assert client.get("/api/market/providers", headers=admin).json() == [{"name": "eodhd", "configured": False, "key_hint": None, "source": None}]
    assert "No API key" in client.post("/api/market/providers/eodhd/test", headers=admin).json()["detail"]
    viewer = login(client, VIEWER)
    assert client.put("/api/market/providers/eodhd/key", headers=viewer, json={"api_key": KEY}).status_code == 403
    assert client.put("/api/market/providers/nope/key", headers=admin, json={"api_key": KEY}).status_code == 400
    assert client.put("/api/market/providers/eodhd/key", headers=admin, json={"api_key": KEY}).status_code == 204
    status = client.get("/api/market/providers", headers=admin)
    assert status.json() == [{"name": "eodhd", "configured": True, "key_hint": "…ABCD", "source": "stored"}]
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
    assert client.get("/api/market/providers", headers=admin).json()[0]["configured"] is False


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

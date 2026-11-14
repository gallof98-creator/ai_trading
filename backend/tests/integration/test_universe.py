"""Stock universe: adding instruments, importing many CSV files, scanning a strategy across them."""
import io

import numpy as np
import pandas as pd
import pytest

from app.database.settings_store import UserSettings
from app.domain.models import AssetClass
from app.instruments.registry import make_equity
from app.market_data.sources import load_csv, synthetic_bars
from tests.conftest import VIEWER, login


def daily_frame(seed, n=900):
    bars = synthetic_bars("1d", n, seed=seed, start="2019-01-01", start_price=50 + seed, annual_vol=0.3).bars
    return bars[bars.index.day_of_week < 5].round(4)


def vendor_csv(seed):
    """Layout of a typical vendor export: Date,Open,High,Low,Close,Adj Close,Volume."""
    f = daily_frame(seed)
    out = pd.DataFrame({"Date": f.index.strftime("%Y-%m-%d"), "Open": f.open, "High": f.high, "Low": f.low, "Close": f.close,
                        "Adj Close": f.close * 0.97, "Volume": f.volume.round()})
    return out.to_csv(index=False)


def bulk_txt(seed, ticker="AAPL.US"):
    """Layout of bulk-download text files: <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>."""
    f = daily_frame(seed)
    lines = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
    lines += [f"{ticker},D,{ts:%Y%m%d},000000,{r.open},{r.high},{r.low},{r.close},{r.volume:.0f},0" for ts, r in f.iterrows()]
    return "\n".join(lines)


@pytest.fixture
def api(client):
    admin = login(client)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000).model_dump())
    return client, admin


def test_csv_layouts_are_parsed_identically():
    expected = daily_frame(3)
    for text in (vendor_csv(3), bulk_txt(3)):
        ds = load_csv(io.StringIO(text))
        assert str(ds.bars.index.tz) == "UTC" and (ds.bars.index == expected.index).all()
        np.testing.assert_allclose(ds.bars[["open", "high", "low", "close"]].to_numpy(), expected[["open", "high", "low", "close"]].to_numpy())
        assert ds.bars.volume.sum() == pytest.approx(expected.volume.round().sum())
    own = "timestamp,open,high,low,close,volume\n2024-01-02T14:30:00+00:00,1,2,0.5,1.5,10\n"
    assert load_csv(io.StringIO(own)).bars.index[0] == pd.Timestamp("2024-01-02 14:30", tz="UTC")
    intraday = "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>\nX,60,20240102,153000,1,2,0.5,1.5,10\n"
    with pytest.raises(ValueError, match="timezone"):
        load_csv(io.StringIO(intraday))  # a time of day without a timezone is ambiguous
    assert load_csv(io.StringIO(intraday), tz="America/New_York").bars.index[0] == pd.Timestamp("2024-01-02 20:30", tz="UTC")
    no_volume = load_csv(io.StringIO("Date,Open,High,Low,Close\n2024-01-02,1,2,0.5,1.5\n"))
    assert no_volume.bars.volume.iloc[0] == 0
    for bad in ("Open,High\n1,2\n", "Date,Open,High,Low\n2024-01-02,1,2,0.5\n"):
        with pytest.raises(ValueError):
            load_csv(io.StringIO(bad))


def test_equity_instruments_per_market():
    us, it = make_equity("aapl", "US"), make_equity("ENI.MI", "IT")
    assert (us.id, us.quote_currency, us.calendar) == ("STOCK:AAPL", "USD", "XNYS")
    assert (it.id, it.symbol, it.quote_currency, it.calendar) == ("STOCK:ENI.MI", "ENI", "EUR", "XMIL")
    assert make_equity("spy", "US", AssetClass.ETF).id == "ETF:SPY"
    assert make_equity("BRK.B", "US").id == "STOCK:BRK.B"
    for ticker, market in (("", "US"), ("A B", "US"), ("AAPL", "ZZ"), ("../etc", "US"), ("X" * 20, "US")):
        with pytest.raises(ValueError):
            make_equity(ticker, market)
    with pytest.raises(ValueError):
        make_equity("BTC", "US", AssetClass.CRYPTO)


def test_add_list_remove_instruments_and_persistence(api, settings, db):
    client, admin = api
    assert {m["code"] for m in client.get("/api/markets", headers=admin).json()} >= {"US", "IT", "DE"}
    r = client.post("/api/instruments", headers=admin, json={"tickers": ["msft", "NVDA", "msft", "AAPL", "bad ticker!"], "market": "US"})
    assert r.json() == {"created": ["STOCK:MSFT", "STOCK:NVDA"], "existing": ["STOCK:AAPL"],
                        "invalid": [{"ticker": "bad ticker!", "reason": "'BAD TICKER!' is not a valid ticker"}]}
    client.post("/api/instruments", headers=admin, json={"tickers": ["ENI", "ISP"], "market": "IT"})
    listed = {i["id"]: i for i in client.get("/api/instruments", headers=admin).json()}
    assert listed["STOCK:ENI.MI"]["quote_currency"] == "EUR" and listed["STOCK:ENI.MI"]["removable"] and not listed["STOCK:AAPL"]["removable"]
    assert client.post("/api/instruments", headers=admin, json={"tickers": ["X"], "market": "ZZ"}).status_code == 400
    assert client.post("/api/instruments", headers=login(client, VIEWER), json={"tickers": ["X"], "market": "US"}).status_code == 403

    from app.main import create_app
    from fastapi.testclient import TestClient
    restarted = TestClient(create_app(settings, db))  # a new process sees the same universe
    ids = {i["id"] for i in restarted.get("/api/instruments", headers=login(restarted)).json()}
    assert {"STOCK:MSFT", "STOCK:NVDA", "STOCK:ENI.MI", "STOCK:ISP.MI"} <= ids

    assert client.delete("/api/instruments/STOCK:AAPL", headers=admin).status_code == 400  # built-in
    assert client.delete("/api/instruments/STOCK:NVDA", headers=admin).status_code == 204
    assert "STOCK:NVDA" not in {i["id"] for i in client.get("/api/instruments", headers=admin).json()}
    # A new instrument is immediately tradable on paper accounts.
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    client.post("/api/instruments", headers=admin, json={"tickers": ["TSLA"], "market": "US"})
    dep = client.post("/api/deployments", headers=admin, json={"strategy": "macd", "instrument_id": "STOCK:TSLA", "timeframe": "1d", "broker_id": "paper-main"})
    assert dep.status_code == 201
    assert client.delete("/api/instruments/STOCK:TSLA", headers=admin).status_code == 400  # in use by a deployment


def test_bulk_import_scan_and_backtest_on_stocks(api):
    client, admin = api
    files = [{"name": "AAPL.csv", "csv": vendor_csv(1)}, {"name": "msft.us.txt", "csv": bulk_txt(2, "MSFT.US")}, {"name": "NVDA.csv", "csv": vendor_csv(3)},
             {"name": "broken.csv", "csv": "Date,Open\n2024-01-01,1\n"}, {"name": "bad name!.csv", "csv": vendor_csv(4)}]
    r = client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": files})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [i["instrument_id"] for i in out["imported"]] == ["STOCK:AAPL", "STOCK:MSFT", "STOCK:NVDA"]
    assert all(i["bars"] > 600 and i["missing_bars"] == 0 for i in out["imported"])  # weekends are not gaps for a stock
    assert {f["file"] for f in out["failed"]} == {"broken.csv", "bad name!.csv"}
    again = client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": files[:1]}).json()
    assert again["imported"] == [] and "already has historical data" in again["failed"][0]["reason"]  # no silent overwrite
    replaced = client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": files[:1], "replace": True}).json()
    assert len(replaced["imported"]) == 1
    cov = {c["instrument_id"]: c for c in client.get("/api/market/coverage", headers=admin).json()}
    assert cov["STOCK:AAPL"]["kind"] == "historical" and cov["STOCK:AAPL"]["bars"] == out["imported"][0]["bars"]

    scan = client.post("/api/backtests/scan", headers=admin, json={"strategy": "ema_crossover", "params": {"fast": 10, "slow": 30}, "timeframe": "1d",
                                                                 "static_fx": {"USD": 0.92}})
    assert scan.status_code == 200, scan.text
    s = scan.json()
    assert [row["instrument_id"] for row in s["rows"]] == ["STOCK:AAPL", "STOCK:MSFT", "STOCK:NVDA"]
    assert s["summary"]["instruments_tested"] == 3 and 0 <= s["summary"]["share_profitable"] <= 1 and s["summary"]["total_trades"] > 10
    assert any("by chance" in n for n in s["notes"])
    picked = client.post("/api/backtests/scan", headers=admin, json={"strategy": "ema_crossover", "timeframe": "1d", "static_fx": {"USD": 0.92},
                                                                   "instrument_ids": ["STOCK:AAPL", "ETF:SPY"]}).json()
    assert picked["summary"] == {**picked["summary"], "instruments_tested": 1, "instruments_skipped": 1}
    assert "No stored bars" in picked["skipped"][0]["reason"]
    assert client.post("/api/backtests/scan", headers=admin, json={"strategy": "ema_crossover", "timeframe": "1h"}).status_code == 404

    one = client.post("/api/backtests", headers=admin, json={"deployments": [{"strategy": "ema_crossover", "instrument_id": "STOCK:AAPL", "timeframe": "1d",
                                                                             "params": {"fast": 10, "slow": 30}}], "static_fx": {"USD": 0.92}}).json()
    row = next(r for r in s["rows"] if r["instrument_id"] == "STOCK:AAPL")
    assert one["metrics"]["total_return"] == pytest.approx(row["total_return"])  # the scan and a single backtest agree
    assert one["data_kinds"] == {"STOCK:AAPL|1d": "historical"} and not any("SIMULATED" in w for w in one["warnings"])
    # Everything else in the research stack works on a user-added instrument too.
    q = {"instrument_id": "STOCK:NVDA", "timeframe": "1d"}
    nv = client.post("/api/backtests", headers=admin, json={"deployments": [{"strategy": "donchian_breakout", **q}], "static_fx": {"USD": 0.92}})
    assert nv.status_code == 200, nv.text
    assert client.post("/api/ai/reports", headers=admin, json={"backtest_id": nv.json()["id"]}).json()["payload"]["reproduced"] is True
    wf = client.post("/api/backtests/walk-forward", headers=admin, json={"strategy": "ema_crossover", **q, "param_grid": {"fast": [5, 10], "slow": [20, 30]},
                                                                         "n_folds": 2, "static_fx": {"USD": 0.92}})
    assert wf.status_code == 200, wf.text
    cmp_ = client.post("/api/backtests/compare", headers=admin, json={"static_fx": {"USD": 0.92}, "items": [{"strategy": "macd", **q}, {"strategy": "rsi_reversion", **q}]})
    assert cmp_.status_code == 200, cmp_.text
    assert client.get("/api/market/analysis", headers=admin, params=q).json()["kind"] == "historical"
    trade_days = {pd.Timestamp(t["entry_ts"]).day_of_week for t in one["trades"]}
    assert trade_days and trade_days <= {0, 1, 2, 3, 4}  # fills only on trading days


def test_instrument_list_shows_the_last_stored_price_and_says_what_it_is(client):
    admin = login(client)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000).model_dump())
    client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": [{"name": "MSFT.csv", "csv": vendor_csv(1)}]})
    rows = {r["id"]: r for r in client.get("/api/instruments", headers=admin).json()}
    msft = rows["STOCK:MSFT"]
    expected = daily_frame(1)
    assert msft["last_price"] == pytest.approx(expected["close"].iloc[-1] * 0.97, rel=1e-3) or msft["last_price"] == pytest.approx(expected["close"].iloc[-1], rel=1e-3)
    assert msft["last_price_source"] == "last stored bar" and msft["last_price_at"][:10] == expected.index[-1].date().isoformat()
    assert rows["STOCK:AAPL"]["last_price"] is None and rows["STOCK:AAPL"]["last_price_source"] is None  # nothing stored: nothing shown

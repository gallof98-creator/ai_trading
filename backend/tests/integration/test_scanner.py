"""Phase 3 of the global module: daily metrics, scanner, saved scans, watchlists."""
import time

import numpy as np
import pandas as pd
import pytest

from app.database.settings_store import UserSettings
from app.global_markets import scanner
from app.global_markets.metrics import FIELDS, compute_metrics
from app.market_data.sources import synthetic_bars
from tests.conftest import VIEWER, login
from tests.integration.test_universe import vendor_csv


def bars(close, volume=None, open_=None, high=None, low=None):
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range("2024-01-01", periods=len(close), tz="UTC", name="ts")
    o = np.concatenate([[close[0]], close[:-1]]) if open_ is None else np.asarray(open_, dtype=float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, close) if high is None else high, "low": np.minimum(o, close) if low is None else low,
                         "close": close, "volume": 1000.0 if volume is None else np.asarray(volume, dtype=float)}, index=idx)


def test_metrics_match_hand_calculation():
    close = np.concatenate([np.full(280, 100.0), [100, 100, 100, 100, 110.0]])  # flat for a long time, then +10% on the last bar
    volume = np.concatenate([np.full(284, 1000.0), [5000.0]])
    open_ = np.concatenate([[100.0], close[:-1]])
    open_[-1] = 104.0  # gapped up 4% at the open
    m = compute_metrics(bars(close, volume, open_))
    assert m["close"] == 110 and m["change_1d"] == pytest.approx(0.10) and m["gap"] == pytest.approx(0.04)
    assert m["avg_volume_20"] == 1000 and m["rel_volume"] == pytest.approx(5.0)
    assert m["ret_5d"] == pytest.approx(0.10) and m["ret_1m"] == pytest.approx(0.10) and m["ret_12m"] == pytest.approx(0.10)
    assert m["mom_12_1"] == pytest.approx(0.0)  # the 12-1 window ends a month ago, before the jump
    assert m["dist_sma_20"] == pytest.approx(110 / 100.5 - 1) and m["dist_sma_200"] == pytest.approx(110 / 100.05 - 1)
    assert m["breakout_20"] is True and m["breakout_55"] is True and m["breakdown_20"] is False
    assert m["dist_high_52w"] == pytest.approx(0.0) and m["trend_up"] is True and m["trend_down"] is False
    assert m["rsi_14"] == 100 and m["bars"] == 285
    assert m["dollar_volume_20"] == pytest.approx((19 * 100 * 1000 + 110 * 5000) / 20)
    assert set(m) == set(FIELDS)


def test_metrics_without_enough_history_are_empty_not_invented():
    assert compute_metrics(bars(np.linspace(10, 12, 20))) is None
    m = compute_metrics(bars(np.linspace(10, 12, 60)))
    assert m["ret_1m"] is not None and m["ret_3m"] is None and m["ret_12m"] is None and m["mom_12_1"] is None
    assert m["dist_sma_200"] is None and m["trend_up"] is None and m["dist_high_52w"] is None and m["breakout_55"] is not None


def test_metrics_use_only_past_bars():
    full = synthetic_bars("1d", 500, seed=3).bars
    tampered = full.copy()
    tampered.iloc[400:] *= 3
    assert compute_metrics(full.iloc[:400]) == compute_metrics(tampered.iloc[:400])


FRAME = pd.DataFrame([
    {"instrument_id": "STOCK:AAA", "symbol": "AAA", "market": "US", "country": "US", "currency": "USD", "asset_type": "common_stock", "sector": "Tech",
     "change_1d": 0.05, "rel_volume": 3.0, "rsi_14": 72.0, "breakout_20": True, "ret_6m": 0.40, "regime": "strong_uptrend"},
    {"instrument_id": "STOCK:BBB", "symbol": "BBB", "market": "US", "country": "US", "currency": "USD", "asset_type": "common_stock", "sector": "Tech",
     "change_1d": -0.02, "rel_volume": 0.8, "rsi_14": 25.0, "breakout_20": False, "ret_6m": -0.10, "regime": "sideways"},
    {"instrument_id": "ETF:CCC", "symbol": "CCC", "market": "US", "country": "US", "currency": "USD", "asset_type": "etf", "sector": None,
     "change_1d": 0.01, "rel_volume": None, "rsi_14": 55.0, "breakout_20": None, "ret_6m": 0.05, "regime": None},
    {"instrument_id": "STOCK:ENI.MI", "symbol": "ENI", "market": "IT", "country": "IT", "currency": "EUR", "asset_type": "common_stock", "sector": "Energy",
     "change_1d": 0.03, "rel_volume": 2.2, "rsi_14": 61.0, "breakout_20": True, "ret_6m": 0.20, "regime": "strong_uptrend"},
])
for _col in list(FIELDS) + ["rel_strength", "group_ret_1m", "industry"]:
    if _col not in FRAME:
        FRAME[_col] = None
FRAME["rel_strength"] = FRAME["ret_6m"].rank(pct=True) * 100


def ids(definition, **kw):
    return [r["instrument_id"] for r in scanner.run_scan(FRAME, definition, **kw)["rows"]]


def test_scan_filters_sorting_and_universe_selection():
    assert ids({"filters": [{"field": "change_1d", "op": ">", "value": 0}], "sort": {"field": "change_1d", "descending": True}}) == ["STOCK:AAA", "STOCK:ENI.MI", "ETF:CCC"]
    assert ids({"filters": [{"field": "rel_volume", "op": ">=", "value": 2}]}) == ["STOCK:AAA", "STOCK:ENI.MI"]  # the ETF has no value: it does not match
    assert ids({"filters": [{"field": "rel_volume", "op": "<", "value": 2}]}) == ["STOCK:BBB"]  # ...in either direction
    assert ids({"filters": [{"field": "breakout_20", "op": "is_true"}, {"field": "rsi_14", "op": "<", "value": 70}]}) == ["STOCK:ENI.MI"]
    assert ids({"filters": [{"field": "breakout_20", "op": "is_false"}]}) == ["STOCK:BBB"]
    assert ids({"filters": [{"field": "regime", "op": "==", "value": "strong_uptrend"}]}) == ["STOCK:AAA", "STOCK:ENI.MI"]
    assert ids({"filters": [{"field": "sector", "op": "in", "value": ["Energy", "Utilities"]}]}) == ["STOCK:ENI.MI"]
    assert ids({"universe": {"markets": ["IT"]}}) == ["STOCK:ENI.MI"] and ids({"universe": {"countries": ["US"], "asset_types": ["etf"]}}) == ["ETF:CCC"]
    assert ids({"universe": {"watchlist": ["mine"]}}, watchlists={"mine": ["STOCK:BBB", "ETF:CCC"]}) == ["STOCK:BBB", "ETF:CCC"]
    assert ids({"sort": {"field": "rel_strength", "descending": True}, "limit": 2}) == ["STOCK:AAA", "STOCK:ENI.MI"]
    out = scanner.run_scan(FRAME, {"universe": {"asset_types": ["common_stock"]}, "filters": [{"field": "rsi_14", "op": "<", "value": 30}]})
    assert (out["total_universe"], out["matched"]) == (3, 1)
    assert scanner.run_scan(pd.DataFrame(), {})["rows"] == []


def test_scan_definitions_are_validated():
    bad = [{"filters": [{"field": "nope", "op": ">", "value": 1}]}, {"filters": [{"field": "rsi_14", "op": "~", "value": 1}]},
           {"filters": [{"field": "rsi_14", "op": ">", "value": "high"}]}, {"filters": [{"field": "breakout_20", "op": ">", "value": 1}]},
           {"filters": [{"field": "rsi_14", "op": "is_true"}]}, {"filters": [{"field": "regime", "op": ">", "value": "x"}]},
           {"filters": [{"field": "sector", "op": "in", "value": "Tech"}]}, {"sort": {"field": "nope"}}, {"limit": 0}, {"limit": 99999},
           {"universe": {"markets": "US"}}, "not an object"]
    for definition in bad:
        with pytest.raises(scanner.ScanError):
            scanner.validate_definition(definition)
    for name, preset in scanner.PRESETS.items():
        assert scanner.validate_definition(preset)["filters"], name


# ---------------------------------------------------------------- through the API
def wait_metrics(client):
    gm = client.app.state.container.runtime.global_markets
    for _ in range(1500):
        if gm.metrics_job is not None and gm.metrics_job.completed.is_set():
            return gm.metrics_job.snapshot()
        time.sleep(0.02)
    raise AssertionError("metrics job did not finish")


@pytest.fixture
def api(client):
    admin = login(client)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000).model_dump())
    files = [{"name": f"{t}.csv", "csv": vendor_csv(i)} for i, t in enumerate(("MSFT", "NVDA", "AMZN", "KO", "XOM", "JPM"), start=1)]
    assert len(client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": files}).json()["imported"]) == 6
    return client, admin


def test_metrics_job_scan_saved_scans_and_watchlists_over_http(api):
    client, admin = api
    assert client.post("/api/scanner/run", headers=admin, json={"definition": {}}).json() == {"total_universe": 0, "matched": 0, "rows": [], "definition": {"filters": [], "universe": {}, "limit": 100}}
    fields = client.get("/api/scanner/fields", headers=admin).json()
    assert {f["name"] for f in fields["metrics"]} == set(FIELDS) and "Unusual volume" in fields["presets"]

    assert client.post("/api/scanner/metrics", headers=admin, json={}).json()["total"] == 6
    st = wait_metrics(client)
    assert (st["status"], st["computed"], st["failed"]) == ("finished", 6, 0)
    client.post("/api/scanner/metrics", headers=admin, json={})
    assert wait_metrics(client)["skipped"] == 6  # nothing changed: nothing recomputed

    everything = client.post("/api/scanner/run", headers=admin, json={"definition": {"sort": {"field": "rel_strength", "descending": True}}}).json()
    assert everything["matched"] == 6 and everything["rows"][0]["rel_strength"] == 100 and everything["rows"][-1]["rel_strength"] == pytest.approx(100 / 6)
    assert all(r["data_kind"] == "historical" and r["market"] == "US" and r["as_of"] for r in everything["rows"])
    ret = [r["ret_6m"] for r in everything["rows"]]
    assert ret == sorted(ret, reverse=True)  # the rank really follows the 6-month return
    up = client.post("/api/scanner/run", headers=admin, json={"definition": {"filters": [{"field": "change_1d", "op": ">", "value": 0}]}}).json()
    down = client.post("/api/scanner/run", headers=admin, json={"definition": {"filters": [{"field": "change_1d", "op": "<=", "value": 0}]}}).json()
    assert up["matched"] + down["matched"] == 6
    assert client.post("/api/scanner/run", headers=admin, json={"definition": {"filters": [{"field": "nope", "op": ">", "value": 1}]}}).status_code == 400

    # Watchlists
    wl = client.put("/api/scanner/watchlists", headers=admin, json={"name": "energy and banks", "instrument_ids": ["STOCK:XOM", "STOCK:JPM", "STOCK:XOM"]}).json()
    assert wl["instrument_ids"] == ["STOCK:XOM", "STOCK:JPM"]
    assert client.put("/api/scanner/watchlists", headers=admin, json={"name": "bad", "instrument_ids": ["STOCK:NOPE"]}).status_code == 400
    only = client.post("/api/scanner/run", headers=admin, json={"definition": {"universe": {"watchlist": ["energy and banks"]}}}).json()
    assert {r["instrument_id"] for r in only["rows"]} == {"STOCK:XOM", "STOCK:JPM"}

    # Saved scan, triggered by the next data update
    viewer = login(client, VIEWER)
    body = {"name": "everything moving", "definition": {"filters": [{"field": "bars", "op": ">", "value": 100}]}, "run_after_update": True}
    assert client.post("/api/scanner/saved", headers=viewer, json=body).status_code == 403
    saved = client.post("/api/scanner/saved", headers=admin, json=body).json()
    assert saved["last_run"] is None and client.post("/api/scanner/saved", headers=admin, json={**body, "definition": {"limit": 0}}).status_code == 400
    client.post("/api/notifications/read-all", headers=admin)
    client.post("/api/scanner/metrics", headers=admin, json={"force": True})
    st = wait_metrics(client)
    assert st["scans"] == [{"name": "everything moving", "matched": 6}]
    again = client.get("/api/scanner/saved", headers=admin).json()[0]
    assert again["last_result"]["matched"] == 6 and len(again["last_result"]["top"]) == 6 and again["last_run"]
    notes = client.get("/api/notifications", headers=admin).json()["items"]
    assert any(n["category"] == "scanner" and "6 matches" in n["title"] for n in notes)
    assert client.delete(f"/api/scanner/saved/{saved['id']}", headers=admin).status_code == 204
    assert client.delete(f"/api/scanner/watchlists/{wl['id']}", headers=admin).status_code == 204
    assert client.get("/api/scanner/saved", headers=admin).json() == [] and client.get("/api/scanner/watchlists", headers=admin).json() == []


def test_removed_instruments_drop_out_of_scans(api):
    client, admin = api
    client.post("/api/scanner/metrics", headers=admin, json={})
    wait_metrics(client)
    assert client.delete("/api/instruments/STOCK:KO", headers=admin).status_code == 204
    out = client.post("/api/scanner/run", headers=admin, json={"definition": {}}).json()
    assert out["matched"] == 5 and "STOCK:KO" not in {r["instrument_id"] for r in out["rows"]}

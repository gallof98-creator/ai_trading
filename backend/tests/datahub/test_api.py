"""The data platform over HTTP: roles, ingestion, point-in-time views, features, the bridge to backtesting."""
import httpx
import pandas as pd
import pytest

from tests.conftest import VIEWER, login

from . import fakes

BTC = "CRYPTO:BTC/USDT"
START = pd.Timestamp(fakes.T0, unit="ms", tz="UTC")


@pytest.fixture
def api(client, settings):
    settings.fred_api_key, settings.sec_user_agent = "k" * 32, "Research Desk desk@example.com"
    hub = client.app.state.container.datahub
    exchanges: dict = {}
    hub.exchange_factory = lambda name: exchanges.setdefault(name, fakes.Exchange(history=3000, base=100.0 if name == "binance" else 100.3))
    hub.transports.update(fred=httpx.MockTransport(fakes.Fred()), sec=httpx.MockTransport(fakes.Sec()))
    return client, login(client), hub


def ingest(client, headers, **body) -> dict:
    r = client.post("/api/data/ingest", headers=headers, json={"wait": True, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_reads_need_a_login_and_writes_need_a_trader(api):
    client, admin, _ = api
    for path in ("/api/data/providers", "/api/data/datasets", "/api/data/catalog", "/api/data/runs", "/api/data/health", "/api/data/features", "/api/data/context"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=admin).status_code == 200, path
    viewer = login(client, VIEWER)
    assert client.get("/api/data/providers", headers=viewer).status_code == 200
    for path, body in (("/api/data/ingest", {"provider": "fred", "dataset": "macro"}), ("/api/data/providers/fred/test", None),
                       ("/api/data/promote", {"source": "binance", "symbol": "BTC/USDT", "timeframe": "1m", "instrument_id": BTC})):
        assert client.post(path, headers=viewer, json=body).status_code == 403, path


def test_providers_datasets_and_refusals(api):
    client, admin, _ = api
    providers = {p["name"]: p for p in client.get("/api/data/providers", headers=admin).json()}
    assert set(providers) == {"fred", "sec", "kraken", "binance", "coingecko", "fmp", "alpaca"}
    assert providers["fred"]["ready"] and not providers["fmp"]["ready"] and providers["fmp"]["daily_budget"] == 240
    assert "delayed" in providers["alpaca"]["timeliness"].lower() and "720" in providers["kraken"]["history"]
    described = client.get("/api/data/datasets", headers=admin).json()
    assert {"bars", "macro", "fundamentals", "orderbook"} <= {d["name"] for d in described["datasets"]} and described["sources"]["sec"] == ["fundamentals", "filings"]
    assert {f["id"] for f in described["feature_sets"]} >= {"bars_intraday@v1", "macro@v1"}
    assert client.post("/api/data/ingest", headers=admin, json={"provider": "fmp", "dataset": "company_profile", "keys": ["AAPL"]}).status_code == 409
    assert client.post("/api/data/ingest", headers=admin, json={"provider": "nobody", "dataset": "bars"}).status_code == 400
    assert client.post("/api/data/providers/nobody/test", headers=admin).status_code == 404
    assert client.get("/api/data/bars", headers=admin, params={"source": "binance", "symbol": "BTC/USDT", "timeframe": "1m"}).status_code == 404
    assert client.get("/api/data/macro", headers=admin).status_code == 404
    probe = client.post("/api/data/providers/fred/test", headers=admin).json()
    assert probe["ok"] is True and probe["requests"] == 1
    assert client.get("/api/data/health", headers=admin).json()["fred"]["availability"] == 1.0


def test_background_ingestion_then_bars_features_and_cross_venue_comparison(api):
    client, admin, hub = api
    job = client.post("/api/data/ingest", headers=admin, json={"provider": "binance", "dataset": "bars", "keys": ["BTC/USDT"], "timeframe": "1m",
                                                               "start": START.isoformat(), "end": (START + pd.Timedelta(minutes=1999)).isoformat()})
    assert job.status_code == 200 and job.json()["status"] == "running"
    hub.jobs[job.json()["id"]]["_thread"].join(timeout=60)
    done = client.get(f"/api/data/jobs/{job.json()['id']}", headers=admin).json()
    assert done["status"] == "finished" and done["result"]["rows_new"] == 2000
    ingest(client, admin, provider="kraken", dataset="bars", keys=["BTC/USD"], timeframe="1m", start=START.isoformat(),
           end=(START + pd.Timedelta(minutes=1999)).isoformat())

    bars = client.get("/api/data/bars", headers=admin, params={"source": "binance", "symbol": "BTC/USDT", "timeframe": "1m", "limit": 5}).json()
    assert len(bars["rows"]) == 5 and bars["rows"][-1]["ts"] == (START + pd.Timedelta(minutes=1999)).isoformat() and bars["rows"][0]["source"] == "binance"
    catalog = client.get("/api/data/catalog", headers=admin, params={"dataset": "bars"}).json()
    assert {(c["provider"], c["rows"]) for c in catalog} == {("binance", 2000), ("kraken", 2000)}
    runs = client.get("/api/data/runs", headers=admin, params={"provider": "binance"}).json()
    assert runs[0]["rows_new"] == 2000 and runs[0]["quality"]["ok"] is True and runs[0]["actor"] == "admin@example.com"

    feats = client.get("/api/data/features/bars_intraday", headers=admin, params={"source": "binance", "symbol": "BTC/USDT", "timeframe": "1m", "limit": 3}).json()
    assert feats["feature_set"] == "bars_intraday@v1" and len(feats["rows"]) == 3 and feats["rows"][-1]["rv"] is not None
    assert client.get("/api/data/features/macro", headers=admin, params={"source": "x", "symbol": "y"}).status_code == 400

    compared = client.get("/api/data/compare", headers=admin, params={"series": "binance:BTC/USDT,kraken:BTC/USD", "timeframe": "1m", "limit": 2}).json()
    assert compared["common_bars"] == 2000 and compared["feature_set"] == "cross_venue@v1"
    last = compared["rows"][-1]
    assert last["close_binance:BTC/USDT"] != last["close_kraken:BTC/USD"]  # two venues, two prices, shown side by side
    assert last["div_bps_binance:BTC/USDT__kraken:BTC/USD"] == pytest.approx((last["close_binance:BTC/USDT"] / last["close_kraken:BTC/USD"] - 1) * 1e4)
    assert client.get("/api/data/compare", headers=admin, params={"series": "binance:BTC/USDT"}).status_code == 400

    ingest(client, admin, provider="kraken", dataset="orderbook", keys=["BTC/USD"])
    book = client.get("/api/data/features/orderbook", headers=admin, params={"source": "kraken", "symbol": "BTC/USD"}).json()
    assert book["rows"][0]["spread_bps"] > 0 and -1 <= book["rows"][0]["imbalance_all"] <= 1


def test_macro_and_fundamentals_are_served_as_known_at_a_point_in_time(api):
    client, admin, _ = api
    out = ingest(client, admin, provider="fred", dataset="macro", keys=["CPIAUCSL", "DGS10", "DGS2"])
    assert out["ok"] == 3

    def cpi(asof, **extra):
        state = client.get("/api/data/macro", headers=admin, params={"asof": asof, **extra}).json()
        return next(s for s in state["series"] if s["series_id"] == "CPIAUCSL"), state

    before, _ = cpi("2024-02-13T22:59:00Z")
    after, state = cpi("2024-02-13T23:00:00Z")
    assert (before["value"], before["observation_date"]) == (300.84, "2023-02-01") and (after["value"], after["observation_date"]) == (308.417, "2024-01-01")
    assert state["derived"]["CPIAUCSL_yoy"] == pytest.approx(308.417 / 299.170 - 1) and "FRED" in state["attribution"]
    morning, _ = cpi("2024-02-13T13:30:00Z", scheduled_release_times=True)
    assert morning["value"] == 308.417  # with the scheduled 08:30 New York release time instead of the conservative evening
    today, state = cpi("2026-11-14T00:00:00Z")
    assert today["value"] == 310.326 and state["derived"]["yc_10y_2y"] == pytest.approx(4.19 - 4.61)

    current = client.get("/api/data/macro/CPIAUCSL", headers=admin).json()["rows"]
    vintages = client.get("/api/data/macro/CPIAUCSL", headers=admin, params={"vintages": True}).json()["rows"]
    assert len(current) == 4 and len(vintages) == 5 and [r["value"] for r in current if r["date"].startswith("2024-01")] == [309.685]

    ingest(client, admin, provider="sec", dataset="fundamentals", keys=["AAPL"])

    def revenue(asof):
        rows = client.get("/api/data/fundamentals", headers=admin, params={"ticker": "aapl", "asof": asof, "concepts": "Revenues"}).json()["rows"]
        return [r["value"] for r in rows if r["period_end"].startswith("2023-12-30")]

    assert revenue("2024-02-01T23:00:00Z") == [] and revenue("2024-06-30T00:00:00Z") == [119575000000] and revenue("2025-02-01T00:00:00Z") == [119600000000]
    filings = client.get("/api/data/filings", headers=admin, params={"ticker": "AAPL", "forms": "10-K"}).json()["rows"]
    assert len(filings) == 1 and filings[0]["accepted_at"] == "2024-11-01T10:01:36+00:00"
    assert client.get("/api/data/fundamentals", headers=admin, params={"ticker": "MSFT"}).status_code == 404

    context = client.get("/api/data/context", headers=admin, params={"asof": "2024-03-15T00:00:00Z"}).json()
    assert context["macro"]["series"] and context["coverage"]["macro"]["series"] == 3 and context["coverage"]["fundamentals"]["rows"] == 5
    assert any("revisions" in c for c in context["caveats"]) and len(context["attribution"]) == 2


def test_a_lake_series_can_feed_backtests_but_never_replaces_bars_in_use(api):
    client, admin, hub = api
    ingest(client, admin, provider="binance", dataset="bars", keys=["BTC/USDT"], timeframe="1m", start=START.isoformat(),
           end=(START + pd.Timedelta(minutes=2999)).isoformat())
    body = {"source": "binance", "symbol": "BTC/USDT", "timeframe": "1m", "instrument_id": BTC}
    promoted = client.post("/api/data/promote", headers=admin, json=body)
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["bars_stored"] == 3000 and promoted.json()["quality"]["ok"] is True
    coverage = [c for c in client.get("/api/market/coverage", headers=admin).json() if c["instrument_id"] == BTC]
    assert coverage == [{**coverage[0], "timeframe": "1m", "bars": 3000, "kind": "historical", "source": "lake:binance:BTC/USDT"}]

    run = client.post("/api/backtests", headers=admin, json={"label": "lake data", "static_fx": {"USDT": 0.92},
                                                            "deployments": [{"strategy": "ema_crossover", "params": {"fast": 5, "slow": 20},
                                                                             "instrument_id": BTC, "timeframe": "1m"}]})
    assert run.status_code == 200, run.text  # the existing backtester runs on the promoted series, unchanged
    assert not any("SIMULATED" in w for w in run.json()["warnings"])

    again = client.post("/api/data/promote", headers=admin, json=body)
    assert again.status_code == 400 and "already has" in again.json()["detail"]  # never merged silently
    assert client.post("/api/data/promote", headers=admin, json={**body, "replace": True, "end": (START + pd.Timedelta(minutes=99)).isoformat()}).json()["bars_stored"] == 100
    assert client.post("/api/data/promote", headers=admin, json={**body, "symbol": "DOGE/USDT"}).status_code == 404
    assert client.post("/api/data/promote", headers=admin, json={**body, "instrument_id": "CRYPTO:NOPE"}).status_code == 404

    # An instrument a strategy is deployed on keeps the bars of its own feed.
    assert client.post("/api/paper/accounts", headers=admin, json={"id": "p1", "label": "paper"}).status_code in (200, 201)
    deployed = client.post("/api/deployments", headers=admin, json={"strategy": "ema_crossover", "params": {"fast": 5, "slow": 20}, "instrument_id": BTC,
                                                                    "timeframe": "1m", "broker_id": "p1"})
    assert deployed.status_code in (200, 201), deployed.text
    refused = client.post("/api/data/promote", headers=admin, json={**body, "replace": True})
    assert refused.status_code == 409 and "in use" in refused.json()["detail"]

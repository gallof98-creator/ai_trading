"""Chart datafeed: symbol resolution, history at every resolution, studies, and paper orders from the chart."""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.brokers.ccxt_adapter import CcxtAdapter
from app.charts import datafeed
from app.database.models import Security
from app.domain.models import OrderType
from app.execution.runtime import MANUAL_STRATEGY, RuntimeError_
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset
from tests.conftest import VIEWER, login
from tests.execution.fakes import FakeCcxt
from tests.integration.test_cross_section import DATES, TICKERS, next_bar, rt, series  # noqa: F401  (rt is a fixture)

AAA = "STOCK:AAA"


def minute_bars(n=3 * 390, start="2024-03-04 14:30"):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC", name="ts")
    close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.05, n))
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.01, "low": np.minimum(open_, close) - 0.01, "close": close, "volume": 100.0}, index=idx)


def test_history_is_built_only_from_stored_bars(rt):
    with rt.db.session() as s:
        daily = rt.store.load(s, AAA, "1d").bars
        info = datafeed.resolve(s, rt.registry, AAA)
        assert info["supported_resolutions"] == ["1D", "1W", "1M"] and info["data"]["kind"] == "historical" and info["stored_timeframes"] == ["1d"]
        d = datafeed.history(s, AAA, "1D", None, None, countback=50)
        assert d["s"] == "ok" and d["t"] == [int(t.timestamp()) for t in daily.index[-50:]] and d["c"] == daily["close"].iloc[-50:].tolist()
        lo, hi = int(daily.index[100].timestamp()), int(daily.index[110].timestamp())
        assert datafeed.history(s, AAA, "1D", lo, hi)["t"] == [int(t.timestamp()) for t in daily.index[100:111]]

        w = datafeed.history(s, AAA, "1W", None, None, countback=1000)
        week = daily[(daily.index >= pd.Timestamp(w["t"][5], unit="s", tz="UTC")) & (daily.index < pd.Timestamp(w["t"][6], unit="s", tz="UTC"))]
        assert (w["o"][5], w["h"][5], w["l"][5], w["c"][5], w["v"][5]) == (week.open.iloc[0], week.high.max(), week.low.min(), week.close.iloc[-1], week.volume.sum())
        m = datafeed.history(s, AAA, "1M", None, None, countback=1000)
        first = pd.Timestamp(m["t"][3], unit="s", tz="UTC")
        month = daily[(daily.index.year == first.year) & (daily.index.month == first.month)]
        assert first.day == 1 and (m["o"][3], m["c"][3], m["h"][3]) == (month.open.iloc[0], month.close.iloc[-1], month.high.max())
        assert sum(m["v"]) == pytest.approx(daily.volume.sum())

        # Intraday is never invented from daily bars.
        with pytest.raises(datafeed.DatafeedError, match="No stored bars can build"):
            datafeed.history(s, AAA, "60", None, None, countback=10)
        with pytest.raises(datafeed.DatafeedError, match="Unsupported resolution"):
            datafeed.history(s, AAA, "7", None, None, countback=10)
        before = datafeed.history(s, AAA, "1D", 1, int(daily.index[0].timestamp()) - 86400)
        assert before == {"s": "no_data", "nextTime": None}
        gap_to = int(daily.index[-1].timestamp()) + 30 * 86400
        assert datafeed.history(s, AAA, "1D", gap_to - 86400, gap_to) == {"s": "no_data", "nextTime": int(daily.index[-1].timestamp())}

    with rt.db.session() as s:
        rt.store.save(s, "STOCK:BBB", "1m", Dataset(minute_bars(), DataKind.DELAYED, "test-feed"))
    with rt.db.session() as s:
        info = datafeed.resolve(s, rt.registry, "STOCK:BBB")
        assert info["supported_resolutions"] == ["1", "5", "15", "30", "60", "240", "1D", "1W", "1M"] and info["data"]["kind"] == "delayed"
        five = datafeed.history(s, "STOCK:BBB", "5", None, int(pd.Timestamp("2024-03-04 15:00", tz="UTC").timestamp()), countback=3)
        raw = minute_bars()
        chunk = raw[(raw.index >= pd.Timestamp(five["t"][0], unit="s", tz="UTC")) & (raw.index < pd.Timestamp(five["t"][1], unit="s", tz="UTC"))]
        assert len(chunk) == 5 and five["o"][0] == chunk.open.iloc[0] and five["c"][0] == chunk.close.iloc[-1] and five["v"][0] == 500


def test_symbols_resolve_by_every_identifier(rt):
    with rt.db.session() as s:
        s.add(Security(ticker="AAA", name="Alpha Corp", exchange="US", mic="XNYS", country="US", currency="USD", security_type="common_stock", isin="US0000000001",
                       figi="BBG000000001", sector="Technology", instrument_id=AAA, sources={"finnhub": {"symbol": "AAA"}}))
        s.add(Security(ticker="AAA", name="Alpha Italia", exchange="MI", mic="XMIL", country="IT", currency="EUR", security_type="common_stock", isin="IT0000000001"))
        s.add(Security(ticker="ZZZ", name="Not in universe", exchange="US", country="US", currency="USD", security_type="etf"))
    with rt.db.session() as s:
        for query in (AAA, "US0000000001", "BBG000000001", "US:AAA", "aaa"):
            info = datafeed.resolve(s, rt.registry, query)
            assert info["instrument_id"] == AAA and info["description"] == "Alpha Corp" and info["mic"] == "XNYS", query
        assert datafeed.resolve(s, rt.registry, "AAA")["other_matches"] == 1  # the Milan listing exists too and is not silently merged
        milan = datafeed.resolve(s, rt.registry, "MI:AAA")
        assert milan["instrument_id"] is None and milan["has_data"] is False and "not in the trading universe" in milan["data"]["note"]
        assert milan["symbol"].startswith("sec:") and datafeed.resolve(s, rt.registry, milan["symbol"])["isin"] == "IT0000000001"
        assert datafeed.resolve(s, rt.registry, "AAA")["provider_symbols"] == {"finnhub": "AAA"}
        with pytest.raises(datafeed.DatafeedError):
            datafeed.resolve(s, rt.registry, "NOPE")

        found = datafeed.search(s, rt.registry, "AAA")
        assert [r["symbol"] for r in found][0] == AAA and found[0]["has_data"] and len(found) == 2 and not found[1]["in_universe"]
        assert {r["ticker"] for r in datafeed.search(s, rt.registry, "", types=["etf"])} >= {"ZZZ"}
        assert [r["ticker"] for r in datafeed.search(s, rt.registry, "", sector="technology")] == ["AAA"]
        assert any(r["type"] == "crypto" for r in datafeed.search(s, rt.registry, "BTC"))  # instruments outside the catalog are searchable too
        assert all(r["has_data"] for r in datafeed.search(s, rt.registry, "", has_data=True, limit=50))


def test_studies_are_causal_and_levels_come_from_confirmed_pivots(rt):
    with rt.db.session() as s:
        out = datafeed.studies(s, AAA, "1D", 200)
        daily = rt.store.load(s, AAA, "1d").bars
    assert len(out["t"]) == 200 and all(len(v) == 200 for v in {**out["overlays"], **out["panes"]}.values())
    assert out["overlays"]["sma_20"][-1] == pytest.approx(daily.close.iloc[-20:].mean(), rel=1e-6)
    assert out["overlays"]["sma_200"][-1] == pytest.approx(daily.close.iloc[-200:].mean(), rel=1e-6) and out["trend"] in ("up", "down", "mixed")
    last = daily.close.iloc[-1]
    for lv in out["levels"]:
        assert (lv["price"] < last) == (lv["kind"] == "support") and lv["touches"] >= 1
    # A level never depends on bars after the confirmation window: cutting the last 3 bars cannot create a level they alone would explain.
    wave = 100 + 10 * np.sin(np.arange(300) / 8)
    bars = pd.DataFrame({"open": wave, "high": wave + 0.5, "low": wave - 0.5, "close": wave, "volume": 1.0}, index=pd.bdate_range("2023-01-02", periods=300, tz="UTC"))
    levels = datafeed.support_resistance(bars)
    assert {l["kind"] for l in levels} == {"support", "resistance"} and any(abs(l["price"] - 110.5) < 1 for l in levels) and any(abs(l["price"] - 89.5) < 1 for l in levels)
    spike = bars.copy()
    spike.iloc[-2:, :4] = 500.0  # an unconfirmed extreme in the last two bars is not a level yet
    assert not any(l["price"] > 200 for l in datafeed.support_resistance(spike))
    assert datafeed.support_resistance(bars.iloc[:10]) == []


def test_chart_orders_are_paper_only_and_pass_the_risk_engine(rt):
    price = rt.chart_order("paper-main", AAA, "buy", 10, 0.1, "admin")
    assert price["status"] == "submitted" and price["stop_price"] == pytest.approx(price["reference_price"] * 0.9, abs=0.01)
    with pytest.raises(RuntimeError_, match="protective stop"):
        rt.chart_order("paper-main", AAA, "buy", 10, None, "admin")
    with pytest.raises(RuntimeError_, match="no position"):
        rt.chart_order("paper-main", "STOCK:CCC", "close", None, None, "admin")
    with pytest.raises(RuntimeError_, match="not in the trading universe"):
        rt.chart_order("paper-main", "STOCK:NOPE", "buy", 1, 0.1, "admin")
    rt.brokers.register(CcxtAdapter("live-x", rt.registry, FakeCcxt()))
    with pytest.raises(RuntimeError_, match="paper accounts only"):
        rt.chart_order("live-x", AAA, "buy", 1, 0.1, "admin")
    rt.brokers.remove("live-x")
    # Too large for the 1% risk-per-trade limit: 10% stop on a position worth far more than 10% of equity.
    big = rt.chart_order("paper-main", "STOCK:BBB", "buy", 5000, 0.1, "admin")
    assert big["status"] == "rejected" and "risk" in big["reason"]
    rt.kill_switch(True, "test", "admin")
    assert "kill switch" in rt.chart_order("paper-main", "STOCK:CCC", "buy", 1, 0.1, "admin")["reason"]
    rt.kill_switch(False, "", "admin")

    assert rt.drive_stored()["bars"] == 0  # no bar after the order: nothing fills on an old price
    day1 = DATES[-1] + pd.offsets.BDay(1)
    next_bar(rt, day1)
    rt.test_clock.now = day1.to_pydatetime() + timedelta(hours=22)
    rt.drive_stored()
    key = ("paper-main", AAA, MANUAL_STRATEGY)
    assert rt.portfolio.qty(key) == 10
    stops = [o for o in rt.engine.orders.values() if o.is_open and o.type is OrderType.STOP and o.instrument_id == AAA]
    assert len(stops) == 1 and stops[0].stop_price == pytest.approx(rt.portfolio.slices[key].avg_price * 0.9, rel=1e-3)
    assert rt.chart_order("paper-main", AAA, "close", None, None, "admin")["status"] == "submitted"
    day2 = day1 + pd.offsets.BDay(1)
    next_bar(rt, day2)
    rt.test_clock.now = day2.to_pydatetime() + timedelta(hours=22)
    rt.drive_stored()
    assert rt.portfolio.qty(key) == 0 and not [o for o in rt.engine.orders.values() if o.is_open and o.instrument_id == AAA]


def test_chart_endpoints_over_http(client):
    from app.database.settings_store import UserSettings
    from tests.integration.test_universe import vendor_csv

    admin, viewer = login(client), login(client, VIEWER)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000, base_currency="USD").model_dump())
    client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": [{"name": "MSFT.csv", "csv": vendor_csv(1)}]})
    assert "1M" in client.get("/api/charts/config", headers=viewer).json()["supported_resolutions"]
    assert client.get("/api/charts/config").status_code == 401
    assert client.get("/api/charts/search?q=MSFT", headers=viewer).json()[0]["symbol"] == "STOCK:MSFT"
    info = client.get("/api/charts/symbols?symbol=STOCK:MSFT", headers=viewer).json()
    assert info["supported_resolutions"] == ["1D", "1W", "1M"]
    assert client.get("/api/charts/symbols?symbol=NOPE", headers=viewer).status_code == 404
    h = client.get("/api/charts/history?symbol=STOCK:MSFT&resolution=1D&countback=30", headers=viewer).json()
    assert h["s"] == "ok" and len(h["t"]) == 30 == len(h["v"])
    assert client.get("/api/charts/history?symbol=STOCK:MSFT&resolution=5&countback=30", headers=viewer).status_code == 400
    st = client.get("/api/charts/studies?symbol=STOCK:MSFT&resolution=1W&bars=100", headers=viewer).json()
    assert len(st["t"]) == 100 and "sma_50" in st["overlays"]
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    order = {"symbol": "STOCK:MSFT", "broker_id": "paper-main", "action": "buy", "qty": 5, "stop_pct": 0.1}
    assert client.post("/api/charts/orders", headers=viewer, json=order).status_code == 403
    # The imported history ends years ago: an order is refused rather than priced on a stale close.
    r = client.post("/api/charts/orders", headers=admin, json=order)
    assert r.status_code == 400 and "No recent stored price" in r.json()["detail"]
    assert client.post("/api/charts/orders", headers=admin, json={**order, "broker_id": "nope"}).status_code == 400
    pos = client.get("/api/charts/position?symbol=STOCK:MSFT", headers=viewer).json()
    assert pos == {"instrument_id": "STOCK:MSFT", "positions": [], "open_orders": []}

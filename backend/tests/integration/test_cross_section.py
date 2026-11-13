"""Phase 5 of the global module: cross-sectional strategies, portfolio weights, rebalancing on paper accounts."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.brokers.ccxt_adapter import CcxtAdapter
from app.core.config import Settings
from app.database.base import Database
from app.database.settings_store import UserSettings, save_settings
from app.domain.models import OrderStatus, OrderType
from app.execution.runtime import TradingRuntime
from app.global_markets import cross_section as cs
from app.global_markets import rebalance
from app.global_markets.metrics import compute_metrics
from app.global_markets.rankings import MODELS
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset
from tests.execution.fakes import FakeCcxt

DATES = pd.bdate_range("2022-01-03", periods=420, tz="UTC", name="ts")


def series(growth, start=100.0, n=420, noise=0.0, seed=0, volume=1e6):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + growth + noise * rng.standard_normal(n))
    open_ = np.concatenate([[start], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001, "low": np.minimum(open_, close) * 0.999, "close": close,
                         "volume": np.full(n, volume)}, index=DATES[:n])


def panel(frames: dict[str, pd.DataFrame], meta=None) -> cs.Panel:
    ids = sorted(frames)
    dates = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    arr = {f: np.column_stack([frames[i][f].reindex(dates).to_numpy() for i in ids]) for f in ("open", "high", "low", "close", "volume")}
    default = {"symbol": "", "name": "", "market": "US", "country": "US", "currency": "USD", "asset_type": "common_stock", "sector": None}
    return cs.Panel(dates, ids, meta={i: {**default, "symbol": i, **(meta or {}).get(i, {})} for i in ids}, **arr)


def noisy(n_instruments=12):
    return {f"S{k:02d}": series(0.0002 * (k - 5), 50 + k, noise=0.012, seed=k) for k in range(n_instruments)}


def test_historical_factors_equal_the_scanner_metrics():
    frames = noisy(4)
    p = panel(frames)
    for model, spec in MODELS.items():
        sel = cs.Selector(p, cs.StrategySpec(model=model))
        for j, iid in enumerate(p.ids):
            m = compute_metrics(frames[iid])
            for factor, _, _ in spec["factors"]:
                assert sel.values[factor][-1, j] == pytest.approx(m[factor], rel=1e-9), (model, factor)
            assert sel.adv[-1, j] == pytest.approx(m["dollar_volume_20"])


def test_selection_and_backtest_never_use_later_data():
    frames = noisy()
    full = panel(frames)
    cut = 380
    tampered = panel({k: f.assign(**{c: np.where(np.arange(len(f)) > cut, f[c] * 3, f[c]) for c in ("open", "high", "low", "close")}) for k, f in frames.items()})
    for model in MODELS:
        spec = cs.StrategySpec(model=model, top_n=4, weighting="min_variance", rebalance="weekly", min_dollar_volume=0)
        assert cs.Selector(full, spec).select(cut) == cs.Selector(tampered, spec).select(cut)
    spec = cs.StrategySpec(model="multi_factor", top_n=4, rebalance="weekly", min_dollar_volume=0, weighting="inverse_volatility")
    a, b = cs.backtest(full, spec), cs.backtest(tampered, spec)
    untouched = [r["ts"] for r in a["equity_curve"] if r["ts"] <= full.dates[cut].date().isoformat()]
    assert len(untouched) > 20
    for ra, rb in zip(a["equity_curve"][: len(untouched)], b["equity_curve"][: len(untouched)]):
        assert ra == rb
    assert a["metrics"]["final_equity"] != b["metrics"]["final_equity"]


def test_backtest_matches_a_hand_calculation():
    frames = {"UP": series(0.002), "FLAT": series(0.0), "DOWN": series(-0.002)}
    p = panel(frames)
    spec = cs.StrategySpec(model="momentum", top_n=1, max_weight=1.0, min_dollar_volume=0, cost_bps=10, stop_pct=0.5)
    out = cs.backtest(p, spec)
    flags = cs.rebalance_days(p.dates, "monthly")
    first = next(i for i in range(cs.WARMUP, len(p.dates)) if flags[i])
    fill = frames["UP"]["open"].iloc[first + 1]            # decided at the close of `first`, bought at the next open
    equity_then = 100_000.0                                # still all cash on the decision day
    shares = np.floor(min(equity_then, 100_000 / 1.001) / fill)
    cash = 100_000 - shares * fill * 1.001
    assert out["metrics"]["final_equity"] == pytest.approx(cash + shares * frames["UP"]["close"].iloc[-1])
    assert out["activity"]["round_trips"] == 0 and out["activity"]["stops_hit"] == 0 and out["activity"]["costs_paid"] == pytest.approx(shares * fill * 0.001)
    assert out["last_selection"]["rows"] == [{"instrument_id": "UP", "symbol": "UP", "market": "US", "weight": 1.0}]
    assert out["universe"]["test_start"] == p.dates[cs.WARMUP].date().isoformat() and len(out["sub_periods"]) == 2
    assert any("Survivorship" in c for c in out["caveats"]) and "do not guarantee" in out["disclaimer"]
    assert out["benchmark"]["total_return"] == pytest.approx(out["benchmark"]["total_return"]) and "no costs" in out["benchmark"]["label"]
    reversal = cs.backtest(p, cs.StrategySpec(model="mean_reversion", top_n=1, max_weight=1.0, min_dollar_volume=0))
    assert reversal["last_selection"]["rows"][0]["instrument_id"] == "DOWN"
    with pytest.raises(ValueError, match="at least"):
        cs.backtest(panel({k: f.iloc[:200] for k, f in frames.items()}), spec)
    with pytest.raises(ValueError, match="Unknown model"):
        cs.backtest(p, cs.StrategySpec(model="tea_leaves"))


def test_protective_stop_fills_at_the_stop_or_at_a_gap_open():
    crash = series(0.002)
    first = next(i for i in range(cs.WARMUP, 420) if cs.rebalance_days(DATES, "monthly")[i])
    entry = crash["open"].iloc[first + 1]
    k = first + 5
    crash.iloc[k:, crash.columns.get_indexer(["open", "high", "low", "close"])] = entry * 0.6   # gaps 40% below the entry and stays there
    frames = {"UP": crash, "FLAT": series(0.0), "DOWN": series(-0.002)}
    out = cs.backtest(panel(frames), cs.StrategySpec(model="momentum", top_n=1, max_weight=1.0, min_dollar_volume=0, cost_bps=0, stop_pct=0.2, rebalance="monthly"))
    assert out["activity"]["stops_hit"] >= 1
    shares = np.floor(100_000 / entry)
    # Sold at the gap open (60% of entry), not at the 80% stop price that never traded.
    after_stop = 100_000 - shares * entry + shares * entry * 0.6
    curve = {r["ts"]: r["equity"] for r in out["equity_curve"]}
    assert curve[DATES[k].date().isoformat()] == pytest.approx(after_stop)  # every day is in the curve for a history this short


def test_weights_caps_and_liquidity_limits():
    w = cs.cap_weights(np.array([0.5, 0.3, 0.2]), 0.4, [])
    assert list(w) == pytest.approx([0.4, 0.3, 0.2])  # the excess stays in cash, nothing is scaled up
    labels = np.array(["US", "US", "IT"], dtype=object)
    w = cs.cap_weights(np.array([0.4, 0.4, 0.2]), 1.0, [(labels, 0.5)])
    assert list(w) == pytest.approx([0.25, 0.25, 0.2])
    mv = cs.min_variance(np.diag([0.04, 0.01, 0.01]))
    assert list(mv) == pytest.approx([1 / 9, 4 / 9, 4 / 9])  # inverse-variance weights for uncorrelated assets
    hedge = cs.min_variance(np.array([[0.04, 0.05], [0.05, 0.09]]))
    assert (hedge >= 0).all() and hedge.sum() == pytest.approx(1) and hedge[1] == 0  # long-only: the unconstrained optimum would short the second asset

    frames = noisy()
    meta = {f"S{k:02d}": {"country": "US" if k < 9 else "IT", "sector": "Tech" if k % 2 else None} for k in range(12)}
    p = panel(frames, meta)
    base = dict(model="momentum", top_n=6, min_dollar_volume=0)
    last = len(p.dates) - 1
    eq = cs.Selector(p, cs.StrategySpec(**base, max_weight=1.0)).select(last)["weights"]
    assert len(eq) == 6 and sum(eq.values()) == pytest.approx(1.0)
    capped = cs.Selector(p, cs.StrategySpec(**base, max_weight=0.1)).select(last)["weights"]
    assert all(v == pytest.approx(0.1) for v in capped.values()) and sum(capped.values()) == pytest.approx(0.6)
    half = cs.Selector(p, cs.StrategySpec(**base, max_weight=1.0, invest=0.5)).select(last)["weights"]
    assert sum(half.values()) == pytest.approx(0.5)
    country = cs.Selector(p, cs.StrategySpec(**base, max_weight=1.0, max_country_weight=0.3)).select(last)["weights"]
    by_country = pd.Series(country).groupby(lambda c: p.meta[p.ids[c]]["country"]).sum()
    assert (by_country <= 0.3 + 1e-9).all()
    iv = cs.Selector(p, cs.StrategySpec(**base, max_weight=1.0, weighting="inverse_volatility"))
    w = iv.select(last)["weights"]
    vols = {c: iv.vol60[last][c] for c in w}
    assert max(w, key=w.get) == min(vols, key=vols.get) and sum(w.values()) == pytest.approx(1.0)
    calm = cs.Selector(p, cs.StrategySpec(**base, max_weight=1.0, target_volatility=0.02)).select(last)["weights"]
    assert sum(calm.values()) < 0.5  # a 2% volatility target leaves most of the portfolio in cash
    rot = cs.Selector(p, cs.StrategySpec(**base, rotation="country", top_groups=1)).select(last)
    assert len({p.meta[p.ids[c]]["country"] for c in rot["weights"]}) == 1 and "Leading country" in rot["note"]
    sector = cs.Selector(p, cs.StrategySpec(**base, rotation="sector", top_groups=1)).select(last)
    assert all(p.meta[p.ids[c]]["sector"] == "Tech" for c in sector["weights"])  # instruments without a sector are never guessed into one
    nothing = cs.Selector(panel(frames), cs.StrategySpec(**base, rotation="sector")).select(last)
    assert nothing["weights"] == {} and "No sector information" in nothing["note"]

    thin = cs.backtest(panel({k: f.assign(volume=10.0) for k, f in frames.items()}), cs.StrategySpec(**base, max_participation=0.05))
    assert thin["activity"]["buys_reduced_by_liquidity_limit"] > 0 and thin["metrics"]["final_equity"] == pytest.approx(100_000, rel=0.01)
    assert cs.backtest(p, cs.StrategySpec(**{**base, "min_dollar_volume": 1e12}))["metrics"]["final_equity"] == 100_000  # nothing is liquid enough


def test_rebalance_days_are_the_first_session_of_each_period():
    days = pd.DatetimeIndex(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05", "2024-03-04"], tz="UTC")
    assert list(cs.rebalance_days(days, "monthly")) == [False, False, True, False, False, True]
    assert list(cs.rebalance_days(days, "weekly")) == [False, False, False, False, True, True]


# ---------------------------------------------------------------- on a paper account, through the risk engine
class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    with db.session() as s:
        save_settings(s, UserSettings(initial_paper_capital=100_000, base_currency="USD"))
    clock = Clock(DATES[-1].to_pydatetime() + timedelta(hours=22))
    runtime = TradingRuntime(db, Settings(_env_file=None), clock=clock)
    runtime.start()
    runtime.create_paper_account("paper-main", "Main", "admin")
    runtime.add_instruments(TICKERS, "US", "stock", "admin")
    for k, t in enumerate(TICKERS):
        runtime._store_download(runtime.registry.get(f"STOCK:{t}"), Dataset(series(0.0004 * (k - 3), 20 + 5 * k, noise=0.004, seed=k), DataKind.HISTORICAL, "test"))
    runtime.test_clock = clock
    return runtime


def next_bar(rt, day, moves: dict[str, float] | None = None, default=0.0):
    """Store one more daily bar per instrument: flat open, then a move to the close."""
    ts = pd.DatetimeIndex([day], name="ts")
    for t in TICKERS:
        inst = rt.registry.get(f"STOCK:{t}")
        with rt.db.session() as s:
            last = rt.store.load(s, inst.id, "1d").bars.iloc[-1]
        close = last.close * (1 + (moves or {}).get(t, default))
        bar = pd.DataFrame({"open": last.close, "high": max(last.close, close), "low": min(last.close, close), "close": close, "volume": 1e6}, index=ts)
        with rt.db.session() as s:
            rt.store.save(s, inst.id, "1d", Dataset(bar, DataKind.HISTORICAL, "test"))


SPEC = cs.StrategySpec(model="momentum", top_n=3, min_dollar_volume=0, stop_pct=0.2)


def test_rebalance_is_sized_and_gated_by_the_risk_engine_and_fills_on_the_next_stored_bar(rt):
    with rt.db.session() as s:
        prop = rebalance.propose(rt, s, SPEC, "paper-main", "admin")
    buys = prop["orders"]
    assert [o["symbol"] for o in buys] == ["HHH", "GGG", "FFF"] and all(o["side"] == "BUY" and o["approved"] for o in buys)
    for o in buys:
        # Target weight is 20%, but the 1% risk-per-trade limit with a 20% stop allows 5% of equity per position.
        assert o["qty"] == np.floor(5_000 / o["price"]) and "size reduced by risk per trade at the stop" in o["reasons"]
        assert o["stop_price"] == pytest.approx(o["price"] * 0.8, abs=0.01)
    assert not rt.engine.orders  # a proposal places nothing

    with rt.db.session() as s:
        done = rebalance.execute(rt, s, prop["id"], "admin")
    assert [r["status"] for r in done["results"]] == ["submitted"] * 3 and done["status"] == "executed"
    with rt.db.session() as s, pytest.raises(rebalance.RebalanceError, match="already executed"):
        rebalance.execute(rt, s, prop["id"], "admin")
    assert rt.drive_stored()["bars"] == 0 and not rt.portfolio.slices  # no bar after the order yet: nothing fills on old prices

    day1 = DATES[-1] + pd.offsets.BDay(1)
    next_bar(rt, day1)
    rt.test_clock.now = day1.to_pydatetime() + timedelta(hours=22)
    assert rt.drive_stored()["bars"] == 3
    held = rebalance.holdings(rt)
    assert {h["symbol"] for h in held["positions"]} == {"HHH", "GGG", "FFF"}
    for h in held["positions"]:
        want = next(o for o in buys if o["symbol"] == h["symbol"])
        assert h["qty"] == want["qty"] and h["strategy"] == "global:momentum"
        assert h["stop_price"] == pytest.approx(h["avg_price"] * 0.8, rel=1e-3)  # protective stop placed from the actual fill
    stops = [o for o in rt.engine.orders.values() if o.is_open and o.type is OrderType.STOP]
    assert len(stops) == 3 and all(o.reduce_only for o in stops)
    assert rt.drive_stored()["bars"] == 0  # the same bar is never applied twice

    day2 = day1 + pd.offsets.BDay(1)
    next_bar(rt, day2, {"HHH": -0.30})  # falls through its stop during the day
    rt.test_clock.now = day2.to_pydatetime() + timedelta(hours=22)
    rt.drive_stored()
    assert {h["symbol"] for h in rebalance.holdings(rt)["positions"]} == {"GGG", "FFF"}
    trade = rt.portfolio.trades[-1]
    assert trade.instrument_id == "STOCK:HHH" and trade.exit_reason == "stop_loss" and trade.pnl < 0

    # Survives a restart: positions, stops and the last bar applied are all restored.
    again = TradingRuntime(rt.db, Settings(_env_file=None), clock=rt.test_clock)
    again.start()
    assert {h["symbol"] for h in rebalance.holdings(again)["positions"]} == {"GGG", "FFF"} and again.drive_stored()["bars"] == 0

    # Next rebalance: momentum leadership changes, the leaver is sold and the newcomer bought.
    with rt.db.session() as s:
        second = rebalance.propose(rt, s, cs.StrategySpec(model="mean_reversion", top_n=3, min_dollar_volume=0), "paper-main", "admin")
    assert second["strategy"] == "global:mean_reversion" and all(o["side"] == "BUY" for o in second["orders"])  # another strategy has its own book
    with rt.db.session() as s:
        third = rebalance.propose(rt, s, cs.StrategySpec(model="momentum", top_n=1, min_dollar_volume=0), "paper-main", "admin")
    sells = [o for o in third["orders"] if o["side"] == "SELL"]
    assert sells and all(o["approved"] for o in sells) and set(third["kept"]) | {o["instrument_id"] for o in sells} == {"STOCK:GGG", "STOCK:FFF"}
    with rt.db.session() as s:
        rebalance.execute(rt, s, third["id"], "admin")
    day3 = day2 + pd.offsets.BDay(1)
    next_bar(rt, day3)
    rt.test_clock.now = day3.to_pydatetime() + timedelta(hours=22)
    rt.drive_stored()
    left = {h["symbol"] for h in rebalance.holdings(rt)["positions"]}
    assert left == {o["symbol"] for o in third["orders"] if o["side"] == "BUY"} | {rt.registry.get(i).symbol for i in third["kept"]}
    assert not [o for o in rt.engine.orders.values() if o.is_open and o.type is OrderType.STOP and o.instrument_id in {x["instrument_id"] for x in sells}]


def test_risk_limits_and_safety_rules_apply_to_rebalances(rt):
    rt.kill_switch(True, "test", "admin")
    with rt.db.session() as s:
        blocked = rebalance.propose(rt, s, SPEC, "paper-main", "admin")
    assert all(not o["approved"] and any("kill switch" in r for r in o["reasons"]) for o in blocked["orders"])
    with rt.db.session() as s:
        res = rebalance.execute(rt, s, blocked["id"], "admin")
    assert {r["status"] for r in res["results"]} == {"not sent"} and not rt.engine.orders
    rt.kill_switch(False, "", "admin")

    # A proposal that was fine when prepared is judged again at execution.
    with rt.db.session() as s:
        ok = rebalance.propose(rt, s, SPEC, "paper-main", "admin")
    rt.kill_switch(True, "changed my mind", "admin")
    with rt.db.session() as s:
        res = rebalance.execute(rt, s, ok["id"], "admin")
    assert {r["status"] for r in res["results"]} == {OrderStatus.REJECTED.value}
    rt.kill_switch(False, "", "admin")

    with rt.db.session() as s:
        save_settings(s, rt.user_settings.model_copy(update={"risk": rt.user_settings.risk.model_copy(update={"max_concurrent_positions": 2})}))
        rt.reload_settings(s)
    with rt.db.session() as s:
        limited = rebalance.propose(rt, s, SPEC, "paper-main", "admin")
    assert [o["approved"] for o in limited["orders"]] == [True, True, False] and "concurrent positions" in " ".join(limited["orders"][2]["reasons"])

    with rt.db.session() as s:
        stale = rebalance.propose(rt, s, SPEC, "paper-main", "admin")
    rt.test_clock.now += timedelta(hours=25)
    with rt.db.session() as s, pytest.raises(rebalance.RebalanceError, match="24 hours"):
        rebalance.execute(rt, s, stale["id"], "admin")

    with rt.db.session() as s, pytest.raises(rebalance.RebalanceError, match="Unknown account"):
        rebalance.propose(rt, s, SPEC, "nope", "admin")
    rt.brokers.register(CcxtAdapter("live-x", rt.registry, FakeCcxt()))
    with rt.db.session() as s, pytest.raises(rebalance.RebalanceError, match="paper accounts only"):
        rebalance.propose(rt, s, SPEC, "live-x", "admin")


# ---------------------------------------------------------------- through the API
def test_analysis_rankings_backtest_and_rebalance_over_http(client):
    from app.database.settings_store import UserSettings as US
    from tests.conftest import VIEWER, login
    from tests.integration.test_scanner import wait_metrics
    from tests.integration.test_universe import vendor_csv

    admin, viewer = login(client), login(client, VIEWER)
    client.put("/api/settings", headers=admin, json=US(initial_paper_capital=100_000, base_currency="USD").model_dump())
    files = [{"name": f"{t}.csv", "csv": vendor_csv(i)} for i, t in enumerate(("MSFT", "NVDA", "AMZN", "KO", "XOM", "JPM"), start=1)]
    client.post("/api/market/import-csv-bulk", headers=admin, json={"market": "US", "timeframe": "1d", "files": files})
    assert client.get("/api/global/overview", headers=admin).json()["instruments"] == 0
    client.post("/api/scanner/metrics", headers=admin, json={})
    wait_metrics(client)

    ov = client.get("/api/global/overview", headers=viewer).json()
    assert ov["equities"] == 6 and ov["breadth"]["advancers"] + ov["breadth"]["decliners"] + ov["breadth"]["unchanged"] == 6
    assert client.get("/api/global/overview?group_by=zodiac", headers=admin).status_code == 400
    models = {m["name"] for m in client.get("/api/global/ranking-models", headers=viewer).json()}
    assert {"momentum", "multi_factor", "breakout"} <= models
    q = "&min_dollar_volume=0"
    rk = client.get("/api/global/rankings?model=momentum" + q, headers=viewer).json()
    assert rk["ranked"] == 6 and rk["validation"]["status"] == "not tested" and [r["rank"] for r in rk["rows"]] == [1, 2, 3, 4, 5, 6]
    assert client.get("/api/global/rankings?model=nope", headers=admin).status_code == 400
    assert client.post("/api/global/report", headers=viewer, json={}).status_code == 403
    rep = client.post("/api/global/report", headers=admin, json={"model": "momentum"}).json()
    assert "## Breadth" in rep["markdown"]

    spec = {"model": "momentum", "top_n": 2, "min_dollar_volume": 0}
    assert client.get("/api/global/strategies/options", headers=viewer).json()["defaults"]["stop_pct"] == 0.2
    assert client.post("/api/global/strategies/backtest", headers=viewer, json=spec).status_code == 403
    assert client.post("/api/global/strategies/backtest", headers=admin, json={**spec, "top_n": 0}).status_code == 422
    bt = client.post("/api/global/strategies/backtest", headers=admin, json=spec)
    assert bt.status_code == 200, bt.text
    assert bt.json()["universe"]["instruments"] == 6 and bt.json()["caveats"]
    tests = client.get("/api/global/strategies/tests", headers=viewer).json()
    assert tests[0]["model"] == "momentum" and tests[0]["instruments"] == 6
    assert client.get("/api/global/rankings?model=momentum" + q, headers=viewer).json()["validation"]["status"] == "tested"

    target = client.post("/api/global/portfolio/target", headers=viewer, json=spec).json()
    assert len(target["rows"]) == 2 and target["invested"] == pytest.approx(0.4)
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    assert client.post("/api/global/portfolio/proposals", headers=viewer, json={"broker_id": "paper-main", "definition": spec}).status_code == 403
    prop = client.post("/api/global/portfolio/proposals", headers=admin, json={"broker_id": "paper-main", "definition": spec})
    assert prop.status_code == 201, prop.text
    assert len(prop.json()["orders"]) == 2
    done = client.post(f"/api/global/portfolio/proposals/{prop.json()['id']}/execute", headers=admin).json()
    assert done["status"] == "executed" and "next" in done
    assert client.post(f"/api/global/portfolio/proposals/{prop.json()['id']}/execute", headers=admin).status_code == 400
    assert client.get("/api/global/portfolio/proposals", headers=viewer).json()[0]["status"] == "executed"
    held = client.get("/api/global/portfolio/holdings", headers=viewer).json()
    assert held["positions"] == [] and len(held["open_orders"]) == sum(r["status"] == "submitted" for r in done["results"])
    assert client.post("/api/global/portfolio/settle", headers=admin).json()["bars"] == 0

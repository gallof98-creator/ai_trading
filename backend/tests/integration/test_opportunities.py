"""Quant AI opportunity engine: validation gate, regime fit, proposals with a verifiable explanation, execution on paper."""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.domain.models import OrderStatus
from app.execution.runtime import TradingRuntime
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset
from app.quant_ai import opportunities as opp
from app.quant_ai.regime import detect_regimes
from app.strategies.registry import create_strategy, list_strategies

CYC = "CRYPTO:CYC/USD"


def cyclical(n=1600, period=60, amp=0.25, noise=0.004, seed=1):
    """Test data: regular swings that a trend rule can follow and a mean-reversion rule cannot."""
    rng = np.random.default_rng(seed)
    logp = np.log(100) + amp * np.sin(2 * np.pi * np.arange(n) / period) + np.cumsum(rng.normal(0, noise, n))
    c = np.exp(logp)
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC", name="ts")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998, "close": c, "volume": 1e6}, index=idx)


def cut_at_entry(bars, strategy="ema_crossover"):
    """End the history on a bar where the strategy opens a long position in a regime that suits trend following."""
    frame = create_strategy(strategy, {}).generate(bars)
    regimes = detect_regimes(bars)["primary"]
    fits = opp.MODULES["trend_following"]["fits"]
    last = max(i for i in range(1500, len(bars)) if frame["action"].iloc[i] == "BUY" and regimes.iloc[i] in fits)
    return bars.iloc[: last + 1]


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    bars = cut_at_entry(cyclical())
    now = [bars.index[-1].to_pydatetime() + timedelta(days=1, hours=1)]
    runtime = TradingRuntime(db, Settings(_env_file=None), clock=lambda: now[0])
    runtime.start()  # initial profile: 1,000 EUR, semi-automatic, 2%
    runtime.create_paper_account("paper-main", "Main", "admin")
    runtime.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", now[0] - timedelta(hours=2))
    runtime.add_instruments(["CYC/USD", "SIM/USD"], "", "crypto", "admin")
    with db.session() as s:
        runtime.store.save(s, CYC, "1d", Dataset(bars, DataKind.HISTORICAL, "test"))
        runtime.store.save(s, "CRYPTO:SIM/USD", "1d", Dataset(bars, DataKind.SIMULATED, "simulated"))
    runtime.now, runtime.test_bars = now, bars
    return runtime


def test_the_nine_modules_cover_every_strategy_and_state_what_they_are():
    d = opp.describe_modules()
    names = [m["name"] for m in d["modules"]]
    assert names == ["trend_following", "momentum", "mean_reversion", "breakout", "volatility", "scalping", "intraday", "swing", "position"]
    covered = {s["name"] for m in d["modules"] if m["kind"] == "signal" for s in m["strategies"]}
    assert covered == {s["name"] for s in list_strategies()}  # every strategy in the library belongs to a module
    assert all(m["exits"]["stop_atr"] > 0 and m["exits"]["take_profit_atr"] > m["exits"]["stop_atr"] for m in d["modules"] if m["kind"] == "horizon")
    assert opp.horizon_of("1m") == "scalping" and opp.horizon_of("1h") == "intraday" and opp.horizon_of("4h") == "swing" and opp.horizon_of("1w") == "position"
    assert d["criteria"]["min_profitable_folds"] == 3 and "horizons" in d["note"]


def test_validation_needs_several_unseen_periods_and_is_recorded(rt):
    with rt.db.session() as s:
        good = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")
        bad = opp.validate(s, rt, "rsi_reversion", CYC, "1d", "admin")
        fake = opp.validate(s, rt, "ema_crossover", "CRYPTO:SIM/USD", "1d", "admin")
    assert good["status"] == "validated" and all(c["passed"] for c in good["checks"]) and len(good["checks"]) == 7
    assert good["profitable_folds"] >= 3 and good["out_of_sample"]["number_of_trades"] >= 20 and good["family"] == "trend_following" and good["horizon"] == "swing"
    assert good["params"]["take_profit_atr"] == 4.0 and good["params"]["stop_atr"] in (1.5, 2.0, 2.5) and "does not mean it will be profitable" in good["caveat"]
    assert bad["status"] == "rejected" and any(not c["passed"] and "profitable in at least 3 of 4" in c["name"] for c in bad["checks"])
    # The same good numbers on simulated prices are still not a validation.
    assert fake["status"] == "rejected" and [c["name"] for c in fake["checks"] if not c["passed"]] == ["market data, not simulated"]
    with rt.db.session() as s:
        listed = {v["strategy"] + "|" + v["instrument_id"]: v["status"] for v in opp.validations(s, rt.clock())}
        assert listed == {f"ema_crossover|{CYC}": "validated", f"rsi_reversion|{CYC}": "rejected", "ema_crossover|CRYPTO:SIM/USD": "rejected"}
        later = rt.clock() + timedelta(days=31)
        assert next(v for v in opp.validations(s, later) if v["strategy"] == "ema_crossover" and v["instrument_id"] == CYC)["status"] == "expired"
    with rt.db.session() as s, pytest.raises(Exception):
        opp.validate(s, rt, "ema_crossover", CYC, "1h", "admin")  # no stored bars at that timeframe


def test_scan_proposes_only_validated_strategies_and_explains_itself(rt):
    with rt.db.session() as s:
        assert opp.scan(s, rt, "paper-main", "admin") == {"scanned_at": rt.clock().isoformat(), "broker_id": "paper-main", "proposed": [], "skipped": [], "validated": 0}
        opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")
        opp.validate(s, rt, "rsi_reversion", CYC, "1d", "admin")
    with rt.db.session() as s:
        out = opp.scan(s, rt, "paper-main", "admin")
    assert [(p["strategy"], p["instrument_id"]) for p in out["proposed"]] == [("ema_crossover", CYC)]
    assert {"strategy": "rsi_reversion", "instrument_id": CYC, "timeframe": "1d", "reason": "did not pass validation"} in out["skipped"]
    assert not [o for o in rt.engine.orders.values() if o.status is OrderStatus.SUBMITTED]  # a scan sends nothing
    [p] = rt.proposals()
    bars = rt.test_bars
    assert p["side"] == "BUY" and p["strategy"] == "ema_crossover" and p["timeframe"] == "1d" and p["entry_price"] == pytest.approx(bars["close"].iloc[-1])
    assert p["risk_base"] <= 20.0 and p["stop_price"] < p["entry_price"] < p["take_profit_price"]
    r = p["rationale"]
    assert r["opportunity"]["family"] == "trend_following" and r["opportunity"]["horizon"] == "swing" and r["opportunity"]["regime"] in r["opportunity"]["regime_fits"]
    assert f"{bars.index[-1]:%Y-%m-%d %H:%M}" in r["opportunity"]["why"] and f"{bars['close'].iloc[-1]:g}" in r["opportunity"]["why"]  # checkable against the stored bar
    assert r["validation"]["profitable_folds"] >= 3 and r["validation"]["number_of_trades"] >= 20 and r["data"]["kind"] == "historical"
    assert r["confidence"] is None  # no probability of success is claimed

    with rt.db.session() as s:
        again = opp.scan(s, rt, "paper-main", "admin")
    assert again["proposed"] == [] and len(rt.proposals()) == 1  # the same bar is never proposed twice
    assert any("already proposed" in x["reason"] for x in again["skipped"])
    assert len(rt.deployments) == 1  # the validated strategy is now deployed, so its exits are managed

    # Approval: sent to the paper account, filled on the next downloaded daily bar, protected by a stop.
    order = rt.resolve_approval(p["id"], True, "trader@example.com")
    assert order.status is OrderStatus.SUBMITTED
    last = bars["close"].iloc[-1]
    key = ("paper-main", CYC, "ema_crossover")

    def add_bar(day):
        with rt.db.session() as s:
            rt.store.save(s, CYC, "1d", Dataset(pd.DataFrame({"open": last, "high": last * 1.01, "low": last * 0.995, "close": last * 1.005, "volume": 1e6},
                                                             index=pd.DatetimeIndex([day], name="ts")), DataKind.HISTORICAL, "test"))
        rt.now[0] = day.to_pydatetime() + timedelta(days=1, hours=1)
        return rt.drive_stored()["bars"]

    # The bar of the approval day had already opened when the order was sent: it must not fill there.
    assert add_bar(bars.index[-1] + pd.Timedelta(days=1)) == 1 and rt.portfolio.qty(key) == 0
    assert add_bar(bars.index[-1] + pd.Timedelta(days=2)) == 1
    assert rt.portfolio.qty(key) == pytest.approx(p["qty"])
    assert len([o for o in rt.engine.orders.values() if o.is_open and o.type.value == "stop"]) == 1


def test_scan_refuses_stale_prices_untradable_instruments_and_a_halted_engine(rt):
    with rt.db.session() as s:
        opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")
    rt.now[0] += timedelta(days=6)
    with rt.db.session() as s:
        stale = opp.scan(s, rt, "paper-main", "admin")
    assert stale["proposed"] == [] and "out of date" in stale["skipped"][0]["reason"]
    rt.now[0] -= timedelta(days=6)
    with rt.db.session() as s:
        assert "liquidity floor" in opp.scan(s, rt, "paper-main", "admin", min_traded_value=1e12)["skipped"][0]["reason"]
    with rt.db.session() as s, pytest.raises(ValueError, match="Unknown account"):
        opp.scan(s, rt, "nope", "admin")
    rt.kill_switch(True, "emergency stop", "admin")
    with rt.db.session() as s, pytest.raises(ValueError, match="halted"):
        opp.scan(s, rt, "paper-main", "admin")
    assert rt.proposals() == []
    with rt.db.session() as s:
        u = opp.universes(s, rt)
    assert u["analysable"]["count"] == 1 and u["tradable"]["paper-main"]["with_prices"] == 1 and u["catalogued"]["count"] > u["analysable"]["count"]


def test_quant_endpoints_over_http(client):
    from tests.conftest import VIEWER, login

    admin, viewer = login(client), login(client, VIEWER)
    dash = client.get("/api/quant/dashboard", headers=viewer).json()
    cfg = dash["configuration"]
    assert (cfg["virtual_capital"], cfg["base_currency"], cfg["trading_mode"], cfg["risk_per_trade"], cfg["risk_amount"]) == (1000, "EUR", "semi_auto", 0.02, 20)
    assert cfg["live_trading_authorized"] is False and dash["proposals"] == [] and "catalogued" in dash["universes"]
    assert len(client.get("/api/quant/modules", headers=viewer).json()["modules"]) == 9
    assert client.post("/api/quant/scan", headers=viewer, json={"broker_id": "paper-main"}).status_code == 403
    assert client.post("/api/quant/scan", headers=admin, json={"broker_id": "paper-main"}).status_code == 400  # no such account yet
    assert client.post("/api/quant/validations", headers=admin, json={"strategy": "ema_crossover", "instrument_id": "STOCK:AAPL", "timeframe": "1d"}).status_code == 400
    assert client.get("/api/quant/validations", headers=viewer).json() == []
    assert client.post("/api/quant/pause", headers=viewer, json={}).status_code == 403
    assert client.post("/api/quant/pause", headers=admin, json={"reason": "EMERGENCY STOP"}).status_code == 204
    assert client.get("/api/quant/dashboard", headers=viewer).json()["overview"]["risk"]["halted"] is True
    assert client.post("/api/quant/resume", headers=admin).status_code == 204
    assert client.get("/api/proposals", headers=viewer).json() == []

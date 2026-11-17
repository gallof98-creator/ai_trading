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
        good = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin", allow_short=True)
        bad = opp.validate(s, rt, "rsi_reversion", CYC, "1d", "admin", allow_short=True)
        fake = opp.validate(s, rt, "ema_crossover", "CRYPTO:SIM/USD", "1d", "admin", allow_short=True)
    assert good["status"] == "validated" and all(c["passed"] for c in good["checks"]) and len(good["checks"]) == 11
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
        assert opp.scan(s, rt, "paper-main", "admin") == {"scanned_at": rt.clock().isoformat(), "broker_id": "paper-main", "proposed": [], "sent": [], "skipped": [],
                                                              "funnel": {"setups": 0}, "validated": 0, "candidates": 0, "explorable": 0}
        opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin", allow_short=True)
        opp.validate(s, rt, "rsi_reversion", CYC, "1d", "admin", allow_short=True)
    with rt.db.session() as s:
        out = opp.scan(s, rt, "paper-main", "admin")
    assert [(p["strategy"], p["instrument_id"]) for p in out["proposed"]] == [("ema_crossover", CYC)]
    assert {"strategy": "rsi_reversion", "instrument_id": CYC, "timeframe": "1d", "level": None, "recorded": "rejected", "stage": "validation", "reason": "did not pass validation"} in out["skipped"]
    assert out["funnel"] == {"setups": 2, "validation": 1, "proposed": 1} and out["sent"] == []
    assert not [o for o in rt.engine.orders.values() if o.status is OrderStatus.SUBMITTED]  # a scan sends nothing
    [p] = rt.proposals()
    bars = rt.test_bars
    assert p["side"] == "BUY" and p["strategy"] == "ema_crossover" and p["timeframe"] == "1d" and p["entry_price"] == pytest.approx(bars["close"].iloc[-1])
    assert p["risk_base"] <= 20.0 and p["stop_price"] < p["entry_price"] < p["take_profit_price"]
    assert 11.5 * 3600 < p["seconds_left"] <= 12 * 3600  # a daily signal stays open for half a day, not fifteen minutes
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
        opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin", allow_short=True)
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


def test_candidate_level_is_weaker_evidence_labelled_and_kept_to_paper_accounts(rt):
    """Long-only, the same rule trades half as often: profitable on most unseen periods, but on too few trades to be validated."""
    from app.brokers.ccxt_adapter import CcxtAdapter
    from tests.execution.fakes import FakeCcxt

    with rt.db.session() as s:
        v = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")  # long-only is the default
    assert v["status"] == "candidate" and v["long_only"] is True and v["params"]["allow_short"] is False
    assert all(c["passed"] for c in v["candidate_checks"]) and not all(c["passed"] for c in v["checks"])
    assert [c["name"] for c in v["checks"] if not c["passed"]] == ["at least 20 trades on unseen data"] and "by chance" in v["caveat"]
    with rt.db.session() as s:
        strict = opp.scan(s, rt, "paper-main", "admin")
    assert strict["proposed"] == [] and "candidate only" in strict["skipped"][0]["reason"] and strict["candidates"] == 1 and strict["validated"] == 0
    rt.brokers.register(CcxtAdapter("exchange-x", rt.registry, FakeCcxt()))
    rt.brokers.get("exchange-x").connect()
    rt.brokers.get("exchange-x")._authorization = object()  # as if live trading had been authorized: real money
    rt.registry.map_symbol("exchange-x", CYC, "CYC/USD")
    with rt.db.session() as s:
        real = opp.scan(s, rt, "exchange-x", "admin", include_candidates=True)
    assert real["proposed"] == [] and any("never with real money" in x["reason"] for x in real["skipped"])
    rt.brokers.remove("exchange-x")
    with rt.db.session() as s:
        paper = opp.scan(s, rt, "paper-main", "admin", include_candidates=True)
    assert len(paper["proposed"]) == 1
    [p] = rt.proposals()
    assert p["rationale"]["validation"]["level"] == "candidate" and "by chance" in p["rationale"]["validation"]["caveat"]


def test_a_daily_position_on_an_instrument_with_live_prices_fills_is_protected_and_exits_on_its_strategy(rt):
    """The Autopilot subscribes live prices for validated instruments. Live minutes must then drive fills and stops,
    and the downloaded daily bars must still reach the daily strategy, so that its own exit signal is produced."""
    from tests.integration.test_live import Feeder

    with rt.db.session() as s:
        opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin", allow_short=True)
    feeder = Feeder(rt.live_hub)
    rt.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.symbol == "CYC/USD" else None)]
    with rt.db.session() as s:
        opp.scan(s, rt, "paper-main", "admin")
    [p] = rt.proposals()
    rt.live_subscribe([CYC], "admin")
    rt.now[0] += timedelta(minutes=1)
    feeder.trade("CYC/USD", p["entry_price"], rt.now[0], 50.0)
    rt.resolve_approval(p["id"], True, "trader@example.com")
    key = ("paper-main", CYC, "ema_crossover")
    for k in range(1, 4):   # the next live minutes: the order must fill at the open of the first one after it
        rt.now[0] += timedelta(minutes=1)
        feeder.trade("CYC/USD", p["entry_price"], rt.now[0], 50.0)
    assert rt.portfolio.qty(key) == pytest.approx(p["qty"], rel=1e-3), "a paper order on a live instrument with a daily strategy never filled"
    stop = rt.engine.protections[key].stop_price
    assert stop < p["entry_price"]

    # The strategy's own exit: daily bars that turn the trend down, downloaded as usual.
    bars = rt.test_bars
    last = float(bars["close"].iloc[-1])
    n = 40   # a long decline: enough for the slow average to be crossed (daily bars do not move stops on a live instrument)
    closes = [last * (0.99 ** (k + 1)) for k in range(n)]
    opens = [last] + closes[:-1]
    falling = pd.DataFrame({"open": opens, "high": [max(o, c) * 1.001 for o, c in zip(opens, closes)], "low": [min(o, c) * 0.999 for o, c in zip(opens, closes)],
                            "close": closes, "volume": 1e6}, index=pd.date_range(bars.index[-1] + pd.Timedelta(days=1), periods=n, freq="1D", name="ts"))
    with rt.db.session() as s:
        rt.store.save(s, CYC, "1d", Dataset(falling, DataKind.HISTORICAL, "test"))
    rt.now[0] = falling.index[-1].to_pydatetime() + timedelta(days=1, hours=1)
    for day in falling.index[::4]:   # exchange rates keep being published meanwhile
        rt.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", day.to_pydatetime())
    fed = rt.drive_stored()
    exits = [a for a in rt.engine.approvals.values() if a.order.reduce_only and a.order.instrument_id == CYC]
    closed = [o for o in rt.engine.orders.values() if o.reduce_only and o.instrument_id == CYC and o.reason == "signal"]
    assert exits or closed, "the daily strategy never saw the new daily bars, so its exit signal was never produced"
    assert exits and exits[0].expires_at is None   # semi-automatic: the exit waits for approval, and a closing proposal never lapses

    # And a resting stop on the internal paper account is triggered by live minutes.
    rt2_price = stop * 0.98
    if rt.portfolio.qty(key):
        rt.now[0] += timedelta(minutes=1)
        feeder.trade("CYC/USD", rt2_price, rt.now[0], 50.0)
        rt.now[0] += timedelta(minutes=1)
        feeder.trade("CYC/USD", rt2_price, rt.now[0], 50.0)
        assert rt.portfolio.qty(key) == 0 and rt.portfolio.trades[-1].exit_reason == "stop_loss"
        assert not [a for a in rt.engine.approvals.values() if a.order.instrument_id == CYC and a.order.reduce_only]   # the waiting exit is withdrawn
    rt.live.stop()


def _rejected_but_not_losing(rt):
    """The candidate from the fixture, recorded again as a setup that failed validation without losing on unseen data."""
    with rt.db.session() as s:
        v = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")
        payload = {k: v[k] for k in v if k not in ("id", "status", "tested_at")}
        payload["profitable_folds"] = 2
        rt.now[0] += timedelta(seconds=1)
        opp.record(s, "rejected", payload, rt.clock())
    with rt.db.session() as s:
        [latest] = [x for x in opp.validations(s, rt.clock()) if x["instrument_id"] == CYC]
    assert latest["status"] == "rejected" and opp.explorable(latest) and opp.level_of(latest) == "exploration"
    return latest


def test_exploration_trades_rejected_but_not_losing_setups_on_paper_only_when_switched_on(rt):
    from app.brokers.ccxt_adapter import CcxtAdapter
    from tests.execution.fakes import FakeCcxt

    _rejected_but_not_losing(rt)
    with rt.db.session() as s:
        off = opp.scan(s, rt, "paper-main", "admin", include_candidates=True)
    assert off["proposed"] == [] and any("exploration is off" in x["reason"] for x in off["skipped"]) and off["explorable"] == 1
    rt.brokers.register(CcxtAdapter("exchange-x", rt.registry, FakeCcxt()))
    rt.brokers.get("exchange-x").connect()
    rt.brokers.get("exchange-x")._authorization = object()  # as if live trading had been authorized: real money
    rt.registry.map_symbol("exchange-x", CYC, "CYC/USD")
    with rt.db.session() as s:
        real = opp.scan(s, rt, "exchange-x", "admin", include_exploration=True)
    assert real["proposed"] == [] and real["sent"] == [] and any("never with real money" in x["reason"] for x in real["skipped"])
    rt.brokers.remove("exchange-x")
    with rt.db.session() as s:
        on = opp.scan(s, rt, "paper-main", "admin", include_exploration=True)
    assert [p["level"] for p in on["proposed"]] == ["exploration"]
    [p] = rt.proposals()
    assert p["rationale"]["validation"]["level"] == "exploration" and "FAILED validation" in p["rationale"]["validation"]["caveat"]
    assert rt.evidence_of("paper-main", CYC, "ema_crossover") == "exploration"


def test_weaker_evidence_is_refused_by_the_engine_on_a_real_money_account_even_if_already_deployed(rt):
    """A deployment started for exploration keeps trading later bars without the scan. If its account were real money
    (a broker switched from paper to live), every entry must be refused by the engine itself."""
    from app.domain.models import Order, Side

    order = Order(broker_id="paper-main", instrument_id=CYC, side=Side.BUY, qty=1, strategy="ema_crossover")
    rt.evidence[rt.evidence_key("paper-main", CYC, "ema_crossover")] = "exploration"
    assert rt.entry_block(order) is None
    adapter = rt.brokers.get("paper-main")
    real = type("Caps", (), {"is_simulated": False})()
    original = type(adapter).capabilities
    try:
        type(adapter).capabilities = property(lambda self: real)
        assert "paper accounts only" in rt.entry_block(order)
        assert rt.entry_block(order.model_copy(update={"reduce_only": True})) is not None  # the engine skips the check for closing orders
        rt.evidence[rt.evidence_key("paper-main", CYC, "ema_crossover")] = "validated"
        assert rt.entry_block(order) is None
    finally:
        type(adapter).capabilities = original


def test_a_broker_paper_environment_counts_as_paper_and_follows_the_trading_mode():
    from types import SimpleNamespace

    from app.brokers.base import is_paper_account

    caps = SimpleNamespace(is_simulated=False)
    assert is_paper_account(SimpleNamespace(capabilities=caps, is_live=False))
    assert not is_paper_account(SimpleNamespace(capabilities=caps, is_live=True))
    assert not is_paper_account(SimpleNamespace(capabilities=caps))  # an adapter that does not say counts as real money
    assert is_paper_account(SimpleNamespace(capabilities=SimpleNamespace(is_simulated=True)))


def test_the_validation_chooses_the_take_profit_and_the_proposal_uses_it(rt):
    assert opp.take_profit_grid("15m") == [1.5, 3.0, 4.5, None] and opp.take_profit_grid("5m") == [0.75, 1.5, 2.25, None]
    with rt.db.session() as s:
        spec = opp.prepare(s, rt, "ema_crossover", CYC, "1d")
        v = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin", allow_short=True)
    assert "take_profit_atr" in spec.param_grid and "take_profit_atr" not in spec.fixed_params
    assert v["params"]["take_profit_atr"] in opp.take_profit_grid("1d") and v["criteria_version"] == 3
    with rt.db.session() as s:
        opp.scan(s, rt, "paper-main", "admin")
    [p] = rt.proposals()
    tp = v["params"]["take_profit_atr"]
    assert (p["take_profit_price"] is None) == (tp is None)


def test_the_validation_list_reads_only_each_setups_latest_test_and_can_be_paged(rt):
    from fastapi.testclient import TestClient  # noqa: F401

    with rt.db.session() as s:
        first = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")
        rt.now[0] += timedelta(seconds=1)
        second = opp.validate(s, rt, "ema_crossover", CYC, "1d", "admin")
        opp._CACHE.clear()
        rows = opp.validations(s, rt.clock())
    assert [r["id"] for r in rows] == [second["id"]] and first["id"] != second["id"]

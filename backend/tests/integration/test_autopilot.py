"""Autopilot: universe, prices, validation, live subscriptions and scans without a click; orders still need one."""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.database.settings_store import AutopilotSettings, initial_settings, save_settings
from app.domain.models import AssetClass, OrderStatus
from app.execution.runtime import TradingRuntime
from app.market_data.calendars import get_calendar
from app.market_data.providers import PriceProvider, SymbolNotFound
from app.market_data.quality import DataKind, validate_bars
from app.market_data.sources import Dataset
from app.security.vault import CredentialVault
from tests.integration.test_live import Feeder
from tests.integration.test_opportunities import cut_at_entry


def us_series(seed, n=1700, period=60, amp=0.25, base=100.0):
    """Test data on US trading days: regular swings a trend rule can follow."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-02", periods=n, tz="UTC", name="ts")
    logp = np.log(base) + amp * np.sin(2 * np.pi * np.arange(n) / period + seed) + np.cumsum(rng.normal(0, 0.004, n))
    c = np.exp(logp)
    o = np.concatenate([[c[0]], c[:-1]])
    frame = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998, "close": c, "volume": 5e6}, index=idx)
    return validate_bars(frame, "1d", get_calendar("XNYS"))[0]


class FakeAlpacaData(PriceProvider):
    name, label, markets, min_interval, needs_secret = "alpaca", "Alpaca", frozenset({"US"}), 0.0, True

    def __init__(self, frames):
        self.frames, self.requests = frames, []

    def list_symbols(self, market):
        return []

    def daily_bars(self, instrument, start=None, end=None):
        self.requests.append((instrument.symbol, start))
        if instrument.symbol not in self.frames:
            raise SymbolNotFound(instrument.symbol)
        bars = self.frames[instrument.symbol]
        if start is not None:
            bars = bars[bars.index >= pd.Timestamp(start, tz="UTC")]
        return Dataset(bars, DataKind.HISTORICAL, "alpaca")


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    key = CredentialVault.generate_key()
    aaa = cut_at_entry(us_series(1))          # ends on a bar where the trend strategy opens a position
    end = aaa.index[-1]
    frames = {"AAA": aaa, "BBB": us_series(2)[:end], "CCC": us_series(3, base=2.0)[:end]}   # CCC is a penny stock: below the price floor
    now = [end.to_pydatetime() + timedelta(days=1, hours=1)]
    runtime = TradingRuntime(db, Settings(_env_file=None, credential_key=key, enabled_providers="alpaca"), clock=lambda: now[0])
    runtime.start()
    runtime.create_paper_account("paper-main", "Main", "admin")
    runtime.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", now[0] - timedelta(hours=2))
    runtime.set_provider_key("alpaca", "PK-TEST", "admin", "SK-TEST")
    data = FakeAlpacaData(frames)
    runtime.provider_factory = lambda name, key: data
    runtime.download_options = {"sleep": lambda s: None, "min_interval": 0}
    feeder = Feeder(runtime.live_hub)
    runtime.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.asset_class is AssetClass.STOCK else None)]
    runtime.autopilot.most_active = lambda key, secret, top: ["AAA", "BBB", "CCC", "NOPE"]
    runtime.autopilot.wait_seconds = 60
    runtime.autopilot.fetch_fx = lambda: [("EUR", "USD", now[0] - timedelta(hours=3), 1.1)]  # the ECB publishes one rate a day
    runtime.now, runtime.data, runtime.frames = now, data, frames
    return runtime


def configure(rt, **kw):
    cfg = AutopilotSettings(enabled=True, strategies=["ema_crossover", "rsi_reversion"], max_instruments=10, min_traded_value=1e6, **kw)
    with rt.db.session() as s:
        save_settings(s, initial_settings().model_copy(update={"autopilot": cfg}))
        rt.reload_settings(s)


def test_a_cycle_goes_from_an_empty_universe_to_a_proposal_without_a_click(rt):
    assert rt.autopilot.tick() is None and rt.autopilot.status()["enabled"] is False  # off until the user switches it on
    configure(rt)
    out = rt.autopilot.tick()
    assert out["universe"]["added"] == ["STOCK:AAA", "STOCK:BBB", "STOCK:CCC", "STOCK:NOPE"] and out["universe"]["histories_requested"] == 4
    assert out["candidates"] == {"count": 2, "by_liquidity": 2, "live_only": 0, "broker_id": "paper-main"}  # the penny stock and the one without data are left out
    assert out["validation"] == {"run": 4, "passed": 2, "could_not_run": 0, "remaining": 0}  # the trend rule passes, the mean-reversion rule does not
    assert out["live"] == {"subscribed": 2, "no_live_source": 0} and set(rt.live.subscribed) == {"STOCK:AAA", "STOCK:BBB"}
    assert out["scan"]["validated_setups"] == 2 and out["scan"]["proposed"] == 1
    assert any("rsi_reversion AAA 1d: did not pass validation" == r for r in out["scan"]["reasons"])

    # The outcome is a proposal. Nothing was sent, and the mode and limits are untouched.
    [p] = rt.proposals()
    assert (p["symbol"], p["side"], p["strategy"]) == ("AAA", "BUY", "ema_crossover") and p["risk_base"] <= 20.0
    assert not [o for o in rt.engine.orders.values() if o.status is OrderStatus.SUBMITTED]
    assert rt.user_settings.trading_mode == "semi_auto" and rt.user_settings.risk.risk_per_trade == 0.02
    st = rt.autopilot.status()
    assert st["sends_orders_by_itself"] is False and st["cycles"] == 1 and st["alpaca_key"] and "every order still waits for your approval" in st["note"]
    assert any("1 new proposal" in a["text"] for a in st["activity"]) and any("validations run, 2 passed" in a["text"] for a in st["activity"])

    # Not due yet: the next tick does nothing. Once due, nothing is repeated needlessly.
    assert rt.autopilot.tick() is None
    rt.now[0] += timedelta(minutes=16)
    again = rt.autopilot.tick()
    assert "skipped" in again["universe"] and "skipped" in again["prices"] and again["validation"]["run"] == 0 and again["scan"]["proposed"] == 0
    # The first proposal lapsed after fifteen minutes; the same signal is not proposed a second time.
    assert rt.proposals() == [] and any("lapsed" in r for r in again["scan"]["reasons"])


def test_prices_are_refreshed_daily_and_expired_validations_are_redone(rt):
    configure(rt)
    rt.autopilot.tick()
    first_requests = len(rt.data.requests)
    # Next day: one more bar exists at the provider.
    for symbol, frame in list(rt.frames.items()):
        nxt = frame.index[-1] + pd.offsets.BDay(1)
        last = frame["close"].iloc[-1]
        rt.frames[symbol] = pd.concat([frame, pd.DataFrame({"open": last, "high": last * 1.01, "low": last * 0.99, "close": last * 1.002, "volume": 5e6},
                                                           index=pd.DatetimeIndex([nxt], name="ts"))])
    rt.now[0] += timedelta(days=1)
    out = rt.autopilot.tick()
    assert out["prices"] == {"new_bars": 3, "failed": 0} and len(rt.data.requests) > first_requests
    with rt.db.session() as s:
        assert rt.store.load(s, "STOCK:AAA", "1d").bars.index[-1] == rt.frames["AAA"].index[-1]
    assert out["validation"]["run"] == 0  # still valid: not repeated
    rt.now[0] += timedelta(days=31)
    out = rt.autopilot.run_cycle()
    assert out["exchange_rates"] == {"rates": 1}
    assert out["validation"]["run"] == 4  # thirty days later every validation has lapsed and is redone


def test_autopilot_respects_the_kill_switch_missing_key_and_one_failing_step(rt):
    configure(rt, seed_most_active=False)
    out = rt.autopilot.tick()
    assert out["universe"] == {"skipped": "switched off"} and "error" in out["candidates"] or out["candidates"]["count"] == 0
    configure(rt)
    rt.set_provider_key("alpaca", None, "admin")
    out = rt.autopilot.run_cycle()
    assert "no Alpaca key" in out["universe"]["skipped"] and "no Alpaca key" in out["prices"]["skipped"]
    rt.set_provider_key("alpaca", "PK-TEST", "admin", "SK-TEST")

    def broken(key, secret, top):
        raise ConnectionError("screener unreachable")

    rt.autopilot.most_active = broken
    rt.autopilot.state["last_seed"] = None
    out = rt.autopilot.run_cycle()
    assert "screener unreachable" in out["universe"]["error"] and "scan" in out  # the other steps still ran
    rt.kill_switch(True, "EMERGENCY STOP", "admin")
    assert rt.autopilot.run_cycle()["scan"] == {"skipped": "trading is paused by the risk engine"}
    assert rt.proposals() == []


def test_autopilot_endpoints(client):
    from tests.conftest import VIEWER, login

    admin, viewer = login(client), login(client, VIEWER)
    st = client.get("/api/quant/autopilot", headers=viewer).json()
    assert st["enabled"] is False and st["settings"]["cycle_minutes"] == 15 and st["settings"]["timeframes"] == ["1d"]
    body = {**st["settings"], "enabled": True, "max_instruments": 12}
    assert client.put("/api/quant/autopilot", headers=viewer, json=body).status_code == 403
    assert client.put("/api/quant/autopilot", headers=admin, json={**body, "strategies": ["nope"]}).status_code == 422
    assert client.put("/api/quant/autopilot", headers=admin, json={**body, "cycle_minutes": 1}).status_code == 422
    # The autopilot's own settings cannot carry a trading mode or a risk limit.
    assert client.put("/api/quant/autopilot", headers=admin, json={**body, "trading_mode": "auto"}).json()["trading_mode"] == "semi_auto"
    saved = client.put("/api/quant/autopilot", headers=admin, json=body).json()
    assert saved["enabled"] is True and saved["settings"]["max_instruments"] == 12
    assert client.get("/api/settings", headers=admin).json()["autopilot"]["enabled"] is True
    assert client.post("/api/quant/autopilot/run", headers=viewer).status_code == 403
    assert client.post("/api/quant/autopilot/run", headers=admin).status_code == 202

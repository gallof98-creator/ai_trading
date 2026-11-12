from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.brokers.ccxt_adapter import CcxtAdapter
from app.core.config import Settings
from app.database.base import Database
from app.database.models import AuditEntry, Notification, OrderRow, RiskEvent
from app.database.settings_store import UserSettings, save_settings
from app.execution.runtime import RuntimeError_, TradingRuntime
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset, synthetic_bars
from app.notifications.service import Channel
from tests.execution.fakes import FakeCcxt

BTC = "CRYPTO:BTC/USDT"
NOW = datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc)


class Recorder(Channel):
    name = "recorder"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, title, body, severity):
        if self.fail:
            raise ConnectionError("smtp down")
        self.sent.append((severity, title))


def make_db(capital=50_000.0, mode="auto", **risk):
    db = Database("sqlite://")
    db.create_all()
    with db.session() as s:
        settings = UserSettings(initial_paper_capital=capital, trading_mode=mode)
        save_settings(s, settings.model_copy(update={"risk": settings.risk.model_copy(update=risk)}))
    return db


def runtime(db, **kw):
    rt = TradingRuntime(db, Settings(_env_file=None), clock=lambda: NOW, **kw)
    rt.start()
    return rt


def deployed(db=None, strategy="ema_crossover", params=None, **kw):
    rt = runtime(db or make_db(), **kw)
    rt.create_paper_account("paper-main", "Main", "admin")
    dep = rt.add_deployment(strategy, params or {"fast": 5, "slow": 20}, BTC, "1h", "paper-main", "admin")
    rt.update_deployment(dep, "admin", active=True)
    return rt, dep


def test_paper_account_requires_configured_capital():
    rt = runtime(make_db(capital=None))
    with pytest.raises(RuntimeError_, match="initial paper capital"):
        rt.create_paper_account("paper-main", "Main", "admin")
    assert rt.overview()["configured"] is False
    rt2 = runtime(make_db(capital=25_000))
    assert rt2.create_paper_account("paper-main", "Main", "admin")["initial_capital"] == 25_000
    with pytest.raises(RuntimeError_, match="already exists"):
        rt2.create_paper_account("paper-main", "Again", "admin")
    assert rt2.overview()["equity"] == 25_000 and rt2.overview()["all_simulated"] is True


def test_deployment_validation():
    rt = runtime(make_db())
    rt.create_paper_account("paper-main", "Main", "admin")
    with pytest.raises(ValueError):
        rt.add_deployment("ema_crossover", {"fast": 50, "slow": 5}, BTC, "1h", "paper-main", "admin")
    for args, msg in ((("ema_crossover", {}, "CRYPTO:NOPE", "1h", "paper-main"), "Unknown instrument"),
                      (("ema_crossover", {}, BTC, "7h", "paper-main"), "timeframe"), (("ema_crossover", {}, BTC, "1h", "nope"), "Unknown broker")):
        with pytest.raises(RuntimeError_, match=msg):
            rt.add_deployment(*args, "admin")
    dep = rt.add_deployment("ema_crossover", {}, BTC, "1h", "paper-main", "admin")
    with pytest.raises(RuntimeError_, match="already deployed"):
        rt.add_deployment("ema_crossover", {"fast": 3}, BTC, "1h", "paper-main", "admin")
    assert not rt.deployments  # created inactive: nothing trades until explicitly activated
    rt.update_deployment(dep, "admin", active=True, params={"fast": 8, "slow": 21})
    assert rt.deployments[dep].strategy.params.fast == 8
    rt.delete_deployment(dep, "admin")
    assert not rt.deployments


def test_full_paper_cycle_on_simulated_replay_is_recorded_and_labelled():
    db = make_db()
    channel = Recorder()
    rt, _ = deployed(db, channels=[channel])
    with pytest.raises(RuntimeError_, match="No usable FX rate"):
        rt.replay_synthetic(BTC, "1h", 50, seed=3, actor="admin")
    out = rt.replay_synthetic(BTC, "1h", 600, seed=3, actor="admin", fx_assumptions={"USD": 0.92})
    assert out == {"bars_processed": 600, "data_kind": "simulated", "remaining": 2400}
    ov = rt.overview()
    assert ov["number_of_trades"] > 5 and ov["data_kinds"] == {BTC: "simulated"} and ov["reconciled"] is True
    assert ov["equity"] == pytest.approx(50_000 + ov["total_pnl"]) and ov["brokers"][0]["is_simulated"]
    assert ov["equity"] == pytest.approx(50_000 + ov["realized_pnl"] + ov["unrealized_pnl"], rel=2e-3)  # differ only by conversion costs
    with db.session() as s:
        orders = s.scalars(select(OrderRow)).all()
        assert len(orders) > 10 and all(o.is_simulated for o in orders)  # paper orders can never be mistaken for real ones
        assert len({o.idempotency_key for o in orders}) == len(orders)
        cats = {n.category for n in s.scalars(select(Notification))}
        assert {"signal", "execution"} <= cats
        assert s.scalar(select(AuditEntry).where(AuditEntry.action == "paper.simulated_replay")) is not None
    assert all(sev in ("warning", "critical") for sev, _ in channel.sent)  # info-level events stay in-app
    assert len(rt.equity_history()) > 100


def test_state_survives_restart_and_continues_identically():
    db_a, db_b = make_db(), make_db()
    a, _ = deployed(db_a)
    a.replay_synthetic(BTC, "1h", 500, seed=7, actor="admin", fx_assumptions={"USD": 0.92})
    b, _ = deployed(db_b)
    b.replay_synthetic(BTC, "1h", 250, seed=7, actor="admin", fx_assumptions={"USD": 0.92})
    b2 = runtime(db_b)  # "restart": a brand-new process reading the database
    assert b2.overview()["equity"] == pytest.approx(b.overview()["equity"])
    assert set(b2.deployments) == set(b.deployments) and b2.portfolio.slices.keys() == b.portfolio.slices.keys()
    assert {k: (p.stop_price, p.stop_order.id if p.stop_order else None) for k, p in b2.engine.protections.items()} == \
           {k: (p.stop_price, p.stop_order.id if p.stop_order else None) for k, p in b.engine.protections.items()}
    b2.replay_synthetic(BTC, "1h", 250, seed=7, actor="admin")
    assert b2.overview()["equity"] == pytest.approx(a.overview()["equity"])
    assert [t.to_dict() for t in b2.portfolio.trades] == [t.to_dict() for t in a.portfolio.trades]


def test_reset_requires_typed_confirmation_and_restores_starting_capital():
    db = make_db()
    rt, _ = deployed(db)
    rt.replay_synthetic(BTC, "1h", 400, seed=3, actor="admin", fx_assumptions={"USD": 0.92})
    before = rt.overview()["equity"]
    assert before != 50_000
    with pytest.raises(RuntimeError_, match="Confirmation does not match"):
        rt.reset_paper_account("paper-main", "yes", "admin")
    assert rt.overview()["equity"] == before
    rt.reset_paper_account("paper-main", "paper-main", "admin")
    ov = rt.overview()
    assert ov["equity"] == 50_000 and ov["positions"] == [] and ov["number_of_trades"] == 0 and ov["reconciled"]
    assert not rt.brokers.get("paper-main").get_open_orders() and not rt.engine.protections
    with db.session() as s:
        assert s.scalar(select(AuditEntry).where(AuditEntry.action == "paper.account_reset")) is not None


def test_simulated_and_market_data_are_never_mixed():
    rt, _ = deployed()
    rt.replay_synthetic(BTC, "1h", 50, seed=3, actor="admin", fx_assumptions={"USD": 0.92})
    real = Dataset(synthetic_bars("1h", 10, seed=1, start="2024-06-03").bars, DataKind.HISTORICAL, "exchange")
    with pytest.raises(RuntimeError_, match="never mixed"):
        rt.ingest(BTC, "1h", real)


def test_live_feed_first_batch_only_warms_up_then_new_bars_trade():
    db = make_db()
    ex = FakeCcxt(api_key=None)
    hour = 3_600_000
    ex.now_ms = int(pd.Timestamp(NOW).timestamp() * 1000)
    series = synthetic_bars("1h", 500, seed=5, start="2024-05-13 12:00").bars  # ends well before NOW
    cursor = {"n": 300}

    def ohlcv(symbol, timeframe, limit=500):
        part = series.iloc[: cursor["n"]]
        return [[int(ts.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume] for ts, r in part.iterrows()]

    ex.fetch_ohlcv = ohlcv
    ex.timeframes = {"1h": "1h"}
    from app.instruments.registry import InstrumentRegistry
    rt = TradingRuntime(db, Settings(_env_file=None), clock=lambda: NOW)
    rt.data_adapter = CcxtAdapter("data-fakex", rt.registry, ex)
    rt.data_adapter.connect()
    rt.start()
    rt.create_paper_account("paper-main", "Main", "admin")
    dep = rt.add_deployment("ema_crossover", {"fast": 5, "slow": 20}, BTC, "1h", "paper-main", "admin")
    rt.update_deployment(dep, "admin", active=True)
    rt.add_fx_rate("EUR", "USD", 1.08, "manual", "admin", ts=series.index[0].to_pydatetime())
    rt.fx.max_age = pd.Timedelta(days=60).to_pytimedelta()
    assert rt.data_adapter.capabilities.trading_enabled is False
    status = rt.tick()
    assert status[BTC].startswith("ok") and not rt.engine.orders  # history is never traded retroactively
    assert rt.kinds[BTC] == "historical"
    cursor["n"] = 500
    rt.tick()
    assert rt.engine.orders and rt.portfolio.trades  # new completed bars drive the paper engine
    assert all(o.is_simulated for o in rt.engine.orders.values())
    assert rt.tick()[BTC].endswith("0 new bars)")


def test_database_failure_halts_new_risk_and_alert_channel_failure_is_harmless():
    db = make_db()
    bad_channel = Recorder(fail=True)
    rt, _ = deployed(db, channels=[bad_channel])
    rt.replay_synthetic(BTC, "1h", 100, seed=3, actor="admin", fx_assumptions={"USD": 0.92})  # failing channel does not break trading
    assert not rt.risk.kill_switch_on
    rt.journal.failed = "disk full"  # what DbJournal records when a write raises
    rt.persist()
    assert rt.risk.kill_switch_on and "database" in rt.risk.kill_switch_reason
    n = len(rt.engine.orders)
    rt.replay_synthetic(BTC, "1h", 300, seed=3, actor="admin")
    opened = [o for o in list(rt.engine.orders.values())[n:] if not o.reduce_only and o.status.value != "rejected"]
    assert opened == []  # only exits and rejections after the halt


def test_kill_switch_and_breaker_controls_are_audited_and_persist():
    db = make_db()
    rt, _ = deployed(db)
    rt.kill_switch(True, "maintenance", "admin@example.com")
    assert runtime(db).risk.kill_switch_reason == "maintenance"
    rt.kill_switch(False, "", "admin@example.com")
    with pytest.raises(RuntimeError_, match="not tripped"):
        rt.reset_breaker("drawdown", "admin")
    with db.session() as s:
        actions = [a.action for a in s.scalars(select(AuditEntry))]
        kinds = [e.kind for e in s.scalars(select(RiskEvent))]
    assert "risk.kill_switch_on" in actions and "risk.kill_switch_off" in actions
    assert kinds == ["kill_switch_on", "kill_switch_off"]


def test_semi_auto_approval_flow_through_runtime():
    rt, _ = deployed(make_db(mode="semi_auto"))
    rt.replay_synthetic(BTC, "1h", 200, seed=3, actor="admin", fx_assumptions={"USD": 0.92})
    assert rt.engine.approvals and not rt.portfolio.slices and not rt.portfolio.trades
    approval_id = next(iter(rt.engine.approvals))
    order = rt.resolve_approval(approval_id, True, "trader@example.com")
    assert order.status.value in ("submitted", "rejected")
    with pytest.raises(RuntimeError_):
        rt.resolve_approval("missing", True, "x")


def test_connecting_external_brokers_respects_the_live_gate_and_approval_default():
    from app.database.models import BrokerAccount
    from app.security.credentials import store_credentials
    from app.security.vault import CredentialVault

    db = make_db()
    key = CredentialVault.generate_key()
    with db.session() as s:
        s.add(BrokerAccount(id="binance-test", adapter="ccxt", label="Testnet", is_live=False, currency="USDT", config={"exchange": "binance"}))
        s.add(BrokerAccount(id="binance-live", adapter="ccxt", label="Live", is_live=True, currency="USDT", config={"exchange": "binance"}))
        s.flush()
        store_credentials(s, CredentialVault(key), "binance-test", {"api_key": "k123", "api_secret": "s"}, ["read", "trade"], "admin")
    rt = TradingRuntime(db, Settings(_env_file=None, credential_key=key), clock=lambda: NOW)
    rt.start()
    made = {}

    def factory(options):
        made["options"] = options
        made["ex"] = FakeCcxt(api_key=options.get("apiKey"))
        return made["ex"]

    out = rt.connect_broker("binance-test", "admin", exchange_factory=factory)
    assert out["capabilities"]["trading_enabled"] and made["ex"].sandbox is True and made["options"]["apiKey"] == "k123"
    with pytest.raises(RuntimeError_, match="Live trading is disabled"):
        rt.connect_broker("binance-live", "admin", exchange_factory=factory)
    with pytest.raises(RuntimeError_, match="already connected"):
        rt.connect_broker("binance-test", "admin", exchange_factory=factory)
    from app.domain.models import Order, Side
    order = Order(broker_id="binance-test", instrument_id=BTC, side=Side.BUY, qty=0.01)
    assert rt._needs_approval(order) is True  # non-simulated venue: human approval by default
    assert [b["id"] for b in rt.broker_status()] == ["binance-test"]
    rt.disconnect_broker("binance-test", "admin")
    assert rt.broker_status() == []

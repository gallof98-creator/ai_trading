from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.brokers.base import BrokerManager, ConnectionLost, RateLimited
from app.brokers.ccxt_adapter import CcxtAdapter
from app.database.settings_store import RiskLimits
from app.domain.fees import CommissionModel
from app.domain.models import OrderStatus, OrderType, Signal, SignalAction, TradingMode
from app.execution.engine import Deployment, RetryPolicy, TradingEngine
from app.instruments.registry import InstrumentRegistry, default_instruments
from app.market_data.fx import FxRateProvider
from app.paper_trading.broker import ExecutionModel, SimulatedBroker
from app.portfolio.engine import PortfolioEngine
from app.risk.engine import RiskEngine
from app.strategies.registry import create_strategy
from tests.execution.fakes import FakeCcxt

T0 = datetime(2024, 3, 5, 15, tzinfo=timezone.utc)
H = pd.Timedelta(hours=1)
BTC, ETH = "CRYPTO:BTC/USDT", "CRYPTO:ETH/USDT"
FREE = ExecutionModel(spread_bps=0, slippage_bps=0, participation_rate=None, commissions={"crypto": CommissionModel()})


class Harness:
    def __init__(self, mode=TradingMode.AUTO, limits=None, adapter_factory=None):
        self.mode = mode
        self.limits = limits or RiskLimits(max_exposure_per_asset=5, max_exposure_per_strategy=5, max_exposure_per_broker=5,
                                           max_gross_leverage=5, max_margin_utilization=1, max_correlated_exposure=5)
        self.registry = InstrumentRegistry()
        for inst in default_instruments():
            self.registry.register(inst)
        self.fx = FxRateProvider(pegs={"USDT": "EUR", "USD": "EUR"})
        self.brokers = BrokerManager()
        if adapter_factory:
            self.broker = adapter_factory(self)
        else:
            self.broker = SimulatedBroker("paper", self.registry, self.fx, 100_000, "EUR", FREE)
            self.broker.connect()
            self.broker.map_all_instruments()
        self.brokers.register(self.broker)
        self.portfolio = PortfolioEngine(self.registry, self.brokers, self.fx)
        self.sleeps = []
        self.risk = None
        self.engine = None
        self._build()
        self.dep = Deployment("d1", create_strategy("ema_crossover"), BTC, "1h", self.broker.broker_id)
        self.i = 0

    def _build(self):
        engine_ref = {}
        self.risk = RiskEngine(lambda: self.limits, lambda e: engine_ref["e"].journal.risk_event(e))
        self.engine = TradingEngine(self.registry, self.brokers, self.portfolio, self.risk, self.fx, lambda: self.limits, lambda: self.mode,
                                    retry=RetryPolicy(max_attempts=3, backoff_seconds=1.0), sleep=self.sleeps.append)
        engine_ref["e"] = self.engine

    def bar(self, o, h, l, c, inst=BTC):
        ts = T0 + self.i * timedelta(hours=1)
        self.engine.on_bar_open(inst, ts, o, 1e9, H)
        self.engine.on_bar_close(inst, ts, h, l, c, H)
        self.engine.end_of_timestamp(ts + timedelta(hours=1))
        self.i += 1
        return ts + timedelta(hours=1)

    def signal(self, target, ts, price=100.0, stop=10.0, tp=None, trail=None, action=None, dep=None, bar_tag=None):
        dep = dep or self.dep
        action = action or (SignalAction.BUY if target > 0 else SignalAction.SELL)
        sig = Signal(ts=ts, instrument_id=dep.instrument_id, timeframe="1h", strategy=dep.strategy.name, strategy_version="1.0.0",
                     action=action, target=target, price=price, stop_distance=stop, take_profit_distance=tp, trailing_distance=trail,
                     meta={"bar_open": bar_tag or ts.isoformat()})
        return self.engine.on_signal(sig, dep)

    def statuses(self):
        return [(o.reason, o.type.value, o.status.value) for o in self.engine.orders.values()]


def test_manual_mode_records_signals_but_never_creates_orders():
    h = Harness(TradingMode.MANUAL)
    ts = h.bar(100, 101, 99, 100)
    assert h.signal(1, ts) == [] and len(h.engine.journal.signals) == 1 and not h.engine.orders


def test_auto_mode_full_cycle_entry_protection_and_exit():
    h = Harness()
    ts = h.bar(100, 101, 99, 100)
    (entry,) = h.signal(1, ts, tp=20.0)
    assert entry.status is OrderStatus.SUBMITTED and entry.qty == pytest.approx(100.0) and entry.is_simulated
    ts = h.bar(100, 101, 99, 100)  # fills at the open; stop and target are placed immediately
    assert h.portfolio.qty(h.dep.key) == 100
    prot = h.engine.protections[h.dep.key]
    assert prot.stop_order.stop_price == 90 and prot.target_order.limit_price == 120 and prot.stop_order.reduce_only
    ts = h.bar(100, 125, 99, 124)  # target hit -> stop cancelled (OCO), protection removed
    assert h.portfolio.qty(h.dep.key) == 0 and h.dep.key not in h.engine.protections
    assert ("stop_loss", "stop", "cancelled") in h.statuses() and ("take_profit", "limit", "filled") in h.statuses()
    assert h.portfolio.trades[0].pnl == pytest.approx(2000) and h.portfolio.trades[0].exit_reason == "take_profit"
    assert h.portfolio.reconcile() == []


def test_duplicate_signal_for_the_same_bar_cannot_create_a_second_order():
    h = Harness()
    ts = h.bar(100, 101, 99, 100)
    first = h.signal(1, ts)
    again = h.signal(1, ts)  # e.g. the feed delivered the same bar twice, or the process restarted and replayed
    assert again[0].id == first[0].id and len(h.engine.orders) == 1
    h.bar(100, 101, 99, 100)
    assert h.portfolio.qty(h.dep.key) == 100
    assert h.signal(1, ts + timedelta(hours=1), bar_tag=ts.isoformat()) == []  # already long: nothing to do


def test_reversal_closes_then_opens_and_stopped_position_is_not_reentered_on_hold():
    h = Harness()
    ts = h.bar(100, 101, 99, 100)
    h.signal(1, ts)
    ts = h.bar(100, 101, 99, 100)
    close, entry = h.signal(-1, ts)
    assert close.reduce_only and close.side.value == "sell" and entry.side.value == "sell" and not entry.reduce_only
    ts = h.bar(100, 101, 99, 100)
    assert h.portfolio.qty(h.dep.key) == -100 and h.engine.protections[h.dep.key].stop_order.stop_price == 110
    ts = h.bar(100, 112, 99, 111)  # short stopped out
    assert h.portfolio.qty(h.dep.key) == 0
    assert h.signal(-1, ts, action=SignalAction.HOLD) == []  # target still short, but no fresh signal -> stay flat


def test_semi_auto_requires_approval_and_rechecks_risk_at_approval_time():
    h = Harness(TradingMode.SEMI_AUTO)
    ts = h.bar(100, 101, 99, 100)
    (pending,) = h.signal(1, ts)
    assert pending.status is OrderStatus.PENDING_APPROVAL and not h.broker.get_open_orders()
    h.bar(100, 101, 99, 100)
    assert h.portfolio.qty(h.dep.key) == 0  # nothing happens without approval
    approval_id = next(iter(h.engine.approvals))
    approved = h.engine.resolve_approval(approval_id, True, "trader@example.com", ts + timedelta(hours=1))
    assert approved.status is OrderStatus.SUBMITTED
    ts = h.bar(100, 101, 99, 100)
    assert h.portfolio.qty(h.dep.key) == 100 and h.engine.protections[h.dep.key].stop_order is not None

    # Declined
    (p2,) = h.signal(0, ts, action=SignalAction.SELL)
    declined = h.engine.resolve_approval(next(iter(h.engine.approvals)), False, "trader@example.com", ts)
    assert declined.status is OrderStatus.CANCELLED and "declined by trader@example.com" in declined.reason
    with pytest.raises(KeyError):
        h.engine.resolve_approval("nope", True, "x", ts)

    # Risk is evaluated again when the approval arrives: the kill switch now blocks it.
    h3 = Harness(TradingMode.SEMI_AUTO)
    ts = h3.bar(100, 101, 99, 100)
    h3.signal(1, ts)
    h3.risk.engage_kill_switch(ts, "operator", "admin@example.com")
    blocked = h3.engine.resolve_approval(next(iter(h3.engine.approvals)), True, "trader@example.com", ts)
    assert blocked.status is OrderStatus.REJECTED and "kill switch" in blocked.reason


def test_new_signal_supersedes_a_pending_approval():
    h = Harness(TradingMode.SEMI_AUTO)
    ts = h.bar(100, 101, 99, 100)
    (old,) = h.signal(1, ts)
    (new,) = h.signal(-1, ts + timedelta(hours=1))
    assert h.engine.orders[old.id].status is OrderStatus.CANCELLED and "superseded" in h.engine.orders[old.id].reason
    assert len(h.engine.approvals) == 1 and new.status is OrderStatus.PENDING_APPROVAL


def test_risk_rejection_is_logged_and_notified():
    h = Harness(limits=RiskLimits(max_concurrent_positions=1, max_exposure_per_asset=5, max_exposure_per_strategy=5, max_exposure_per_broker=5,
                                  max_gross_leverage=5, max_margin_utilization=1, max_correlated_exposure=5))
    ts = h.bar(100, 101, 99, 100)
    h.bar(2000, 2001, 1999, 2000, inst=ETH)
    h.i -= 1
    h.signal(1, ts)
    ts = h.bar(100, 101, 99, 100)
    eth = Deployment("d2", create_strategy("macd"), ETH, "1h", "paper")
    (rejected,) = h.signal(1, ts, price=2000.0, stop=100.0, dep=eth)
    assert rejected.status is OrderStatus.REJECTED and "concurrent positions" in rejected.reason
    assert h.engine.journal.risk_events[-1].kind == "order_rejected"
    assert h.engine.journal.alerts[-1]["category"] == "order_rejected"
    assert not [o for o in h.broker.orders.values() if o.instrument_id == ETH]  # never reached the venue


# ---------- emergency policies ----------
def _long_position_with_pending_order(policy):
    limits = RiskLimits(emergency_policy=policy, max_exposure_per_asset=5, max_exposure_per_strategy=5, max_exposure_per_broker=5,
                        max_gross_leverage=5, max_margin_utilization=1, max_correlated_exposure=5)
    h = Harness(limits=limits)
    ts = h.bar(100, 101, 99, 100)
    h.signal(1, ts)
    ts = h.bar(100, 101, 99, 100)
    eth = Deployment("d2", create_strategy("macd"), ETH, "1h", "paper")
    h.signal(1, ts, price=2000.0, stop=100.0, dep=eth)  # working entry order, not yet filled
    h.engine.engage_kill_switch("test", "admin@example.com", ts)
    return h, ts


def test_default_policy_blocks_new_risk_and_touches_nothing_else():
    h, ts = _long_position_with_pending_order("block_new_risk")
    assert h.portfolio.qty(h.dep.key) == 100 and len(h.broker.get_open_orders()) == 2  # stop + pending ETH entry untouched
    assert h.signal(-1, ts)[1].status is OrderStatus.REJECTED  # reversal: the close is allowed, the new short is not
    h.bar(100, 101, 99, 100)
    assert h.portfolio.qty(h.dep.key) == 0


def test_cancel_policy_cancels_entries_but_keeps_protective_stops():
    h, ts = _long_position_with_pending_order("cancel_open_orders")
    open_orders = h.broker.get_open_orders()
    assert [(o.type, o.reason) for o in open_orders] == [(OrderType.STOP, "stop_loss")]
    assert h.portfolio.qty(h.dep.key) == 100


def test_flatten_policy_closes_positions_only_when_configured():
    h, ts = _long_position_with_pending_order("flatten_positions")
    assert [(o.reason, o.reduce_only) for o in h.broker.get_open_orders()] == [("emergency", True)]
    h.bar(98, 99, 97, 98)
    assert h.portfolio.qty(h.dep.key) == 0 and h.portfolio.trades[-1].exit_reason == "emergency" and not h.broker.get_open_orders()
    assert any(a["severity"] == "critical" for a in h.engine.journal.alerts)


def test_drawdown_breaker_trips_from_market_moves():
    h = Harness(limits=RiskLimits(max_drawdown=0.05, max_daily_loss=0.5, risk_per_trade=0.05, max_exposure_per_asset=5, max_exposure_per_strategy=5,
                                  max_exposure_per_broker=5, max_gross_leverage=5, max_margin_utilization=1, max_correlated_exposure=5))
    ts = h.bar(100, 101, 99, 100)
    h.signal(1, ts, stop=50.0)
    h.bar(100, 101, 99, 100)
    h.bar(100, 100, 40, 45)  # 100 units * -55 = -5.5% of equity
    assert "drawdown" in h.risk.breakers
    assert [e.kind for e in h.engine.journal.risk_events] == ["breaker_tripped"]


# ---------- connection failures, retries, idempotency against a (mock) real exchange ----------
def ccxt_harness():
    def factory(h):
        h.exchange = FakeCcxt()
        adapter = CcxtAdapter("binance-test", h.registry, h.exchange, account_currency="USDT")
        adapter.connect()
        return adapter

    h = Harness(adapter_factory=factory)
    h.portfolio.mark(BTC, 30000.0, T0)
    return h


def test_transient_failures_are_retried_with_backoff_and_create_exactly_one_order():
    h = ccxt_harness()
    import ccxt
    h.exchange.fail_next = [ccxt.RequestTimeout("t1"), ccxt.RateLimitExceeded("429")]
    (order,) = h.signal(1, T0, price=30000.0, stop=3000.0)
    assert order.status is OrderStatus.SUBMITTED and order.is_simulated is False
    assert h.exchange.create_calls == 3 and len(h.exchange.orders) == 1 and h.sleeps == [1.0, 2.0]


def test_lost_response_is_reconciled_instead_of_resubmitted():
    h = ccxt_harness()
    import ccxt
    h.exchange.fail_after_accept = ccxt.RequestTimeout("the order was accepted but the reply never arrived")
    (order,) = h.signal(1, T0, price=30000.0, stop=3000.0)
    assert order.status is OrderStatus.SUBMITTED and order.broker_order_id == "1"
    assert h.exchange.create_calls == 1 and len(h.exchange.orders) == 1 and h.sleeps == []


def test_unreachable_broker_gives_up_rejects_and_raises_a_critical_alert():
    h = ccxt_harness()
    import ccxt
    h.exchange.fail_next = [ccxt.NetworkError("down")] * 5
    (order,) = h.signal(1, T0, price=30000.0, stop=3000.0)
    assert order.status is OrderStatus.REJECTED and "unreachable after 3 attempts" in order.reason
    assert h.exchange.create_calls == 3 and not h.exchange.orders
    assert h.engine.journal.alerts[-1]["severity"] == "critical"
    assert h.signal(1, T0, price=30000.0, stop=3000.0)[0].id == order.id and h.exchange.create_calls == 3  # no silent re-send


def test_real_broker_fills_are_ingested_by_polling_and_stop_is_placed_at_the_exchange():
    h = ccxt_harness()
    (order,) = h.signal(1, T0, price=30000.0, stop=3000.0)
    half, rest = 0.02, order.qty - 0.02
    h.exchange.fill("1", half, 30010.0, fee=1.5)
    h.engine.poll_orders(T0 + timedelta(minutes=1))
    assert h.portfolio.qty(h.dep.key) == pytest.approx(half)
    assert h.engine.orders[order.id].status is OrderStatus.PARTIALLY_FILLED
    stop = h.engine.protections[h.dep.key].stop_order
    assert stop is not None and stop.status is OrderStatus.SUBMITTED and stop.side.value == "sell"
    assert h.exchange.orders[stop.broker_order_id]["params"]["triggerPrice"] == pytest.approx(27010.0)
    assert "reduceOnly" not in h.exchange.orders[stop.broker_order_id]["params"]  # spot venue: flag is advisory only
    h.engine.poll_orders(T0 + timedelta(minutes=2))  # nothing new -> no double counting
    assert h.portfolio.qty(h.dep.key) == pytest.approx(half)
    h.exchange.fill("1", rest, 30020.0, fee=1.5)
    h.engine.poll_orders(T0 + timedelta(minutes=3))
    assert h.portfolio.qty(h.dep.key) == pytest.approx(order.qty)
    assert h.portfolio.slices[h.dep.key].avg_price == pytest.approx((half * 30010 + rest * 30020) / order.qty)
    live_stops = [o for o in h.exchange.orders.values() if o["status"] == "open" and "triggerPrice" in o["params"]]
    assert len(live_stops) == 1 and live_stops[0]["amount"] == pytest.approx(order.qty)  # one stop, resized to the full position


def test_netting_venue_drops_native_reduce_only_when_strategies_share_an_instrument():
    def factory(h):
        h.exchange = FakeCcxt(default_type="swap")
        adapter = CcxtAdapter("bybit-test", h.registry, h.exchange, account_currency="USDT")
        adapter.connect()
        return adapter

    h = Harness(adapter_factory=factory)
    h.portfolio.mark(BTC, 30000.0, T0)
    (a,) = h.signal(1, T0, price=30000.0, stop=3000.0)
    h.exchange.fill(a.broker_order_id, a.qty, 30000.0)
    h.engine.poll_orders(T0)
    stop_a = h.engine.protections[h.dep.key].stop_order
    assert h.exchange.orders[stop_a.broker_order_id]["params"].get("reduceOnly") is True  # alone on the instrument: native flag used
    other = Deployment("d2", create_strategy("macd"), BTC, "1h", "bybit-test")
    (b,) = h.signal(-1, T0 + timedelta(hours=1), price=30000.0, stop=3000.0, dep=other)
    h.exchange.fill(b.broker_order_id, b.qty, 30000.0)
    h.engine.poll_orders(T0 + timedelta(hours=1))
    stop_b = h.engine.protections[other.key].stop_order
    assert "reduceOnly" not in h.exchange.orders[stop_b.broker_order_id]["params"]  # net position is flat: the venue would refuse it
    assert stop_b.reduce_only is True  # our own books still treat it as an exit

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app.database.settings_store import RiskLimits
from app.domain.models import AssetClass, Instrument, Order, Side
from app.risk.engine import RiskEngine
from app.risk.models import EmergencyAction, Exposure, OrderIntent, PortfolioState
from app.risk.sizing import position_size

T0 = datetime(2024, 3, 4, 10, tzinfo=timezone.utc)
STOCK = Instrument(id="STOCK:AAPL", asset_class=AssetClass.STOCK, symbol="AAPL", quote_currency="USD", lot_size=1, min_qty=1, margin_rate=0.5)
OTHER = STOCK.model_copy(update={"id": "STOCK:MSFT", "symbol": "MSFT"})
ES = Instrument(id="FUTURE:ES", asset_class=AssetClass.FUTURE, symbol="ES", quote_currency="USD", lot_size=1, min_qty=1,
                contract_multiplier=50, margin_rate=0.06)
BTC = Instrument(id="CRYPTO:BTC/USDT", asset_class=AssetClass.CRYPTO, symbol="BTC/USDT", quote_currency="USDT", lot_size=0.0001, min_qty=0.0001)


def make(limits=None):
    limits = limits or RiskLimits(max_gross_leverage=5, max_exposure_per_broker=5, max_exposure_per_strategy=5, max_exposure_per_asset=5,
                                  max_margin_utilization=1, max_correlated_exposure=5)
    events = []
    return RiskEngine(lambda: limits, events.append), events, limits


def state(equity=100_000.0, exposures=(), day_start=None, peak=None, ts=T0, **kw):
    return PortfolioState(ts=ts, equity=equity, day_start_equity=day_start or equity, peak_equity=peak or equity, exposures=list(exposures), **kw)


def intent(qty=10, price=100.0, stop=5.0, side=Side.BUY, inst=STOCK, strategy="s1", broker="paper", fx=1.0, reduce_only=False):
    order = Order(broker_id=broker, instrument_id=inst.id, side=side, qty=qty, strategy=strategy, reduce_only=reduce_only)
    return OrderIntent(order=order, instrument=inst, price=price, fx_rate=fx, stop_distance=stop)


def exp(notional, inst=STOCK, strategy="s1", broker="paper", margin=0.0):
    return Exposure(broker, inst.id, strategy, inst.asset_class.value, inst.quote_currency, notional, margin)


# ---------- sizing ----------
def test_position_size_risks_exactly_the_configured_fraction():
    qty = position_size(100_000, 0.01, stop_distance=5, price=100, instrument=STOCK, fx_rate=1.0)
    assert qty == 200 and qty * 5 == 1000  # stopped out => lose 1% of equity
    assert position_size(100_000, 0.01, 5, 100, STOCK, fx_rate=0.5) == 400  # each USD of loss costs 0.5 EUR
    assert position_size(100_000, 0.01, 10, 5000, ES, 1.0) == 2  # 1000 / (10 * 50)
    assert position_size(100_000, 0.01, 30, 5000, ES, 1.0) == 0  # one contract would risk 1.5% -> no trade, never round up
    assert position_size(10_000, 0.01, 1500, 60_000, BTC, 0.9) == pytest.approx(0.074)  # 100 / (1500 * 0.9) = 0.07407 -> rounded down to the lot


def test_position_size_caps_and_volatility_adjustment():
    assert position_size(100_000, 0.01, 5, 100, STOCK, 1.0, max_notional=15_000) == 150
    assert position_size(100_000, 0.01, 5, 100, STOCK, 1.0, realized_vol=0.8, target_vol=0.4) == 100  # twice the target vol -> half size
    assert position_size(100_000, 0.01, 5, 100, STOCK, 1.0, realized_vol=0.2, target_vol=0.4) == 200  # never scaled up
    for bad in ((0, 0.01, 5), (100_000, 0, 5), (100_000, 0.01, 0), (-5, 0.01, 5)):
        assert position_size(bad[0], bad[1], bad[2], 100, STOCK, 1.0) == 0


# ---------- pre-trade limits ----------
def test_order_within_limits_is_approved_and_stop_is_mandatory():
    engine, events, _ = make()
    assert engine.evaluate(intent(qty=200, stop=5), state()).approved
    d = engine.evaluate(intent(qty=200, stop=None), state())
    assert not d.approved and "no stop-loss" in d.reasons[0]
    assert events[-1].kind == "order_rejected" and events[-1].detail["reasons"] == d.reasons


def test_risk_per_trade_limit():
    engine, _, _ = make()
    assert engine.evaluate(intent(qty=200, stop=5), state()).approved  # exactly 1%
    d = engine.evaluate(intent(qty=201, stop=5), state())
    assert not d.approved and "risk per trade" in d.reasons[0]
    assert not engine.evaluate(intent(qty=200, stop=5, fx=1.1), state()).approved  # FX makes the same order riskier in EUR


@pytest.mark.parametrize("field,label,kw", [
    ("max_exposure_per_asset", "exposure in STOCK:AAPL", {}),
    ("max_exposure_per_strategy", "exposure of strategy s1", {}),
    ("max_exposure_per_broker", "exposure at broker paper", {}),
    ("max_gross_leverage", "gross exposure", {}),
])
def test_exposure_limits(field, label, kw):
    base = dict(max_gross_leverage=5, max_exposure_per_broker=5, max_exposure_per_strategy=5, max_exposure_per_asset=5,
                max_margin_utilization=1, max_correlated_exposure=5)
    engine, _, _ = make(RiskLimits(**{**base, field: 0.3}))
    existing = [exp(20_000, inst=OTHER)]  # 20% already held elsewhere (same strategy, same broker)
    ok_qty = 100 if field != "max_exposure_per_asset" else 300
    assert engine.evaluate(intent(qty=ok_qty, stop=1), state(exposures=existing)).approved
    bad_qty = 101 if field != "max_exposure_per_asset" else 301
    d = engine.evaluate(intent(qty=bad_qty, stop=1), state(exposures=existing))
    assert not d.approved and any(label in r for r in d.reasons), d.reasons


def test_margin_and_concurrent_position_limits():
    engine, _, _ = make(RiskLimits(max_gross_leverage=5, max_exposure_per_broker=5, max_exposure_per_strategy=5, max_exposure_per_asset=5,
                                   max_margin_utilization=0.5, max_correlated_exposure=5, max_concurrent_positions=1))
    d = engine.evaluate(intent(qty=1001, stop=0.5), state())  # 100,100 notional * 50% margin > 50% of equity
    assert any("margin utilisation" in r for r in d.reasons)
    held = [exp(10_000, inst=OTHER)]
    d = engine.evaluate(intent(qty=10, stop=1), state(exposures=held))
    assert any("concurrent positions" in r for r in d.reasons)
    assert engine.evaluate(intent(qty=10, stop=1, inst=OTHER), state(exposures=held)).approved  # adding to an existing position is not a new one


def test_correlation_limit_counts_effectively_same_direction_positions():
    engine, _, _ = make(RiskLimits(max_gross_leverage=5, max_exposure_per_broker=5, max_exposure_per_strategy=5, max_exposure_per_asset=5,
                                   max_margin_utilization=1, max_correlated_exposure=0.4, correlation_threshold=0.8))
    held = [exp(30_000, inst=OTHER)]
    high = {("STOCK:AAPL", "STOCK:MSFT"): 0.9}
    d = engine.evaluate(intent(qty=150, stop=1), state(exposures=held, correlations=high))
    assert any("correlated exposure" in r for r in d.reasons)
    assert engine.evaluate(intent(qty=150, stop=1), state(exposures=held, correlations={("STOCK:AAPL", "STOCK:MSFT"): 0.5})).approved
    # Shorting a highly correlated asset is a hedge, not concentration.
    assert engine.evaluate(intent(qty=150, stop=1, side=Side.SELL), state(exposures=held, correlations=high)).approved
    # Long a NEGATIVELY correlated asset while long the other is also a hedge; short it and it is concentration.
    neg = {("STOCK:AAPL", "STOCK:MSFT"): -0.9}
    assert engine.evaluate(intent(qty=150, stop=1), state(exposures=held, correlations=neg)).approved
    assert not engine.evaluate(intent(qty=150, stop=1, side=Side.SELL), state(exposures=held, correlations=neg)).approved


# ---------- breakers, kill switch, emergency policy ----------
def test_daily_loss_breaker_blocks_new_risk_but_allows_reduction_and_resets_next_day():
    engine, events, _ = make()
    assert engine.monitor(state(equity=97_100, day_start=100_000)) == [] and not engine.halted
    assert engine.monitor(state(equity=97_000, day_start=100_000)) == []  # default policy: block only, no liquidation
    assert engine.halted and events[-1].kind == "breaker_tripped" and events[-1].severity == "critical"
    held = [exp(20_000)]
    d = engine.evaluate(intent(qty=10, stop=1), state(equity=97_000, exposures=held))
    assert not d.approved and "daily_loss" in d.reasons[0]
    assert engine.evaluate(intent(qty=100, side=Side.SELL, stop=None), state(equity=97_000, exposures=held)).approved  # closing is allowed
    flip = engine.evaluate(intent(qty=300, side=Side.SELL, stop=1), state(equity=97_000, exposures=held))
    assert not flip.approved  # selling through zero into a short is new risk
    n = len(events)
    engine.monitor(state(equity=96_000, day_start=100_000))
    assert len(events) == n  # already tripped: no duplicate alerts
    engine.monitor(state(equity=96_000, day_start=96_000, ts=T0 + timedelta(days=1)))
    assert not engine.halted and events[-1].kind == "breaker_reset"


def test_drawdown_breaker_needs_manual_reset():
    engine, events, _ = make()
    engine.monitor(state(equity=84_000, day_start=84_500, peak=100_000))
    assert "drawdown" in engine.breakers
    engine.monitor(state(equity=90_000, day_start=90_000, peak=100_000, ts=T0 + timedelta(days=3)))
    assert engine.halted  # does not clear by itself
    engine.reset_breaker("drawdown", T0, actor="admin@example.com")
    assert not engine.halted and events[-1].detail == {"breaker": "drawdown", "actor": "admin@example.com"}


def test_emergency_policy_is_only_what_the_user_configured():
    for policy, expected in (("block_new_risk", []), ("cancel_open_orders", [EmergencyAction.CANCEL_OPEN_ORDERS]),
                             ("flatten_positions", [EmergencyAction.CANCEL_OPEN_ORDERS, EmergencyAction.FLATTEN_POSITIONS])):
        engine, _, _ = make(RiskLimits(emergency_policy=policy))
        assert engine.monitor(state(equity=80_000, day_start=100_000)) == expected
        assert engine.monitor(state(equity=79_000, day_start=100_000)) == []  # actions fire once, on the transition
        assert engine.engage_kill_switch(T0, "manual", "admin") == expected


def test_kill_switch_and_unreconciled_books_block_trading():
    engine, events, _ = make()
    engine.engage_kill_switch(T0, "operator pressed the button", "admin@example.com")
    d = engine.evaluate(intent(), state())
    assert not d.approved and "kill switch" in d.reasons[0]
    assert engine.evaluate(intent(side=Side.SELL, reduce_only=True, stop=None), state(exposures=[exp(5_000)])).approved
    engine.release_kill_switch(T0, "admin@example.com")
    assert engine.evaluate(intent(), state()).approved
    engine.monitor(state(reconciled=False))
    assert "reconciliation" in engine.breakers and not engine.evaluate(intent(), state()).approved
    assert not engine.evaluate(intent(), state(equity=0, day_start=1, peak=1)).approved


def test_limits_are_read_live_from_user_configuration():
    limits = RiskLimits(risk_per_trade=0.01)
    engine = RiskEngine(lambda: limits)
    assert not engine.evaluate(intent(qty=30, stop=50), state()).approved
    limits = RiskLimits(risk_per_trade=0.02)
    assert engine.evaluate(intent(qty=30, stop=50), state()).approved


def test_risk_engine_does_not_depend_on_ai_or_strategy_code():
    code = ("import sys, app.risk.engine, app.risk.sizing; "
            "bad=[m for m in sys.modules if m.startswith(('app.quant_ai','app.machine_learning','app.strategies','sklearn','torch','xgboost'))]; "
            "print(bad); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.brokers.base import OrderRejected
from app.domain.fees import CommissionModel
from app.domain.models import Order, OrderStatus, OrderType, Side
from app.instruments.registry import InstrumentRegistry, default_instruments
from app.market_data.fx import FxRateProvider, FxRateUnavailable
from app.paper_trading.broker import ExecutionModel, SimulatedBroker

T0 = datetime(2024, 3, 5, 15, tzinfo=timezone.utc)  # Tuesday, inside every session
H = pd.Timedelta(hours=1)
AAPL, BTC, ES = "STOCK:AAPL", "CRYPTO:BTC/USDT", "FUTURE:ES"
FREE = dict(spread_bps=0, slippage_bps=0, participation_rate=None,
            commissions={k: CommissionModel() for k in ("crypto", "stock", "etf", "forex", "future")})


def broker(cash=100_000.0, currency="USD", fx=None, **model):
    reg = InstrumentRegistry()
    for inst in default_instruments():
        reg.register(inst)
    b = SimulatedBroker("paper", reg, fx or FxRateProvider(pegs={"USDT": "USD"}), cash, currency, ExecutionModel(**{**FREE, **model}))
    b.connect()
    b.map_all_instruments()
    return b


def order(side=Side.BUY, qty=10, inst=AAPL, ts=T0 - timedelta(minutes=1), **kw):
    return Order(broker_id="paper", instrument_id=inst, side=side, qty=qty, created_at=ts, **kw)


def bar(b, i, o, h, l, c, v=1e9, inst=AAPL):
    ts = T0 + i * timedelta(hours=1)
    fills = b.process_open(inst, ts, o, v, H)
    return fills + b.process_intrabar(inst, ts, h, l, c, H)


def test_market_order_fills_at_next_open_with_costs_and_never_on_the_signal_bar():
    b = broker(spread_bps=10, slippage_bps=5, commissions={**FREE["commissions"], "stock": CommissionModel(per_unit=0.01, minimum=1)})
    late = b.submit_order(order(ts=T0 + timedelta(minutes=30)))  # created during bar 0
    assert bar(b, 0, 100, 101, 99, 100.5) == []  # cannot fill in a bar that opened before the order existed
    fills = bar(b, 1, 102, 103, 101, 102)
    assert len(fills) == 1 and fills[0].price == pytest.approx(102 * 1.001) and fills[0].commission == 1.0
    assert b.get_order_status(late).status is OrderStatus.FILLED and b.get_order_status(late).is_simulated
    bal = b.get_account_balance()
    assert bal.cash == pytest.approx(100_000 - 1.0)
    assert bal.equity == pytest.approx(100_000 - 1.0 + 10 * (102 - 102 * 1.001))
    assert b.costs["spread_slippage"] == pytest.approx(10 * 102 * 0.001)


def test_round_trip_pnl_long_and_short():
    b = broker()
    b.submit_order(order(qty=10))
    bar(b, 0, 100, 105, 99, 104)
    b.submit_order(order(Side.SELL, qty=25, ts=T0))  # close 10 and go short 15
    bar(b, 1, 110, 111, 108, 109)
    assert b.cash == pytest.approx(100_000 + 10 * (110 - 100))
    assert b.positions[(AAPL, None)] == [-15, 110]
    assert b.get_account_balance().equity == pytest.approx(100_100 + 15 * (110 - 109))
    b.submit_order(order(Side.BUY, qty=15, ts=T0 + timedelta(hours=1), reduce_only=True))
    bar(b, 2, 120, 121, 119, 120)
    assert b.cash == pytest.approx(100_100 - 15 * 10) and b.positions == {}


def test_stop_order_fills_at_stop_or_at_gap_open():
    b = broker()
    b.submit_order(order(qty=10))
    bar(b, 0, 100, 101, 99, 100)
    stop = b.submit_order(order(Side.SELL, qty=10, type=OrderType.STOP, stop_price=95.0, reduce_only=True, ts=T0))
    assert bar(b, 1, 100, 101, 96, 97) == []  # never traded at 95
    f = bar(b, 2, 97, 98, 94, 96)
    assert f[0].price == 95.0 and b.get_order_status(stop).status is OrderStatus.FILLED

    b2 = broker()
    b2.submit_order(order(qty=10))
    bar(b2, 0, 100, 101, 99, 100)
    b2.submit_order(order(Side.SELL, qty=10, type=OrderType.STOP, stop_price=95.0, reduce_only=True, ts=T0))
    assert bar(b2, 1, 90, 92, 89, 91)[0].price == 90.0  # gapped below the stop: filled at the worse open, not at 95


def test_limit_requires_trade_through_and_stop_beats_target_in_same_bar():
    b = broker()
    lim = b.submit_order(order(qty=10, type=OrderType.LIMIT, limit_price=99.0))
    assert bar(b, 0, 100, 101, 99.0, 100) == []  # touched, not traded through
    assert bar(b, 1, 100, 101, 98.9, 100)[0].price == 99.0
    assert b.get_order_status(lim).status is OrderStatus.FILLED
    stop = b.submit_order(order(Side.SELL, qty=10, type=OrderType.STOP, stop_price=97.0, reduce_only=True, ts=T0 + timedelta(hours=1)))
    target = b.submit_order(order(Side.SELL, qty=10, type=OrderType.LIMIT, limit_price=103.0, reduce_only=True, ts=T0 + timedelta(hours=1)))
    fills = bar(b, 2, 100, 104, 96, 100)  # both levels inside the bar
    assert [f.price for f in fills] == [97.0]
    assert b.get_order_status(stop).status is OrderStatus.FILLED
    assert b.get_order_status(target).status is OrderStatus.CANCELLED and b.positions == {}
    better = b.submit_order(order(qty=5, type=OrderType.LIMIT, limit_price=99.0, ts=T0 + timedelta(hours=2)))
    assert bar(b, 3, 98, 99, 97, 98)[0].price == 98.0  # opened below the buy limit -> filled at the open
    assert b.get_order_status(better).avg_fill_price == 98.0


def test_partial_fills_respect_bar_volume():
    b = broker(participation_rate=0.1)
    o = b.submit_order(order(qty=250))
    f1 = bar(b, 0, 100, 101, 99, 100, v=1000)  # at most 100 shares
    st = b.get_order_status(o)
    assert f1[0].qty == 100 and st.status is OrderStatus.PARTIALLY_FILLED and st.remaining == 150
    f2 = bar(b, 1, 102, 103, 101, 102, v=1000)
    f3 = bar(b, 2, 104, 105, 103, 104, v=1000)
    st = b.get_order_status(o)
    assert (f2[0].qty, f3[0].qty) == (100, 50) and st.status is OrderStatus.FILLED
    assert st.avg_fill_price == pytest.approx((100 * 100 + 100 * 102 + 50 * 104) / 250)
    assert b.positions[(AAPL, None)][0] == 250
    tiny = b.submit_order(order(qty=10, ts=T0 + timedelta(hours=2)))
    assert bar(b, 3, 104, 105, 103, 104, v=5) == [] and b.get_order_status(tiny).is_open  # 0.5 share is below the lot


def test_orders_do_not_fill_outside_the_trading_session():
    b = broker()
    o = b.submit_order(order(qty=10))
    night = T0.replace(hour=3)  # 22:00 New York
    assert b.process_open(AAPL, night, 100, 1e9, H) == [] and b.process_intrabar(AAPL, night, 101, 99, 100, H) == []
    assert b.get_order_status(o).is_open
    assert len(b.process_open(BTC, night, 60_000, 1e9, H)) == 0  # no BTC order
    b.submit_order(order(qty=0.01, inst=BTC, ts=night - timedelta(minutes=1)))
    assert len(b.process_open(BTC, night + timedelta(hours=1), 60_000, 1e9, H)) == 1  # crypto trades around the clock


def test_margin_reduce_only_and_idempotency_constraints():
    b = broker(cash=10_000)
    big = b.submit_order(order(qty=250))  # 25,000 notional * 50% margin > 10,000 equity
    assert bar(b, 0, 100, 101, 99, 100) == []
    assert b.get_order_status(big).status is OrderStatus.REJECTED and b.get_order_status(big).reason == "insufficient margin"
    with pytest.raises(OrderRejected):
        b.submit_order(order(Side.SELL, qty=1, reduce_only=True))
    o = order(qty=10, ts=T0)
    first, again = b.submit_order(o), b.submit_order(o)
    assert first.id == again.id and len(b.get_open_orders()) == 1
    assert b.cancel_order(first).status is OrderStatus.CANCELLED
    assert bar(b, 1, 100, 101, 99, 100) == []


def test_futures_multiplier_financing_and_roll_costs():
    b = broker(cash=1_000_000, financing_long_annual=0.0, financing_short_annual=0.10,
               commissions={**FREE["commissions"], "future": CommissionModel(per_unit=2.0)}, spread_bps=2)
    b.submit_order(order(Side.SELL, qty=2, inst=ES))
    ts = datetime(2024, 3, 5, 15, tzinfo=timezone.utc)
    b.process_open(ES, ts, 5000, 1e9, pd.Timedelta(days=1))
    b.process_intrabar(ES, ts, 5010, 4990, 5000, pd.Timedelta(days=1))
    entry = 5000 * (1 - 0.0001)
    cash = 1_000_000 - 4.0
    assert b.cash == pytest.approx(cash) and b.positions[(ES, None)] == [-2, pytest.approx(entry)]
    b.process_open(ES, ts + timedelta(days=1), 4900, 1e9, pd.Timedelta(days=1))
    financing = 2 * 5000 * 50 * 0.10 / 365
    assert b.cash == pytest.approx(cash - financing) and b.costs["financing"] == pytest.approx(financing)
    assert b.get_account_balance().equity == pytest.approx(cash - financing + 2 * 50 * (entry - 4900))
    roll = b.charge_roll(ES, ts + timedelta(days=1))
    assert roll == pytest.approx(2 * 4.0 + 2 * 4900 * 50 * 0.0002)
    assert b.charge_roll(AAPL, ts) == 0.0


def test_multi_currency_account_uses_timestamped_fx_and_conversion_costs():
    fx = FxRateProvider(conversion_cost_bps=10)
    fx.add_rate("EUR", "USD", T0 - timedelta(hours=1), 1.25, "test")
    fx.add_rate("EUR", "USD", T0 + timedelta(hours=1, minutes=-1), 1.00, "test")  # USD strengthens before bar 1
    b = broker(cash=100_000, currency="EUR", fx=fx)
    b.submit_order(order(qty=100))
    bar(b, 0, 100, 101, 99, 100)
    b.submit_order(order(Side.SELL, qty=100, ts=T0, reduce_only=True))
    bar(b, 1, 110, 111, 109, 110)
    assert b.cash == pytest.approx(100_000 + 1000 * 1.00 * (1 - 0.001))  # realised at the rate in force at the exit
    with pytest.raises(FxRateUnavailable):
        late = broker(cash=1000, currency="EUR", fx=FxRateProvider())
        late.submit_order(order(qty=1))
        bar(late, 0, 100, 101, 99, 100)


def test_state_roundtrip_and_reset():
    b = broker()
    b.submit_order(order(qty=10))
    bar(b, 0, 100, 101, 99, 100)
    working = b.submit_order(order(Side.SELL, qty=10, type=OrderType.STOP, stop_price=90.0, reduce_only=True, ts=T0))
    clone = broker()
    clone.load_state(b.to_state())
    assert clone.to_state() == b.to_state() and clone.get_order_status(working).is_open
    assert bar(clone, 1, 89, 90, 88, 89)[0].price == 89
    b.reset(50_000)
    assert b.cash == 50_000 and not b.positions and not b.orders
    with pytest.raises(ValueError):
        b.reset(0)


def test_two_strategies_with_opposite_positions_keep_separate_books():
    b = broker()
    b.submit_order(order(qty=10, strategy="trend"))
    b.submit_order(order(Side.SELL, qty=10, strategy="reversion"))
    bar(b, 0, 100, 101, 99, 100)
    assert b.net_qty(AAPL) == 0 and b.get_positions() == []  # flat on a net basis, as a netting broker would report
    assert b.get_account_balance().margin_used == pytest.approx(2 * 10 * 100 * 0.5)  # margin is charged on both legs
    stop = b.submit_order(order(Side.SELL, qty=10, type=OrderType.STOP, stop_price=95.0, reduce_only=True, strategy="trend", ts=T0))
    fills = bar(b, 1, 100, 101, 94, 96)
    assert fills[0].price == 95.0 and b.get_order_status(stop).status is OrderStatus.FILLED  # the long's stop still works
    assert b.positions == {(AAPL, "reversion"): [-10, 100]}
    assert b.cash == pytest.approx(100_000 - 50) and b.get_account_balance().equity == pytest.approx(100_000 - 50 + 40)
    with pytest.raises(OrderRejected):
        b.submit_order(order(Side.SELL, qty=5, reduce_only=True, strategy="trend", ts=T0))  # nothing left in that book

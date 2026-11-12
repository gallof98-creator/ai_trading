import ccxt
import pytest

from app.brokers.base import (BrokerError, BrokerManager, ConnectionLost, OrderRejected, RateLimited, UnsupportedOperation)
from app.brokers.ccxt_adapter import CcxtAdapter
from app.brokers.ibkr_adapter import IbkrAdapter
from app.core.config import LIVE_CONFIRMATION_PHRASE, Settings
from app.domain.models import AssetClass, Order, OrderStatus, OrderType, Side
from app.instruments.registry import InstrumentRegistry, default_instruments
from app.live_trading.gate import LiveAuthorization, LiveTradingNotAuthorized, authorize_live_trading
from app.market_data.quality import DataKind
from tests.execution.fakes import FakeCcxt, FakeIB

BTC, ETH, AAPL, ES, EURUSD = "CRYPTO:BTC/USDT", "CRYPTO:ETH/USDT", "STOCK:AAPL", "FUTURE:ES", "FOREX:EUR/USD"


@pytest.fixture
def registry():
    reg = InstrumentRegistry()
    for inst in default_instruments():
        reg.register(inst)
    return reg


def order(instrument=BTC, side=Side.BUY, qty=0.01, broker="x", **kw):
    return Order(broker_id=broker, instrument_id=instrument, side=side, qty=qty, **kw)


def ccxt_adapter(registry, **kw):
    ex = FakeCcxt(**kw)
    adapter = CcxtAdapter("x", registry, ex)
    adapter.connect()
    return adapter, ex


# ---------- capability detection ----------
def test_ccxt_capabilities_are_detected_not_assumed(registry):
    spot, ex = ccxt_adapter(registry)
    caps = spot.capabilities
    assert ex.sandbox is True  # no live authorization -> testnet
    assert caps.asset_classes == {AssetClass.CRYPTO} and caps.order_types == {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}
    assert not caps.supports_short and not caps.supports_reduce_only
    assert caps.timeframes == {"1m", "1h"}  # exchange-only "3d" is not a platform timeframe
    swap, _ = ccxt_adapter(registry, default_type="swap")
    assert swap.capabilities.supports_short and swap.capabilities.supports_reduce_only


def test_unsupported_orders_are_refused_before_reaching_the_exchange(registry):
    adapter, ex = ccxt_adapter(registry)
    mgr = BrokerManager()
    mgr.register(adapter)
    cases = {
        "short selling": order(side=Side.SELL),
        "stop_limit not supported": order(type=OrderType.STOP_LIMIT, limit_price=1, stop_price=1),
        "limit price missing": order(type=OrderType.LIMIT),
        "asset class stock": order(instrument=AAPL, qty=1),
        "not active": order(instrument=ETH, qty=1),
        "below exchange minimum 0.001": order(qty=0.0005),
        "order value below": order(qty=0.001, type=OrderType.LIMIT, limit_price=100.0),
        "lot size": order(qty=0.0100001),
        "unknown instrument": order(instrument="CRYPTO:NOPE/USDT"),
    }
    for expected, o in cases.items():
        with pytest.raises(UnsupportedOperation, match=expected):
            mgr.submit(o)
    assert ex.create_calls == 0
    assert mgr.submit(order()).status is OrderStatus.SUBMITTED and ex.create_calls == 1
    with pytest.raises(BrokerError, match="Unknown broker"):
        mgr.submit(order(broker="nope"))


def test_exchange_without_sandbox_or_without_keys_is_data_only(registry):
    for kw in ({"sandbox": False}, {"api_key": None}):
        adapter, ex = ccxt_adapter(registry, **kw)
        assert adapter.capabilities.trading_enabled is False
        assert "market data only" in "; ".join(adapter.unsupported_reasons(order()))
        with pytest.raises(UnsupportedOperation):
            adapter.submit_order(order())
        assert ex.create_calls == 0
        assert len(adapter.get_historical_data(BTC, "1h").bars) == 4  # data still works


# ---------- CCXT behaviour ----------
def test_ccxt_order_lifecycle_partial_fill_and_cancel(registry):
    adapter, ex = ccxt_adapter(registry)
    o = adapter.submit_order(order(qty=0.01))
    assert o.broker_order_id == "1" and o.is_simulated is False and ex.orders["1"]["clientOrderId"] == o.client_order_id
    ex.fill("1", 0.004, 30000.0, fee=0.12)
    o = adapter.get_order_status(o)
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == 0.004 and o.remaining == pytest.approx(0.006) and o.commission == 0.12
    assert [x.client_order_id for x in adapter.get_open_orders()] == [o.client_order_id]
    o = adapter.cancel_order(o)
    assert o.status is OrderStatus.CANCELLED and o.filled_qty == 0.004
    assert adapter.cancel_order(o).status is OrderStatus.CANCELLED  # cancelling again is harmless
    stop = adapter.submit_order(order(type=OrderType.STOP, stop_price=25000.0, side=Side.BUY))
    assert ex.orders[stop.broker_order_id]["params"]["triggerPrice"] == 25000.0


def test_ccxt_errors_are_translated(registry):
    adapter, ex = ccxt_adapter(registry)
    for raised, expected in ((ccxt.RequestTimeout("t"), ConnectionLost), (ccxt.ExchangeNotAvailable("x"), ConnectionLost),
                             (ccxt.RateLimitExceeded("429"), RateLimited), (ccxt.InsufficientFunds("no"), OrderRejected),
                             (ccxt.InvalidOrder("bad"), OrderRejected), (ccxt.AuthenticationError("key"), BrokerError)):
        ex.fail_next = [raised]
        with pytest.raises(expected):
            adapter.submit_order(order())
    assert not ex.orders


def test_lost_response_can_be_reconciled_by_client_id_and_resubmit_cannot_duplicate(registry):
    adapter, ex = ccxt_adapter(registry)
    o = order()
    ex.fail_after_accept = ccxt.RequestTimeout("response lost")
    with pytest.raises(ConnectionLost):
        adapter.submit_order(o)
    found = adapter.find_order(o.client_order_id, BTC)  # the order DID reach the exchange
    assert found is not None and found.broker_order_id == "1"
    with pytest.raises(OrderRejected, match="duplicate"):
        adapter.submit_order(o)  # same client id -> the exchange refuses a second order
    assert len(ex.orders) == 1
    assert adapter.find_order("never-sent", BTC) is None


def test_ccxt_account_positions_quotes_and_incomplete_bar(registry):
    adapter, ex = ccxt_adapter(registry)
    bal = adapter.get_account_balance()
    assert bal.cash == 1000 and bal.equity == pytest.approx(1000 + 0.5 * 30000)
    pos = adapter.get_positions()
    assert [(p.instrument_id, p.qty, p.avg_price) for p in pos] == [(BTC, 0.5, None)]
    q = adapter.get_market_data(BTC)
    assert (q.bid, q.ask, q.kind) == (29990.0, 30010.0, "realtime")
    ds = adapter.get_historical_data(BTC, "1h")
    assert ds.kind is DataKind.HISTORICAL and len(ds.bars) == 4 and str(ds.bars.index.tz) == "UTC"  # forming bar dropped
    with pytest.raises(UnsupportedOperation):
        adapter.get_historical_data(BTC, "4h")


# ---------- live gating ----------
def test_live_authorization_only_comes_from_explicit_deployment_settings(registry):
    with pytest.raises(LiveTradingNotAuthorized):
        authorize_live_trading(Settings(_env_file=None))
    with pytest.raises(LiveTradingNotAuthorized):
        authorize_live_trading(Settings(_env_file=None, live_trading_enabled=True, live_trading_confirmation="yes"))
    forged = LiveAuthorization("production", object())
    with pytest.raises(LiveTradingNotAuthorized):
        CcxtAdapter("x", registry, FakeCcxt(), authorization=forged).connect()

    auth = authorize_live_trading(Settings(_env_file=None, live_trading_enabled=True, live_trading_confirmation=LIVE_CONFIRMATION_PHRASE))
    ex = FakeCcxt(withdrawals=False)
    live = CcxtAdapter("x", registry, ex, authorization=auth)
    live.connect()
    assert live.is_live and ex.sandbox is False
    with pytest.raises(BrokerError, match="withdrawal permission"):
        CcxtAdapter("y", registry, FakeCcxt(withdrawals=True), authorization=auth).connect()


# ---------- IBKR (mocked) ----------
def ibkr(registry, **kw):
    ib = FakeIB(**kw)
    adapter = IbkrAdapter("ib", registry, ib=ib)
    for iid, sym in ((AAPL, "AAPL"), (EURUSD, "EUR/USD")):
        registry.map_symbol("ib", iid, sym)
    adapter.connect()
    return adapter, ib


def test_ibkr_refuses_live_account_without_authorization(registry):
    ib = FakeIB(accounts=("U999",))
    with pytest.raises(BrokerError, match="LIVE IBKR account"):
        IbkrAdapter("ib", registry, ib=ib).connect()
    assert ib.connected is False


def test_ibkr_capabilities_and_contract_checks(registry):
    adapter, ib = ibkr(registry)
    assert AssetClass.CRYPTO not in adapter.capabilities.asset_classes
    assert "asset class crypto" in "; ".join(adapter.unsupported_reasons(order(broker="ib")))
    assert adapter.unsupported_reasons(order(instrument=AAPL, qty=10, broker="ib")) == []
    assert adapter.unsupported_reasons(order(instrument=AAPL, qty=10, side=Side.SELL, broker="ib")) == []  # shorting allowed
    assert "no symbol mapping" in "; ".join(adapter.unsupported_reasons(order(instrument=ES, qty=1, broker="ib")))
    registry.map_symbol("ib", ES, "ES")
    assert "needs an expiry" in "; ".join(adapter.unsupported_reasons(order(instrument=ES, qty=1, broker="ib")))
    registry.map_symbol("ib", "ETF:SPY", "BOGUS")
    assert "does not recognise" in "; ".join(adapter.unsupported_reasons(order(instrument="ETF:SPY", qty=1, broker="ib")))


def test_ibkr_order_lifecycle_idempotency_and_account(registry):
    adapter, ib = ibkr(registry)
    o = order(instrument=AAPL, qty=10, broker="ib", type=OrderType.LIMIT, limit_price=190.0)
    placed = adapter.submit_order(o)
    assert placed.status is OrderStatus.SUBMITTED and ib.trades()[0].order.orderRef == o.client_order_id
    assert adapter.submit_order(o).broker_order_id == placed.broker_order_id and len(ib.trades()) == 1  # idempotent resubmit
    ib.fill(ib.trades()[0], 4, 189.5, commission=1.0)
    st = adapter.get_order_status(o)
    assert st.status is OrderStatus.PARTIALLY_FILLED and st.filled_qty == 4 and st.commission == 1.0
    assert adapter.find_order(o.client_order_id, AAPL).filled_qty == 4
    assert [(p.instrument_id, p.qty, p.avg_price) for p in adapter.get_positions()] == [(AAPL, 4.0, 189.5)]
    assert adapter.cancel_order(o).status is OrderStatus.CANCELLED
    bal = adapter.get_account_balance()
    assert (bal.equity, bal.cash, bal.margin_used, bal.currency) == (50000.0, 20000.0, 5000.0, "EUR")


def test_ibkr_rejection_and_connection_loss(registry):
    adapter, ib = ibkr(registry)
    ib.reject = True
    with pytest.raises(OrderRejected, match="margin"):
        adapter.submit_order(order(instrument=AAPL, qty=10, broker="ib"))
    ib.reject, ib.fail_place = False, ConnectionError("socket closed")
    with pytest.raises(ConnectionLost):
        adapter.submit_order(order(instrument=AAPL, qty=10, broker="ib"))
    assert adapter.is_connected() is False
    assert "not connected" in "; ".join(adapter.unsupported_reasons(order(instrument=AAPL, qty=10, broker="ib")))

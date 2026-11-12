"""Interactive Brokers adapter (stocks, ETFs, forex, futures) on top of ib_async.

VERIFICATION STATUS: exercised only against a mock of the ib_async client. It has not been run
against TWS / IB Gateway and must be validated on an IBKR paper account before any use.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from ..domain.models import AccountBalance, AssetClass, Order, OrderStatus, OrderType, Position, Quote, Side
from ..instruments.registry import InstrumentRegistry
from ..live_trading.gate import LiveAuthorization, require_valid
from ..market_data.quality import DataKind
from ..market_data.sources import Dataset
from ..market_data.timeframes import OHLCV, TIMEFRAMES
from .base import BrokerAdapter, BrokerCapabilities, BrokerError, ConnectionLost, OrderRejected

_BAR_SIZE = {"1m": ("1 min", "2 D"), "5m": ("5 mins", "10 D"), "15m": ("15 mins", "20 D"), "30m": ("30 mins", "30 D"),
             "1h": ("1 hour", "60 D"), "4h": ("4 hours", "180 D"), "1d": ("1 day", "5 Y"), "1w": ("1 week", "10 Y")}
_STATUS = {"PendingSubmit": OrderStatus.SUBMITTED, "PreSubmitted": OrderStatus.SUBMITTED, "Submitted": OrderStatus.SUBMITTED,
           "ApiPending": OrderStatus.SUBMITTED, "Filled": OrderStatus.FILLED, "Cancelled": OrderStatus.CANCELLED,
           "ApiCancelled": OrderStatus.CANCELLED, "PendingCancel": OrderStatus.SUBMITTED, "Inactive": OrderStatus.REJECTED}
_CAPS = BrokerCapabilities(
    asset_classes=frozenset({AssetClass.STOCK, AssetClass.ETF, AssetClass.FOREX, AssetClass.FUTURE}),
    order_types=frozenset(OrderType), supports_short=True, supports_reduce_only=False, supports_historical_data=True,
    supports_bid_ask=True, timeframes=frozenset(_BAR_SIZE), is_simulated=False)


def _net(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (ConnectionError, TimeoutError, OSError) as exc:
            self._connected = False
            raise ConnectionLost(str(exc)) from exc

    return wrapper


class IbkrAdapter(BrokerAdapter):
    def __init__(self, broker_id: str, registry: InstrumentRegistry, ib=None, host: str = "127.0.0.1", port: int = 7497,
                 client_id: int = 1, account_currency: str = "EUR", authorization: LiveAuthorization | None = None) -> None:
        super().__init__(broker_id, registry)
        if ib is None:
            from ib_async import IB

            ib = IB()
        self.ib, self.host, self.port, self.client_id = ib, host, port, client_id
        self.account_currency = account_currency
        self._authorization = authorization
        self._connected = False
        self._contracts: dict[str, object] = {}

    @_net
    def connect(self) -> None:
        if self._authorization is not None:
            require_valid(self._authorization)
        self.ib.connect(self.host, self.port, clientId=self.client_id)
        accounts = self.ib.managedAccounts()
        # IBKR paper accounts are prefixed "D" (e.g. DU1234567). Anything else is real money.
        if self._authorization is None and any(not a.startswith("D") for a in accounts):
            self.ib.disconnect()
            raise BrokerError("Connected gateway serves a LIVE IBKR account but live trading is not authorized; connection closed")
        self._connected = True

    def disconnect(self) -> None:
        self.ib.disconnect()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    @property
    def capabilities(self) -> BrokerCapabilities:
        return _CAPS

    def _contract(self, instrument_id: str):
        if instrument_id in self._contracts:
            return self._contracts[instrument_id]
        from ib_async import Forex, Future, Stock

        inst = self.registry.get(instrument_id)
        symbol = self.registry.to_broker(self.broker_id, instrument_id)
        if inst.asset_class in (AssetClass.STOCK, AssetClass.ETF):
            contract = Stock(symbol, "SMART", inst.quote_currency, primaryExchange=inst.exchange or "")
        elif inst.asset_class is AssetClass.FOREX:
            contract = Forex(symbol.replace("/", ""))
        elif inst.asset_class is AssetClass.FUTURE:
            if inst.expiry is None:
                raise BrokerError(f"{instrument_id} needs an expiry to be traded at IBKR")
            contract = Future(symbol, inst.expiry.strftime("%Y%m"), inst.exchange or "", currency=inst.quote_currency)
        else:
            raise BrokerError(f"{inst.asset_class.value} is not supported at IBKR by this adapter")
        if not self.ib.qualifyContracts(contract):
            raise BrokerError(f"IBKR does not recognise {instrument_id} ({symbol})")
        self._contracts[instrument_id] = contract
        return contract

    def instrument_reasons(self, order: Order) -> list[str]:
        try:
            self._contract(order.instrument_id)
        except BrokerError as exc:
            return [str(exc)]
        return []

    @_net
    def get_account_balance(self) -> AccountBalance:
        tags = {v.tag: float(v.value) for v in self.ib.accountSummary()
                if v.tag in ("NetLiquidation", "TotalCashValue", "InitMarginReq") and v.currency == self.account_currency}
        if "NetLiquidation" not in tags:
            raise BrokerError(f"IBKR returned no account summary in {self.account_currency}")
        return AccountBalance(broker_id=self.broker_id, currency=self.account_currency, cash=tags.get("TotalCashValue", 0.0),
                              equity=tags["NetLiquidation"], margin_used=tags.get("InitMarginReq", 0.0))

    def _instrument_id(self, contract) -> str:
        for iid, known in self._contracts.items():
            if getattr(known, "conId", None) == getattr(contract, "conId", object()):
                return iid
        return f"UNMAPPED:{getattr(contract, 'localSymbol', '') or getattr(contract, 'symbol', '?')}"

    @_net
    def get_positions(self) -> list[Position]:
        out = []
        for p in self.ib.positions():
            if p.position:
                mult = float(getattr(p.contract, "multiplier", "") or 1)
                out.append(Position(broker_id=self.broker_id, instrument_id=self._instrument_id(p.contract), qty=float(p.position),
                                    avg_price=float(p.avgCost) / mult))
        return out

    @_net
    def get_market_data(self, instrument_id: str) -> Quote:
        t = self.ib.reqTickers(self._contract(instrument_id))[0]

        def clean(x):
            return float(x) if x is not None and x == x and x > 0 else None

        # Without a market-data subscription IBKR serves delayed quotes; label them as such.
        kind = DataKind.DELAYED if getattr(t, "marketDataType", 1) in (3, 4) else DataKind.REALTIME
        return Quote(instrument_id=instrument_id, ts=getattr(t, "time", None) or datetime.now(timezone.utc), bid=clean(t.bid),
                     ask=clean(t.ask), last=clean(t.last), kind=kind.value)

    @_net
    def get_historical_data(self, instrument_id: str, timeframe: str, limit: int = 500) -> Dataset:
        inst = self.registry.get(instrument_id)
        size, duration = _BAR_SIZE[timeframe]
        rows = self.ib.reqHistoricalData(self._contract(instrument_id), endDateTime="", durationStr=duration, barSizeSetting=size,
                                         whatToShow="MIDPOINT" if inst.asset_class is AssetClass.FOREX else "TRADES",
                                         useRTH=True, formatDate=2)
        idx = pd.DatetimeIndex([pd.Timestamp(r.date) for r in rows])
        idx = (idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")).rename("ts")
        bars = pd.DataFrame([[r.open, r.high, r.low, r.close, max(r.volume, 0)] for r in rows], index=idx, columns=OHLCV, dtype=float)
        bars = bars[bars.index + TIMEFRAMES[timeframe] <= pd.Timestamp.now(tz="UTC")].iloc[-limit:]
        return Dataset(bars, DataKind.HISTORICAL, "ibkr")

    def _parse(self, trade, order: Order) -> Order:
        st = trade.orderStatus
        status = _STATUS.get(st.status, OrderStatus.SUBMITTED)
        filled = float(st.filled or 0)
        if status is OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        commission = sum(float(getattr(f.commissionReport, "commission", 0) or 0) for f in trade.fills)
        return order.model_copy(update={"broker_order_id": str(trade.order.orderId), "status": status, "filled_qty": filled,
                                        "avg_fill_price": float(st.avgFillPrice) or None, "commission": commission,
                                        "is_simulated": False, "updated_at": datetime.now(timezone.utc)})

    def _trade(self, client_order_id: str):
        return next((t for t in self.ib.trades() if t.order.orderRef == client_order_id), None)

    @_net
    def submit_order(self, order: Order) -> Order:
        from ib_async import LimitOrder, MarketOrder, StopLimitOrder, StopOrder

        existing = self._trade(order.client_order_id)
        if existing is not None:  # idempotent: this client id was already placed in this session
            return self._parse(existing, order)
        action = order.side.value.upper()
        kw = {"orderRef": order.client_order_id, "tif": "GTC"}
        if order.type is OrderType.MARKET:
            ib_order = MarketOrder(action, order.qty, **kw)
        elif order.type is OrderType.LIMIT:
            ib_order = LimitOrder(action, order.qty, order.limit_price, **kw)
        elif order.type is OrderType.STOP:
            ib_order = StopOrder(action, order.qty, order.stop_price, **kw)
        else:
            ib_order = StopLimitOrder(action, order.qty, order.limit_price, order.stop_price, **kw)
        trade = self.ib.placeOrder(self._contract(order.instrument_id), ib_order)
        parsed = self._parse(trade, order)
        if parsed.status is OrderStatus.REJECTED:
            raise OrderRejected("; ".join(e.message for e in trade.log if e.message) or "order rejected by IBKR")
        return parsed

    @_net
    def cancel_order(self, order: Order) -> Order:
        trade = self._trade(order.client_order_id)
        if trade is None:
            raise BrokerError(f"Order {order.client_order_id} is not known to this IBKR session")
        if _STATUS.get(trade.orderStatus.status) is OrderStatus.SUBMITTED:
            self.ib.cancelOrder(trade.order)
        return self._parse(trade, order)

    @_net
    def get_order_status(self, order: Order) -> Order:
        trade = self._trade(order.client_order_id)
        if trade is None:
            raise BrokerError(f"Order {order.client_order_id} is not known to this IBKR session")
        return self._parse(trade, order)

    def _from_trade(self, trade) -> Order:
        o = trade.order
        kind = {"MKT": OrderType.MARKET, "LMT": OrderType.LIMIT, "STP": OrderType.STOP, "STP LMT": OrderType.STOP_LIMIT}.get(o.orderType, OrderType.MARKET)
        base = Order(broker_id=self.broker_id, instrument_id=self._instrument_id(trade.contract), side=Side(o.action.lower()),
                     qty=float(o.totalQuantity), type=kind, client_order_id=o.orderRef or f"ib-{o.orderId}",
                     limit_price=o.lmtPrice if kind in (OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
                     stop_price=o.auxPrice if kind in (OrderType.STOP, OrderType.STOP_LIMIT) else None)
        return self._parse(trade, base)

    @_net
    def get_open_orders(self) -> list[Order]:
        return [self._from_trade(t) for t in self.ib.openTrades()]

    @_net
    def find_order(self, client_order_id: str, instrument_id: str) -> Order | None:
        trade = self._trade(client_order_id)
        return self._from_trade(trade).model_copy(update={"instrument_id": instrument_id}) if trade else None

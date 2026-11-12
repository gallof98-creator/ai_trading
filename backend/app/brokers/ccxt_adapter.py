"""CCXT adapter (Binance, Bybit and other CCXT exchanges).

Without a live authorization the adapter switches the exchange to its sandbox/testnet; if the
exchange has none, the connection is market-data only. Real endpoints are used for trading
only when a LiveAuthorization is supplied.
"""
from __future__ import annotations

import functools
from datetime import datetime, timezone

import ccxt
import pandas as pd

from ..domain.models import AccountBalance, AssetClass, Order, OrderStatus, OrderType, Position, Quote, Side
from ..instruments.registry import InstrumentRegistry
from ..live_trading.gate import LiveAuthorization, require_valid
from ..market_data.quality import DataKind
from ..market_data.sources import Dataset
from ..market_data.timeframes import OHLCV, TIMEFRAMES
from .base import (BrokerAdapter, BrokerCapabilities, BrokerError, ConnectionLost, OrderRejected, RateLimited,
                   UnsupportedOperation)

_STATUS = {"open": OrderStatus.SUBMITTED, "closed": OrderStatus.FILLED, "canceled": OrderStatus.CANCELLED,
           "cancelled": OrderStatus.CANCELLED, "expired": OrderStatus.CANCELLED, "rejected": OrderStatus.REJECTED}


def _translate(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as exc:
            raise RateLimited(str(exc)) from exc
        except ccxt.NetworkError as exc:  # includes timeouts and ExchangeNotAvailable
            raise ConnectionLost(str(exc)) from exc
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds) as exc:
            raise OrderRejected(str(exc)) from exc
        except ccxt.BaseError as exc:
            raise BrokerError(str(exc)) from exc

    return wrapper


class CcxtAdapter(BrokerAdapter):
    def __init__(self, broker_id: str, registry: InstrumentRegistry, exchange, account_currency: str = "USDT",
                 authorization: LiveAuthorization | None = None) -> None:
        super().__init__(broker_id, registry)
        self.exchange = exchange
        self.account_currency = account_currency
        self._authorization = authorization
        self._caps: BrokerCapabilities | None = None
        self._derivatives = False

    @property
    def is_live(self) -> bool:
        return self._authorization is not None

    @_translate
    def connect(self) -> None:
        trading_enabled = True
        if self._authorization is not None:
            require_valid(self._authorization)
            self._refuse_withdrawal_keys()
        else:
            try:
                self.exchange.set_sandbox_mode(True)
            except ccxt.NotSupported:
                trading_enabled = False  # no testnet: public market data only
        self.exchange.enableRateLimit = True
        self.exchange.load_markets()
        has = self.exchange.has
        self._derivatives = (self.exchange.options or {}).get("defaultType", "spot") in ("swap", "future")
        types = set()
        if has.get("createMarketOrder", True):
            types.add(OrderType.MARKET)
        if has.get("createLimitOrder", True):
            types.add(OrderType.LIMIT)
        if has.get("createStopMarketOrder") or has.get("createStopOrder"):
            types.add(OrderType.STOP)
        if has.get("createStopLimitOrder"):
            types.add(OrderType.STOP_LIMIT)
        if not has.get("createOrder", True) or not getattr(self.exchange, "apiKey", None):
            trading_enabled = False
        self._caps = BrokerCapabilities(
            asset_classes=frozenset({AssetClass.CRYPTO}), order_types=frozenset(types),
            supports_short=self._derivatives, supports_reduce_only=self._derivatives,
            supports_historical_data=bool(has.get("fetchOHLCV")), supports_bid_ask=bool(has.get("fetchTicker")),
            timeframes=frozenset(set(self.exchange.timeframes or {}) & set(TIMEFRAMES)),
            is_simulated=False, trading_enabled=trading_enabled,
        )
        # CCXT unified symbols are already normalized, so a registry symbol that the exchange lists is an exact match.
        for inst in self.registry.all():
            if inst.asset_class is AssetClass.CRYPTO and inst.symbol in self.exchange.markets and not self.registry.is_mapped(self.broker_id, inst.id):
                self.registry.map_symbol(self.broker_id, inst.id, inst.symbol)

    def _refuse_withdrawal_keys(self) -> None:
        """Least privilege: where the exchange exposes key permissions (Binance), refuse keys that can withdraw."""
        probe = getattr(self.exchange, "sapiGetAccountApiRestrictions", None)
        if probe is None:
            return
        restrictions = probe()
        if str(restrictions.get("enableWithdrawals", False)).lower() == "true":
            raise BrokerError("This API key has withdrawal permission. Create a key limited to reading and trading.")

    def disconnect(self) -> None:
        self._caps = None

    def is_connected(self) -> bool:
        return self._caps is not None

    @property
    def capabilities(self) -> BrokerCapabilities:
        if self._caps is None:
            raise BrokerError(f"{self.broker_id} is not connected")
        return self._caps

    def _symbol(self, instrument_id: str) -> str:
        return self.registry.to_broker(self.broker_id, instrument_id)

    def instrument_reasons(self, order: Order) -> list[str]:
        market = self.exchange.markets.get(self._symbol(order.instrument_id))
        if market is None:
            return ["symbol not listed on exchange"]
        reasons = []
        if market.get("active") is False:
            reasons.append("market is not active")
        min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
        if min_amount and order.qty < min_amount:
            reasons.append(f"quantity below exchange minimum {min_amount}")
        min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min")
        ref_price = order.limit_price or order.stop_price
        if min_cost and ref_price and order.qty * ref_price < min_cost:
            reasons.append(f"order value below exchange minimum {min_cost}")
        return reasons

    # ---- account / data ----
    @_translate
    def get_account_balance(self) -> AccountBalance:
        bal = self.exchange.fetch_balance()
        cur = bal.get(self.account_currency) or {}
        cash = float(cur.get("total") or 0.0)
        equity = cash
        if not self._derivatives:  # spot: add the value of held coins
            for pos in self.get_positions():
                equity += pos.qty * (pos.last_price or 0.0)
        return AccountBalance(broker_id=self.broker_id, currency=self.account_currency, cash=cash, equity=equity,
                              margin_used=float(cur.get("used") or 0.0))

    @_translate
    def get_positions(self) -> list[Position]:
        out = []
        if self._derivatives:
            for p in self.exchange.fetch_positions():
                qty = float(p.get("contracts") or 0) * float(p.get("contractSize") or 1) * (-1 if p.get("side") == "short" else 1)
                if qty:
                    try:
                        iid = self.registry.from_broker(self.broker_id, p["symbol"])
                    except KeyError:
                        iid = f"UNMAPPED:{p['symbol']}"  # surfaced, never hidden: reconciliation will flag it
                    out.append(Position(broker_id=self.broker_id, instrument_id=iid, qty=qty, avg_price=p.get("entryPrice"), last_price=p.get("markPrice")))
            return out
        totals = self.exchange.fetch_balance().get("total") or {}
        for inst in self.registry.all():
            if inst.asset_class is AssetClass.CRYPTO and self.registry.is_mapped(self.broker_id, inst.id):
                qty = float(totals.get(inst.base_currency) or 0.0)
                if qty > 0:
                    last = self.exchange.fetch_ticker(self._symbol(inst.id)).get("last")
                    out.append(Position(broker_id=self.broker_id, instrument_id=inst.id, qty=qty, avg_price=None, last_price=last))
        return out

    @_translate
    def get_market_data(self, instrument_id: str) -> Quote:
        t = self.exchange.fetch_ticker(self._symbol(instrument_id))
        ts = datetime.fromtimestamp(t["timestamp"] / 1000, timezone.utc) if t.get("timestamp") else datetime.now(timezone.utc)
        return Quote(instrument_id=instrument_id, ts=ts, bid=t.get("bid"), ask=t.get("ask"), last=t.get("last"), kind=DataKind.REALTIME.value)

    @_translate
    def get_historical_data(self, instrument_id: str, timeframe: str, limit: int = 500) -> Dataset:
        if timeframe not in self.capabilities.timeframes:
            raise UnsupportedOperation(f"{self.broker_id} does not provide {timeframe} bars")
        rows = self.exchange.fetch_ohlcv(self._symbol(instrument_id), timeframe, limit=limit)
        idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True).rename("ts")
        bars = pd.DataFrame([r[1:6] for r in rows], index=idx, columns=OHLCV, dtype=float)
        # The newest bar is still forming; signals must only ever see completed bars.
        now = pd.Timestamp(self.exchange.milliseconds(), unit="ms", tz="UTC")
        bars = bars[bars.index + TIMEFRAMES[timeframe] <= now]
        return Dataset(bars, DataKind.HISTORICAL, f"ccxt:{self.exchange.id}")

    # ---- orders ----
    def _parse(self, raw: dict, order: Order) -> Order:
        filled = float(raw.get("filled") or 0.0)
        status = _STATUS.get(raw.get("status") or "open", OrderStatus.SUBMITTED)
        if status is OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        fee = raw.get("fee") or {}
        return order.model_copy(update={
            "broker_order_id": str(raw["id"]), "status": status, "filled_qty": filled, "avg_fill_price": raw.get("average"),
            "commission": float(fee.get("cost") or 0.0), "is_simulated": False, "updated_at": datetime.now(timezone.utc)})

    @_translate
    def submit_order(self, order: Order) -> Order:
        if not self.capabilities.trading_enabled:
            raise UnsupportedOperation("trading is not enabled on this connection")
        params: dict = {"clientOrderId": order.client_order_id}
        if order.reduce_only:
            params["reduceOnly"] = True
        if order.type in (OrderType.STOP, OrderType.STOP_LIMIT):
            params["triggerPrice"] = order.stop_price
        kind = "limit" if order.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) else "market"
        raw = self.exchange.create_order(self._symbol(order.instrument_id), kind, order.side.value, order.qty, order.limit_price, params)
        return self._parse(raw, order)

    @_translate
    def cancel_order(self, order: Order) -> Order:
        try:
            self.exchange.cancel_order(order.broker_order_id, self._symbol(order.instrument_id))
        except ccxt.OrderNotFound:
            pass  # already filled or cancelled: fetch the truth below
        return self.get_order_status(order)

    @_translate
    def get_order_status(self, order: Order) -> Order:
        return self._parse(self.exchange.fetch_order(order.broker_order_id, self._symbol(order.instrument_id)), order)

    @_translate
    def get_open_orders(self) -> list[Order]:
        out = []
        for raw in self.exchange.fetch_open_orders():
            try:
                iid = self.registry.from_broker(self.broker_id, raw["symbol"])
            except KeyError:
                iid = f"UNMAPPED:{raw['symbol']}"
            base = Order(broker_id=self.broker_id, instrument_id=iid, side=Side(raw["side"]), qty=float(raw["amount"]),
                         type=OrderType.LIMIT if raw.get("type") == "limit" else OrderType.MARKET, limit_price=raw.get("price"),
                         client_order_id=raw.get("clientOrderId") or str(raw["id"]))
            out.append(self._parse(raw, base))
        return out

    @_translate
    def find_order(self, client_order_id: str, instrument_id: str) -> Order | None:
        symbol = self._symbol(instrument_id)
        candidates = list(self.exchange.fetch_open_orders(symbol))
        if self.exchange.has.get("fetchClosedOrders"):
            candidates += list(self.exchange.fetch_closed_orders(symbol, limit=50))
        for raw in candidates:
            if raw.get("clientOrderId") == client_order_id:
                base = Order(broker_id=self.broker_id, instrument_id=instrument_id, side=Side(raw["side"]), qty=float(raw["amount"]),
                             client_order_id=client_order_id)
                return self._parse(raw, base)
        return None

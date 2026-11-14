"""Alpaca Markets adapter (official Trading API v2 over REST).

Paper and live are separate endpoints with separate credentials. Without a `LiveAuthorization` the adapter can
only ever talk to the paper endpoint; the live endpoint is reachable only through the platform's live gate.

Virtual capital: an Alpaca paper account starts with a large USD balance that has nothing to do with the capital
the user wants to trade. The adapter therefore reports a *virtual* balance: the configured capital, converted to
the account currency once (at the rate recorded when the account was linked), plus the profit and loss the broker
account has made since then. Position sizing and every risk limit work on that figure. The broker's real buying
power is checked separately before an order is sent and is never presented as the user's capital.

VERIFICATION STATUS: written from Alpaca's API documentation and tested against an emulated API. It has not been
exercised with real Alpaca credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pandas as pd

from ..domain.models import AccountBalance, AssetClass, Order, OrderStatus, OrderType, Position, Quote, Side
from ..instruments.registry import InstrumentRegistry, market_of
from ..live_trading.gate import LiveAuthorization, require_valid
from ..market_data.quality import DataKind
from ..market_data.sources import Dataset
from ..market_data.timeframes import OHLCV
from .base import BrokerAdapter, BrokerCapabilities, BrokerError, ConnectionLost, OrderRejected, RateLimited

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

_STATUS = {
    "new": OrderStatus.SUBMITTED, "accepted": OrderStatus.SUBMITTED, "pending_new": OrderStatus.SUBMITTED, "accepted_for_bidding": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.SUBMITTED, "pending_replace": OrderStatus.SUBMITTED, "held": OrderStatus.SUBMITTED, "calculated": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED, "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED, "expired": OrderStatus.CANCELLED, "done_for_day": OrderStatus.CANCELLED, "replaced": OrderStatus.CANCELLED,
    "stopped": OrderStatus.CANCELLED, "rejected": OrderStatus.REJECTED, "suspended": OrderStatus.REJECTED,
}
_TYPE = {OrderType.MARKET: "market", OrderType.LIMIT: "limit", OrderType.STOP: "stop", OrderType.STOP_LIMIT: "stop_limit"}
_TIMEFRAME = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour", "4h": "4Hour", "1d": "1Day", "1w": "1Week"}


class AlpacaAdapter(BrokerAdapter):
    def __init__(self, broker_id: str, registry: InstrumentRegistry, key: str, secret: str, virtual_capital: float | None = None,
                 capital_fx: float | None = None, authorization: LiveAuthorization | None = None, feed: str = "iex",
                 client: httpx.Client | None = None, state: dict | None = None, trading_url: str | None = None, data_url: str | None = None) -> None:
        """`virtual_capital` is in the platform's base currency; `capital_fx` converts one unit of it into the account
        currency at linking time. `state` restores the baseline saved by an earlier connection."""
        super().__init__(broker_id, registry)
        self._authorization = authorization
        if authorization is not None:
            require_valid(authorization)
        # The endpoint follows from the authorization alone: no setting, parameter or AI output can pick the live one.
        self.trading_url = (trading_url or (LIVE_URL if authorization is not None else PAPER_URL)).rstrip("/")
        self.data_url = (data_url or DATA_URL).rstrip("/")
        self.feed = feed
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._has_keys = bool(key and secret)
        self.client = client or httpx.Client(timeout=15)
        self.virtual_capital, self.capital_fx = virtual_capital, capital_fx
        self.state: dict = dict(state or {})
        self.assets: dict[str, dict] = {}
        self._caps: BrokerCapabilities | None = None
        self.account_currency = "USD"

    @property
    def is_live(self) -> bool:
        return self._authorization is not None

    @property
    def environment(self) -> str:
        return "live" if self.is_live else "paper"

    # ---- transport
    def _call(self, method: str, url: str, **kw):
        try:
            r = self.client.request(method, url, headers=self._headers, **kw)
        except httpx.TimeoutException as exc:
            raise ConnectionLost(f"Alpaca did not answer in time ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:
            raise ConnectionLost(f"Alpaca is unreachable ({type(exc).__name__})") from None
        if r.status_code == 429:
            retry = r.headers.get("Retry-After")
            raise RateLimited("Alpaca rate limit reached", float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
        if r.status_code >= 500:
            raise ConnectionLost(f"Alpaca server error {r.status_code}")
        if r.status_code in (401, 403) and "/v2/orders" not in url:
            raise BrokerError("Alpaca rejected the API key for this endpoint. Paper keys work only on the paper endpoint and live keys only on the live one.")
        if r.status_code in (400, 403, 422):
            try:
                message = r.json().get("message", r.text)
            except ValueError:
                message = r.text
            raise OrderRejected(f"Alpaca: {str(message)[:300]}")
        if r.status_code == 404:
            raise BrokerError("Alpaca: not found")
        if r.status_code == 204:
            return None
        try:
            return r.json()
        except ValueError:
            raise BrokerError("Alpaca returned an unreadable answer") from None

    def _trading(self, method: str, path: str, **kw):
        return self._call(method, self.trading_url + path, **kw)

    # ---- lifecycle
    def connect(self) -> None:
        if not self._has_keys:
            raise BrokerError("Alpaca needs an API key and secret")
        if self.is_live:
            require_valid(self._authorization)
        account = self._trading("GET", "/v2/account")
        if account.get("account_blocked") or account.get("trading_blocked"):
            raise BrokerError("This Alpaca account is blocked from trading")
        self.account_currency = account.get("currency", "USD")
        equity = float(account["equity"])
        if self.virtual_capital is not None and "baseline_equity" not in self.state:
            if not self.capital_fx or self.capital_fx <= 0:
                raise BrokerError("An exchange rate is needed to convert the virtual capital into the account currency")
            self.state = {"baseline_equity": equity, "virtual_capital_base": self.virtual_capital, "capital_fx": self.capital_fx,
                          "virtual_start": self.virtual_capital * self.capital_fx, "linked_at": datetime.now(timezone.utc).isoformat(),
                          "ledger": [{"ts": datetime.now(timezone.utc).isoformat(), "kind": "initial_capital", "amount_base": self.virtual_capital,
                                      "fx": self.capital_fx, "amount_account": self.virtual_capital * self.capital_fx}]}
        self.assets = {}
        for asset_class in ("us_equity", "crypto"):
            for a in self._trading("GET", "/v2/assets", params={"status": "active", "asset_class": asset_class}) or []:
                self.assets[a["symbol"]] = a
        for inst in self.registry.all():
            symbol = self._native(inst)
            if symbol and symbol in self.assets and not self.registry.is_mapped(self.broker_id, inst.id):
                self.registry.map_symbol(self.broker_id, inst.id, symbol)
        self._caps = BrokerCapabilities(
            asset_classes=frozenset({AssetClass.STOCK, AssetClass.ETF, AssetClass.CRYPTO}),
            order_types=frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT}),
            supports_short=True, supports_reduce_only=False, supports_historical_data=True, supports_bid_ask=True,
            timeframes=frozenset(_TIMEFRAME), is_simulated=False, trading_enabled=True)

    @staticmethod
    def _native(inst) -> str | None:
        if inst.asset_class in (AssetClass.STOCK, AssetClass.ETF) and market_of(inst) == "US":
            return inst.symbol
        if inst.asset_class is AssetClass.CRYPTO and inst.quote_currency in ("USD", "USDT", "USDC"):
            return inst.symbol
        return None

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

    def _asset(self, instrument_id: str) -> dict:
        return self.assets.get(self._symbol(instrument_id), {})

    # ---- account
    def broker_account(self) -> dict:
        """The broker's own figures, in the account currency. Not the user's capital when a virtual capital is set."""
        a = self._trading("GET", "/v2/account")
        return {"currency": a.get("currency", "USD"), "equity": float(a["equity"]), "cash": float(a["cash"]), "buying_power": float(a.get("buying_power", 0)),
                "status": a.get("status"), "pattern_day_trader": a.get("pattern_day_trader"), "environment": self.environment}

    def get_account_balance(self) -> AccountBalance:
        a = self.broker_account()
        if "baseline_equity" not in self.state:
            return AccountBalance(broker_id=self.broker_id, currency=a["currency"], cash=a["cash"], equity=a["equity"])
        equity = self.state["virtual_start"] + (a["equity"] - self.state["baseline_equity"])
        held = sum(abs(p.qty) * (p.last_price or p.avg_price or 0.0) for p in self.get_positions())
        return AccountBalance(broker_id=self.broker_id, currency=a["currency"], cash=equity - held, equity=equity)

    def adjust_virtual_capital(self, amount_base: float, fx: float, kind: str) -> dict:
        """A virtual deposit (positive) or withdrawal (negative), in base currency, converted at the given rate and recorded."""
        if "virtual_start" not in self.state:
            raise BrokerError("This account has no virtual capital")
        if fx <= 0:
            raise BrokerError("A positive exchange rate is required")
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "amount_base": amount_base, "fx": fx, "amount_account": amount_base * fx}
        if self.state["virtual_start"] + entry["amount_account"] <= 0:
            raise BrokerError("A withdrawal cannot exceed the virtual capital")
        self.state = {**self.state, "virtual_start": self.state["virtual_start"] + entry["amount_account"], "ledger": [*self.state["ledger"], entry]}
        return entry

    def to_state(self) -> dict:
        return dict(self.state)

    def get_positions(self) -> list[Position]:
        reverse = {self.registry.to_broker(self.broker_id, i.id): i.id for i in self.registry.all() if self.registry.is_mapped(self.broker_id, i.id)}
        out = []
        for p in self._trading("GET", "/v2/positions") or []:
            iid = reverse.get(p["symbol"]) or reverse.get(p["symbol"].replace("USD", "/USD") if "/" not in p["symbol"] else p["symbol"])
            if iid is None:
                continue  # held at the broker but not an instrument the platform trades
            qty = float(p["qty"]) * (-1 if p.get("side") == "short" and float(p["qty"]) > 0 else 1)
            out.append(Position(broker_id=self.broker_id, instrument_id=iid, qty=qty, avg_price=float(p["avg_entry_price"]),
                                last_price=float(p["current_price"]) if p.get("current_price") else None))
        return out

    # ---- market data
    def get_market_data(self, instrument_id: str) -> Quote:
        symbol, inst = self._symbol(instrument_id), self.registry.get(instrument_id)
        if inst.asset_class is AssetClass.CRYPTO:
            q = self._call("GET", f"{self.data_url}/v1beta3/crypto/us/latest/quotes", params={"symbols": symbol})["quotes"][symbol]
        else:
            q = self._call("GET", f"{self.data_url}/v2/stocks/{symbol}/quotes/latest", params={"feed": self.feed})["quote"]
        return Quote(instrument_id=instrument_id, ts=pd.Timestamp(q["t"]).to_pydatetime(), bid=float(q["bp"]) or None, ask=float(q["ap"]) or None,
                     kind="realtime")

    def get_historical_data(self, instrument_id: str, timeframe: str, limit: int = 500) -> Dataset:
        symbol, inst = self._symbol(instrument_id), self.registry.get(instrument_id)
        crypto = inst.asset_class is AssetClass.CRYPTO
        url = f"{self.data_url}/v1beta3/crypto/us/bars" if crypto else f"{self.data_url}/v2/stocks/bars"
        params = {"symbols": symbol, "timeframe": _TIMEFRAME[timeframe], "limit": min(limit, 10_000), "sort": "desc",
                  "start": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")} | ({} if crypto else {"feed": self.feed, "adjustment": "all"})
        rows = (self._call("GET", url, params=params).get("bars") or {}).get(symbol) or []
        frame = pd.DataFrame([[b["o"], b["h"], b["l"], b["c"], b["v"]] for b in rows], columns=OHLCV,
                             index=pd.DatetimeIndex([pd.Timestamp(b["t"]) for b in rows], name="ts")).sort_index()
        return Dataset(frame, DataKind.REALTIME, f"alpaca:{self.feed if not crypto else 'crypto'}")

    # ---- orders
    def instrument_reasons(self, order: Order) -> list[str]:
        inst, asset, reasons = self.registry.get(order.instrument_id), self._asset(order.instrument_id), []
        if not asset or not asset.get("tradable", False):
            return [f"{inst.symbol} is not tradable at Alpaca"]
        crypto = inst.asset_class is AssetClass.CRYPTO
        if order.side is Side.SELL and not order.reduce_only and (crypto or not asset.get("shortable", False)):
            reasons.append(f"{inst.symbol} cannot be sold short at Alpaca")
        if crypto and order.type is OrderType.STOP:
            reasons.append("Alpaca does not accept plain stop orders for crypto")
        if not crypto and abs(order.qty - round(order.qty)) > 1e-9 and (order.type is not OrderType.MARKET or not asset.get("fractionable", False)):
            reasons.append("fractional share quantities are accepted only for market orders in fractionable stocks")
        if not order.reduce_only and order.side is Side.BUY:
            try:
                power = self.broker_account()["buying_power"]
                price = order.limit_price or order.stop_price
                if price and order.qty * price > power:
                    reasons.append(f"the order needs {order.qty * price:.2f} but the broker's buying power is {power:.2f}")
            except BrokerError:
                pass  # the order itself will fail loudly if the broker cannot be reached
        return reasons

    def market_open(self) -> dict:
        c = self._trading("GET", "/v2/clock")
        return {"is_open": bool(c.get("is_open")), "next_open": c.get("next_open"), "next_close": c.get("next_close")}

    def _to_order(self, raw: dict, base: Order) -> Order:
        filled = float(raw.get("filled_qty") or 0)
        status = _STATUS.get(raw.get("status", ""), OrderStatus.SUBMITTED)
        if status is OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        return base.model_copy(update={
            "status": status, "filled_qty": filled, "avg_fill_price": float(raw["filled_avg_price"]) if raw.get("filled_avg_price") else None,
            "broker_order_id": raw.get("id"), "is_simulated": False, "updated_at": datetime.now(timezone.utc),
            "reason": base.reason if status is not OrderStatus.REJECTED else f"{base.reason or ''} | rejected by Alpaca".strip(" |")})

    def submit_order(self, order: Order) -> Order:
        inst = self.registry.get(order.instrument_id)
        crypto = inst.asset_class is AssetClass.CRYPTO
        body: dict = {"symbol": self._symbol(order.instrument_id), "qty": f"{order.qty:.9f}".rstrip("0").rstrip("."), "side": order.side.value.lower(),
                      "type": _TYPE[order.type], "client_order_id": order.client_order_id,
                      # Stocks: market orders are for today's session; resting stops and limits stay until cancelled.
                      "time_in_force": "gtc" if crypto or order.type is not OrderType.MARKET else "day"}
        if order.limit_price is not None:
            body["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            body["stop_price"] = str(order.stop_price)
        return self._to_order(self._trading("POST", "/v2/orders", json=body), order)

    def cancel_order(self, order: Order) -> Order:
        if order.broker_order_id is None:
            raise BrokerError(f"Order {order.id} was never accepted by Alpaca")
        try:
            self._trading("DELETE", f"/v2/orders/{order.broker_order_id}")
        except OrderRejected:
            pass  # already filled or cancelled: the status read below tells which
        return self.get_order_status(order)

    def replace_order(self, order: Order, qty: float | None = None, limit_price: float | None = None, stop_price: float | None = None) -> Order:
        """Modify a working order. Alpaca replaces it with a new one; the platform's order keeps its identity."""
        if order.broker_order_id is None:
            raise BrokerError(f"Order {order.id} was never accepted by Alpaca")
        body = {k: str(v) for k, v in (("qty", qty), ("limit_price", limit_price), ("stop_price", stop_price)) if v is not None}
        raw = self._trading("PATCH", f"/v2/orders/{order.broker_order_id}", json=body)
        updated = order.model_copy(update={k: v for k, v in (("qty", qty), ("limit_price", limit_price), ("stop_price", stop_price)) if v is not None})
        return self._to_order(raw, updated)

    def get_order_status(self, order: Order) -> Order:
        if order.broker_order_id is None:
            found = self.find_order(order.client_order_id, order.instrument_id)
            if found is None:
                raise BrokerError(f"Order {order.id} is unknown to Alpaca")
            return found.model_copy(update={"id": order.id, "strategy": order.strategy, "signal_id": order.signal_id, "reason": order.reason,
                                            "reduce_only": order.reduce_only, "created_at": order.created_at})
        return self._to_order(self._trading("GET", f"/v2/orders/{order.broker_order_id}"), order)

    def find_order(self, client_order_id: str, instrument_id: str) -> Order | None:
        try:
            raw = self._trading("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        except BrokerError as exc:
            if "not found" in str(exc):
                return None
            raise
        base = Order(client_order_id=client_order_id, broker_id=self.broker_id, instrument_id=instrument_id, side=Side(raw["side"].lower()),
                     qty=float(raw["qty"]), type={v: k for k, v in _TYPE.items()}[raw["type"]],
                     limit_price=float(raw["limit_price"]) if raw.get("limit_price") else None, stop_price=float(raw["stop_price"]) if raw.get("stop_price") else None)
        return self._to_order(raw, base)

    def get_open_orders(self) -> list[Order]:
        reverse = {self.registry.to_broker(self.broker_id, i.id): i.id for i in self.registry.all() if self.registry.is_mapped(self.broker_id, i.id)}
        out = []
        for raw in self._trading("GET", "/v2/orders", params={"status": "open", "limit": 500}) or []:
            iid = reverse.get(raw["symbol"])
            if iid is None:
                continue
            base = Order(client_order_id=raw.get("client_order_id") or raw["id"], broker_id=self.broker_id, instrument_id=iid, side=Side(raw["side"].lower()),
                         qty=float(raw["qty"]), type={v: k for k, v in _TYPE.items()}.get(raw["type"], OrderType.MARKET),
                         limit_price=float(raw["limit_price"]) if raw.get("limit_price") else None, stop_price=float(raw["stop_price"]) if raw.get("stop_price") else None)
            out.append(self._to_order(raw, base))
        return out

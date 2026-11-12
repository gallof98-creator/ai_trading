"""Broker adapter contract, capability model and error taxonomy."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain.models import AccountBalance, AssetClass, Order, OrderType, Position, Quote, Side
from ..instruments.registry import InstrumentRegistry
from ..market_data.sources import Dataset


class BrokerError(Exception):
    pass


class RetryableBrokerError(BrokerError):
    """Transient: the request may be retried with the SAME client order id."""

    retry_after: float | None = None


class ConnectionLost(RetryableBrokerError):
    pass


class RateLimited(RetryableBrokerError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OrderRejected(BrokerError):
    pass


class UnsupportedOperation(BrokerError):
    pass


@dataclass(frozen=True)
class BrokerCapabilities:
    asset_classes: frozenset[AssetClass]
    order_types: frozenset[OrderType]
    supports_short: bool
    supports_reduce_only: bool
    supports_historical_data: bool
    supports_bid_ask: bool
    timeframes: frozenset[str]
    is_simulated: bool
    trading_enabled: bool = True  # False => market data only

    def to_dict(self) -> dict:
        return {
            "asset_classes": sorted(a.value for a in self.asset_classes), "order_types": sorted(t.value for t in self.order_types),
            "supports_short": self.supports_short, "supports_reduce_only": self.supports_reduce_only,
            "supports_historical_data": self.supports_historical_data, "supports_bid_ask": self.supports_bid_ask,
            "timeframes": sorted(self.timeframes), "is_simulated": self.is_simulated, "trading_enabled": self.trading_enabled,
        }


class BrokerAdapter(ABC):
    """Uniform interface over one broker/exchange account. Adapters translate; they hold no
    strategy, sizing or risk logic."""

    def __init__(self, broker_id: str, registry: InstrumentRegistry) -> None:
        self.broker_id = broker_id
        self.registry = registry

    # ---- lifecycle ----
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Detected at connect time; never assumed identical across brokers."""

    # ---- account / data ----
    @abstractmethod
    def get_account_balance(self) -> AccountBalance: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_market_data(self, instrument_id: str) -> Quote: ...

    @abstractmethod
    def get_historical_data(self, instrument_id: str, timeframe: str, limit: int = 500) -> Dataset: ...

    # ---- orders ----
    @abstractmethod
    def get_open_orders(self) -> list[Order]: ...

    @abstractmethod
    def submit_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_order(self, order: Order) -> Order: ...

    @abstractmethod
    def get_order_status(self, order: Order) -> Order: ...

    @abstractmethod
    def find_order(self, client_order_id: str, instrument_id: str) -> Order | None:
        """Look an order up by OUR client id. Used after a timeout to learn whether a submit
        reached the broker, so a retry cannot duplicate it."""

    # ---- capability checks ----
    def instrument_reasons(self, order: Order) -> list[str]:
        """Adapter-specific instrument/size checks (override)."""
        return []

    def unsupported_reasons(self, order: Order) -> list[str]:
        """Why this adapter cannot take this order. Empty list => supported."""
        try:
            inst = self.registry.get(order.instrument_id)
        except KeyError:
            return [f"unknown instrument {order.instrument_id}"]
        caps, reasons = self.capabilities, []
        if not self.is_connected():
            reasons.append("broker is not connected")
        if not caps.trading_enabled:
            reasons.append("trading is not enabled on this connection (market data only)")
        if inst.asset_class not in caps.asset_classes:
            reasons.append(f"asset class {inst.asset_class.value} not supported")
        if not self.registry.is_mapped(self.broker_id, inst.id):
            reasons.append(f"{inst.id} has no symbol mapping for {self.broker_id}")
        if order.type not in caps.order_types:
            reasons.append(f"order type {order.type.value} not supported")
        if order.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limit_price is None:
            reasons.append("limit price missing")
        if order.type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.stop_price is None:
            reasons.append("stop price missing")
        if order.reduce_only and not caps.supports_reduce_only:
            reasons.append("reduce-only orders not supported")
        if order.side is Side.SELL and not order.reduce_only and not (caps.supports_short and inst.shortable):
            reasons.append("short selling not supported for this instrument/broker")
        if inst.min_qty and order.qty < inst.min_qty:
            reasons.append(f"quantity {order.qty} below minimum {inst.min_qty}")
        if abs(inst.round_qty(order.qty) - order.qty) > 1e-9:
            reasons.append(f"quantity {order.qty} is not a multiple of lot size {inst.lot_size}")
        if not reasons:
            reasons.extend(self.instrument_reasons(order))
        return reasons


class BrokerManager:
    """Registry of connected accounts. The single gate through which orders reach adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, BrokerAdapter] = {}

    def register(self, adapter: BrokerAdapter) -> None:
        if adapter.broker_id in self._adapters:
            raise ValueError(f"Broker {adapter.broker_id} already registered")
        self._adapters[adapter.broker_id] = adapter

    def remove(self, broker_id: str) -> None:
        self._adapters.pop(broker_id, None)

    def get(self, broker_id: str) -> BrokerAdapter:
        try:
            return self._adapters[broker_id]
        except KeyError:
            raise BrokerError(f"Unknown broker account {broker_id}") from None

    def all(self) -> list[BrokerAdapter]:
        return list(self._adapters.values())

    def submit(self, order: Order) -> Order:
        """Never forwards an order the adapter has not declared it supports."""
        adapter = self.get(order.broker_id)
        reasons = adapter.unsupported_reasons(order)
        if reasons:
            raise UnsupportedOperation("; ".join(reasons))
        return adapter.submit_order(order)

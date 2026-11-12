"""Normalized, broker-independent domain models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    STOCK = "stock"
    ETF = "etf"
    FOREX = "forex"
    FUTURE = "future"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    NEW = "new"
    PENDING_APPROVAL = "pending_approval"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class TradingMode(StrEnum):
    MANUAL = "manual"  # AI signals only; the user trades by hand
    SEMI_AUTO = "semi_auto"  # orders wait for user approval
    AUTO = "auto"  # orders are routed after risk approval


class Instrument(BaseModel):
    """Internal instrument. `id` is the normalized identifier used everywhere, e.g.
    CRYPTO:BTC/USDT, STOCK:AAPL, ETF:SPY, FOREX:EUR/USD, FUTURE:ES-202612."""

    id: str
    asset_class: AssetClass
    symbol: str
    quote_currency: str
    base_currency: str | None = None
    exchange: str | None = None
    calendar: str = "24x7"
    tick_size: float = Field(0.01, gt=0)
    lot_size: float = Field(1.0, gt=0)  # quantity increment
    min_qty: float = Field(0.0, ge=0)
    contract_multiplier: float = Field(1.0, gt=0)
    margin_rate: float = Field(1.0, gt=0, le=1)  # initial margin as fraction of notional
    shortable: bool = True
    expiry: datetime | None = None

    def round_qty(self, qty: float) -> float:
        """Round a quantity DOWN to the lot size (never round a risk-sized order up)."""
        steps = int(abs(qty) / self.lot_size + 1e-6)  # tolerance for binary floating point (e.g. 0.3 / 0.1)
        return round(steps * self.lot_size, 12) * (1 if qty >= 0 else -1)

    def is_lot_multiple(self, qty: float) -> bool:
        steps = abs(qty) / self.lot_size
        return abs(steps - round(steps)) < 1e-6

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 12)

    def notional(self, qty: float, price: float) -> float:
        """Notional in quote currency."""
        return abs(qty) * price * self.contract_multiplier


class Bar(BaseModel):
    instrument_id: str
    timeframe: str
    ts: datetime  # bar OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Signal(BaseModel):
    id: str = Field(default_factory=new_id)
    ts: datetime
    instrument_id: str
    timeframe: str
    strategy: str
    strategy_version: str
    action: SignalAction
    target: int  # desired position direction: -1, 0, +1
    price: float
    stop_distance: float | None = None
    take_profit_distance: float | None = None
    trailing_distance: float | None = None
    # Only populated when a statistically calibrated model produced it; never a made-up number.
    confidence: float | None = None
    regime: str | None = None
    meta: dict = Field(default_factory=dict)


class Order(BaseModel):
    id: str = Field(default_factory=new_id)
    # Sent to the broker as the client order id: resubmitting the same key can never create a second order.
    client_order_id: str = ""
    broker_id: str
    instrument_id: str
    side: Side
    qty: float = Field(gt=0)
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    commission: float = 0.0
    broker_order_id: str | None = None
    signal_id: str | None = None
    strategy: str | None = None
    reduce_only: bool = False
    is_simulated: bool = True  # False only for orders accepted by a real broker
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _ctx) -> None:
        if not self.client_order_id:
            self.client_order_id = self.id

    @property
    def remaining(self) -> float:
        return self.qty - self.filled_qty

    @property
    def is_simulated_venue(self) -> bool:
        return self.is_simulated

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)


class Fill(BaseModel):
    order_id: str
    broker_id: str
    instrument_id: str
    side: Side
    qty: float = Field(gt=0)
    price: float
    commission: float = 0.0  # in the instrument's quote currency
    ts: datetime
    strategy: str | None = None


class Quote(BaseModel):
    instrument_id: str
    ts: datetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    kind: str = "realtime"  # realtime | delayed | simulated


class Position(BaseModel):
    broker_id: str
    instrument_id: str
    qty: float  # signed: >0 long, <0 short
    avg_price: float | None  # None when the broker does not report a cost basis
    realized_pnl: float = 0.0  # quote currency
    last_price: float | None = None

    @property
    def direction(self) -> int:
        return (self.qty > 0) - (self.qty < 0)


class AccountBalance(BaseModel):
    broker_id: str
    currency: str
    cash: float
    equity: float
    margin_used: float = 0.0

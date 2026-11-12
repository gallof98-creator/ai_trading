"""Simulated broker: the execution venue for BOTH backtesting and paper trading.

Fill rules (deliberately conservative):
  * Market orders fill at the next bar's open, worsened by half the spread plus slippage.
  * Stop orders trigger when the bar trades through the stop; they fill at the stop, or at the
    open if the bar gapped beyond it, worsened by half the spread plus slippage.
  * Limit orders fill only if the bar trades strictly THROUGH the limit (touching is not enough),
    at the limit price, or at the open if the bar opened beyond it.
  * Within one bar, stops are evaluated before limits (a stop and a target hit in the same bar
    counts as the stop).
  * A fill cannot exceed `participation_rate` of the bar's volume; the rest stays working.
  * Commission, financing (funding/borrow) and currency conversion costs are charged.
  * Positions are kept per (instrument, strategy tag) so that two strategies holding opposite
    positions in one instrument each keep their own stop and exit orders. `get_positions`
    reports the net position per instrument, as a netting broker would.
All amounts are simulated. Orders created here always carry is_simulated=True.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from ..brokers.base import BrokerAdapter, BrokerCapabilities, BrokerError, OrderRejected
from ..domain.fees import DEFAULT_COMMISSIONS, CommissionModel
from ..domain.models import AccountBalance, AssetClass, Fill, Order, OrderStatus, OrderType, Position, Quote, Side
from ..instruments.registry import InstrumentRegistry
from ..market_data.calendars import get_calendar
from ..market_data.fx import FxRateProvider
from ..market_data.sources import Dataset
from ..market_data.timeframes import TIMEFRAMES


@dataclass
class ExecutionModel:
    spread_bps: float = 2.0  # full bid/ask spread; half is paid on each side
    slippage_bps: float = 1.0
    participation_rate: float | None = 0.1  # max share of a bar's volume one order may take; None = unlimited
    commissions: dict[str, CommissionModel] = field(default_factory=lambda: dict(DEFAULT_COMMISSIONS))
    financing_long_annual: float = 0.0  # funding / margin interest on long notional
    financing_short_annual: float = 0.0  # borrow fee / funding on short notional

    def to_dict(self) -> dict:
        d = asdict(self)
        d["commissions"] = {k: asdict(v) for k, v in self.commissions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionModel":
        d = dict(d)
        if "commissions" in d:
            d["commissions"] = {k: CommissionModel(**v) for k, v in d["commissions"].items()}
        return cls(**d)


_CAPS = BrokerCapabilities(
    asset_classes=frozenset(AssetClass), order_types=frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}),
    supports_short=True, supports_reduce_only=True, supports_historical_data=False, supports_bid_ask=False,
    timeframes=frozenset(TIMEFRAMES), is_simulated=True, per_strategy_positions=True)
_PRIORITY = {OrderType.STOP: 0, OrderType.MARKET: 1, OrderType.LIMIT: 2}


class SimulatedBroker(BrokerAdapter):
    def __init__(self, broker_id: str, registry: InstrumentRegistry, fx: FxRateProvider, initial_cash: float, currency: str = "EUR",
                 execution: ExecutionModel | None = None) -> None:
        super().__init__(broker_id, registry)
        if not initial_cash > 0:
            raise ValueError("Initial capital must be positive")
        self.fx, self.currency = fx, currency
        self.execution = execution or ExecutionModel()
        self.initial_cash = float(initial_cash)
        self._connected = False
        self._reset_state()

    def _reset_state(self) -> None:
        self.cash = self.initial_cash  # account currency: capital + realised P&L - costs
        self.positions: dict[tuple[str, str | None], list[float]] = {}  # (instrument, strategy tag) -> [signed qty, average price]
        self.orders: dict[str, Order] = {}
        self._seq: dict[str, int] = {}
        self.last_price: dict[str, float] = {}
        self.last_ts: datetime | None = None
        self.costs = {"commission": 0.0, "financing": 0.0, "spread_slippage": 0.0}

    def reset(self, initial_cash: float | None = None) -> None:
        if initial_cash is not None:
            if not initial_cash > 0:
                raise ValueError("Initial capital must be positive")
            self.initial_cash = float(initial_cash)
        self._reset_state()

    # ---- lifecycle / capabilities ----
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    @property
    def capabilities(self) -> BrokerCapabilities:
        return _CAPS

    def map_all_instruments(self) -> None:
        for inst in self.registry.all():
            if not self.registry.is_mapped(self.broker_id, inst.id):
                self.registry.map_symbol(self.broker_id, inst.id, inst.symbol)

    # ---- valuation ----
    def _to_account(self, amount: float, currency: str, ts: datetime, with_cost: bool) -> float:
        return self.fx.convert(amount, currency, self.currency, ts, with_cost=with_cost)

    def _require_ts(self) -> datetime:
        if self.last_ts is None:
            raise BrokerError("Simulated broker has not received any market data yet")
        return self.last_ts

    def _books(self, instrument_id: str) -> list[list[float]]:
        return [v for (iid, _tag), v in self.positions.items() if iid == instrument_id]

    def net_qty(self, instrument_id: str) -> float:
        return sum(v[0] for v in self._books(instrument_id))

    def get_account_balance(self) -> AccountBalance:
        if not self.positions:
            return AccountBalance(broker_id=self.broker_id, currency=self.currency, cash=self.cash, equity=self.cash)
        ts = self._require_ts()
        equity, margin = self.cash, 0.0
        for (iid, _tag), (qty, avg) in self.positions.items():
            inst = self.registry.get(iid)
            price = self.last_price[iid]
            equity += self._to_account(qty * (price - avg) * inst.contract_multiplier, inst.quote_currency, ts, with_cost=False)
            margin += self._to_account(inst.notional(qty, price) * inst.margin_rate, inst.quote_currency, ts, with_cost=False)
        return AccountBalance(broker_id=self.broker_id, currency=self.currency, cash=self.cash, equity=equity, margin_used=margin)

    def get_positions(self) -> list[Position]:
        out = []
        for iid in sorted({iid for iid, _ in self.positions}):
            books = self._books(iid)
            net = sum(q for q, _ in books)
            if abs(net) > 1e-12:
                avg = books[0][1] if len(books) == 1 else None  # a netted cost basis across strategies is not meaningful
                out.append(Position(broker_id=self.broker_id, instrument_id=iid, qty=net, avg_price=avg, last_price=self.last_price.get(iid)))
        return out

    def get_market_data(self, instrument_id: str) -> Quote:
        if instrument_id not in self.last_price:
            raise BrokerError(f"No price received yet for {instrument_id}")
        return Quote(instrument_id=instrument_id, ts=self._require_ts(), last=self.last_price[instrument_id], kind="simulated")

    def get_historical_data(self, instrument_id: str, timeframe: str, limit: int = 500) -> Dataset:
        raise BrokerError("The simulated broker is an execution venue only; it has no market data of its own")

    # ---- orders ----
    def get_open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open]

    def submit_order(self, order: Order) -> Order:
        if order.client_order_id in self._seq:  # idempotent: same client id returns the existing order
            return next(o for o in self.orders.values() if o.client_order_id == order.client_order_id)
        if order.reduce_only:
            qty = self.positions.get((order.instrument_id, order.strategy), (0.0, 0.0))[0]
            if qty == 0 or qty * order.side.sign > 0:
                raise OrderRejected("reduce-only order without an opposite position")
        accepted = order.model_copy(update={"status": OrderStatus.SUBMITTED, "is_simulated": True, "broker_order_id": f"sim-{len(self._seq) + 1}"})
        self._seq[order.client_order_id] = len(self._seq)
        self.orders[accepted.id] = accepted
        return accepted

    def cancel_order(self, order: Order) -> Order:
        current = self.orders.get(order.id)
        if current is None:
            raise BrokerError(f"Unknown order {order.id}")
        if current.is_open:
            current = current.model_copy(update={"status": OrderStatus.CANCELLED, "updated_at": self.last_ts or current.updated_at})
            self.orders[order.id] = current
        return current

    def get_order_status(self, order: Order) -> Order:
        try:
            return self.orders[order.id]
        except KeyError:
            raise BrokerError(f"Unknown order {order.id}") from None

    def find_order(self, client_order_id: str, instrument_id: str) -> Order | None:
        return next((o for o in self.orders.values() if o.client_order_id == client_order_id), None)

    # ---- market simulation ----
    def _session_open(self, instrument_id: str, ts: datetime, intraday: bool) -> bool:
        return not intraday or get_calendar(self.registry.get(instrument_id).calendar).is_open(ts)

    def _eligible(self, instrument_id: str, ts: datetime) -> list[Order]:
        orders = [o for o in self.orders.values() if o.is_open and o.instrument_id == instrument_id and o.created_at <= ts]
        return sorted(orders, key=lambda o: (_PRIORITY[o.type], self._seq[o.client_order_id]))

    def process_open(self, instrument_id: str, ts: datetime, open_: float, volume: float, bar_delta: pd.Timedelta) -> list[Fill]:
        """A new bar opened at `ts`: accrue financing, then fill everything marketable at the open."""
        prev_ts, self.last_ts = self.last_ts, ts
        self._accrue_financing(instrument_id, ts, bar_delta)
        self.last_price[instrument_id] = open_
        self._volume_left = {instrument_id: self._volume_budget(volume)}
        if not self._session_open(instrument_id, ts, bar_delta < pd.Timedelta(days=1)):
            return []
        fills = []
        for o in self._eligible(instrument_id, ts):
            buy = o.side is Side.BUY
            if o.type is OrderType.MARKET:
                price, taker = open_, True
            elif o.type is OrderType.STOP and ((open_ >= o.stop_price) if buy else (open_ <= o.stop_price)):
                price, taker = open_, True  # gapped through the stop
            elif o.type is OrderType.LIMIT and ((open_ < o.limit_price) if buy else (open_ > o.limit_price)):
                price, taker = open_, False  # opened beyond the limit: price improvement
            else:
                continue
            fills += self._execute(o, price, taker, ts)
        return fills

    def process_intrabar(self, instrument_id: str, ts: datetime, high: float, low: float, close: float, bar_delta: pd.Timedelta) -> list[Fill]:
        """The bar that opened at `ts` has completed with this high/low/close."""
        fills = []
        if self._session_open(instrument_id, ts, bar_delta < pd.Timedelta(days=1)):
            for o in self._eligible(instrument_id, ts):
                buy = o.side is Side.BUY
                if o.type is OrderType.STOP and ((high >= o.stop_price) if buy else (low <= o.stop_price)):
                    fills += self._execute(o, o.stop_price, True, ts)
                elif o.type is OrderType.LIMIT and ((low < o.limit_price) if buy else (high > o.limit_price)):
                    fills += self._execute(o, o.limit_price, False, ts)
        self.last_price[instrument_id] = close
        return fills

    def _volume_budget(self, volume: float) -> float | None:
        rate = self.execution.participation_rate
        return None if rate is None or volume <= 0 else rate * volume

    def _accrue_financing(self, instrument_id: str, ts: datetime, bar_delta: pd.Timedelta) -> None:
        if instrument_id not in self.last_price:
            return
        inst = self.registry.get(instrument_id)
        for qty, _avg in self._books(instrument_id):
            rate = self.execution.financing_long_annual if qty > 0 else self.execution.financing_short_annual
            if rate == 0:
                continue
            cost = inst.notional(qty, self.last_price[instrument_id]) * rate * (bar_delta / pd.Timedelta(days=365))
            cost_acct = -self._to_account(-cost, inst.quote_currency, ts, with_cost=True)
            self.cash -= cost_acct
            self.costs["financing"] += cost_acct

    def _execute(self, order: Order, ref_price: float, taker: bool, ts: datetime) -> list[Fill]:
        inst = self.registry.get(order.instrument_id)
        book = (inst.id, order.strategy)
        pos_qty, avg = self.positions.get(book, (0.0, 0.0))
        qty = order.remaining
        if order.reduce_only:
            closable = max(0.0, -pos_qty * order.side.sign)
            if closable <= 0:  # position already gone: the order has nothing left to do
                self.orders[order.id] = order.model_copy(update={"status": OrderStatus.CANCELLED, "updated_at": ts, "reason": order.reason})
                return []
            qty = min(qty, closable)
        budget = self._volume_left.get(inst.id)
        if budget is not None:
            qty = inst.round_qty(min(qty, budget))
            if qty <= 0:
                return []  # no liquidity left in this bar; keep working
        sign = order.side.sign
        penalty = (self.execution.spread_bps / 2 + self.execution.slippage_bps) / 10_000 if taker else 0.0
        price = ref_price * (1 + sign * penalty)

        # Margin check for the part of the fill that opens or increases exposure.
        new_qty = pos_qty + sign * qty
        if abs(new_qty) > abs(pos_qty) + 1e-12:
            balance = self.get_account_balance() if self.positions else AccountBalance(broker_id=self.broker_id, currency=self.currency, cash=self.cash, equity=self.cash)
            extra = self._to_account(inst.notional(abs(new_qty) - abs(pos_qty), price) * inst.margin_rate, inst.quote_currency, ts, with_cost=False)
            if balance.margin_used + extra > balance.equity + 1e-9:
                self.orders[order.id] = order.model_copy(update={"status": OrderStatus.REJECTED, "updated_at": ts, "reason": "insufficient margin"})
                return []

        # Realised P&L on the part that closes existing exposure.
        closing = min(qty, abs(pos_qty)) if pos_qty * sign < 0 else 0.0
        if closing:
            realized = closing * (price - avg) * (1 if pos_qty > 0 else -1) * inst.contract_multiplier
            self.cash += self._to_account(realized, inst.quote_currency, ts, with_cost=True)
        if new_qty == 0 or abs(new_qty) < 1e-12:
            self.positions.pop(book, None)
        elif pos_qty * new_qty < 0 or pos_qty == 0:  # opened fresh or flipped
            self.positions[book] = [new_qty, price]
        elif abs(new_qty) > abs(pos_qty):  # added
            self.positions[book] = [new_qty, (abs(pos_qty) * avg + qty * price) / abs(new_qty)]
        else:  # reduced
            self.positions[book] = [new_qty, avg]

        commission = self.execution.commissions[inst.asset_class.value].cost(qty, price, inst.contract_multiplier)
        commission_acct = -self._to_account(-commission, inst.quote_currency, ts, with_cost=True)
        self.cash -= commission_acct
        self.costs["commission"] += commission_acct
        self.costs["spread_slippage"] += self._to_account(qty * abs(price - ref_price) * inst.contract_multiplier, inst.quote_currency, ts, with_cost=False)
        if budget is not None:
            self._volume_left[inst.id] = budget - qty

        filled = order.filled_qty + qty
        avg_fill = ((order.avg_fill_price or 0.0) * order.filled_qty + price * qty) / filled
        done = filled >= order.qty - 1e-12 or (order.reduce_only and abs(new_qty) < 1e-12)
        self.orders[order.id] = order.model_copy(update={
            "filled_qty": filled, "avg_fill_price": avg_fill, "commission": order.commission + commission, "updated_at": ts,
            "status": OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED})
        return [Fill(order_id=order.id, broker_id=self.broker_id, instrument_id=inst.id, side=order.side, qty=qty, price=price,
                     commission=commission, ts=ts, strategy=order.strategy)]

    def charge_roll(self, instrument_id: str, ts: datetime) -> float:
        """Futures rollover: closing the expiring contract and reopening the next costs two commissions
        and two half-spreads on the open position. Returns the cost in account currency."""
        qty = sum(abs(q) for q, _ in self._books(instrument_id))
        if qty == 0:
            return 0.0
        inst = self.registry.get(instrument_id)
        price = self.last_price[instrument_id]
        cost = 2 * self.execution.commissions[inst.asset_class.value].cost(qty, price, inst.contract_multiplier) \
            + inst.notional(qty, price) * self.execution.spread_bps / 10_000
        cost_acct = -self._to_account(-cost, inst.quote_currency, ts, with_cost=True)
        self.cash -= cost_acct
        self.costs["commission"] += cost_acct
        return cost_acct

    # ---- persistence (paper accounts survive restarts) ----
    def to_state(self) -> dict:
        return {"initial_cash": self.initial_cash, "currency": self.currency, "cash": self.cash,
                "positions": [[iid, tag, qty, avg] for (iid, tag), (qty, avg) in self.positions.items()],
                "orders": [o.model_dump(mode="json") for o in self.orders.values()], "seq": self._seq, "last_price": self.last_price,
                "last_ts": self.last_ts.isoformat() if self.last_ts else None, "costs": self.costs, "execution": self.execution.to_dict()}

    def load_state(self, state: dict) -> None:
        self.initial_cash, self.currency, self.cash = state["initial_cash"], state["currency"], state["cash"]
        self.positions = {(iid, tag): [qty, avg] for iid, tag, qty, avg in state["positions"]}
        self.orders = {o["id"]: Order.model_validate(o) for o in state["orders"]}
        self._seq, self.last_price, self.costs = dict(state["seq"]), dict(state["last_price"]), dict(state["costs"])
        self.last_ts = datetime.fromisoformat(state["last_ts"]) if state["last_ts"] else None
        self.execution = ExecutionModel.from_dict(state["execution"])

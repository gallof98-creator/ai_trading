"""Trading engine: the single pipeline used by backtesting, paper trading and live trading.

    signal -> position sizing -> RISK ENGINE -> (approval) -> broker manager -> adapter
    fill   -> portfolio -> protective stop / take-profit orders

Only the broker adapter differs between the three modes. The engine never reads the wall clock;
callers pass event timestamps, which keeps backtests reproducible.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pandas as pd

from ..brokers.base import BrokerError, BrokerManager, OrderRejected, RetryableBrokerError, UnsupportedOperation
from ..database.settings_store import RiskLimits
from ..domain.models import Fill, Instrument, Order, OrderStatus, OrderType, Side, Signal, TradingMode
from ..instruments.registry import InstrumentRegistry
from ..market_data.fx import FxRateProvider
from ..portfolio.engine import Key, PortfolioEngine
from ..risk.engine import RiskEngine
from ..risk.models import EmergencyAction, OrderIntent, RiskEventRecord
from ..risk.sizing import position_size
from ..strategies.base import Strategy


@dataclass
class Deployment:
    id: str
    strategy: Strategy
    instrument_id: str
    timeframe: str
    broker_id: str

    @property
    def key(self) -> Key:
        return (self.broker_id, self.instrument_id, self.strategy.name)


@dataclass
class Protection:
    direction: int
    stop_distance: float
    take_profit_distance: float | None
    trailing_distance: float | None
    stop_price: float | None = None
    stop_order: Order | None = None
    target_order: Order | None = None
    soft_stop: bool = False  # broker has no stop orders: the engine watches the level at each bar close
    version: int = 0


@dataclass
class PendingApproval:
    id: str
    order: Order
    intent: OrderIntent
    protection: tuple | None
    created_at: datetime
    expires_at: datetime | None = None
    rationale: dict = field(default_factory=dict)


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0


class Journal:
    """Observer interface. The default implementation keeps everything in memory (used by backtests)."""

    def __init__(self) -> None:
        self.signals: list[Signal] = []
        self.orders: dict[str, Order] = {}
        self.fills: list[tuple[Fill, str | None]] = []
        self.risk_events: list[RiskEventRecord] = []
        self.alerts: list[dict] = []

    def signal(self, signal: Signal) -> None:
        self.signals.append(signal)

    def order(self, order: Order) -> None:
        self.orders[order.id] = order

    def fill(self, fill: Fill, reason: str | None) -> None:
        self.fills.append((fill, reason))

    def risk_event(self, event: RiskEventRecord) -> None:
        self.risk_events.append(event)

    def alert(self, ts: datetime, category: str, severity: str, title: str, body: str = "") -> None:
        self.alerts.append({"ts": ts, "category": category, "severity": severity, "title": title, "body": body})


class TradingEngine:
    def __init__(self, registry: InstrumentRegistry, brokers: BrokerManager, portfolio: PortfolioEngine, risk: RiskEngine,
                 fx: FxRateProvider, limits: Callable[[], RiskLimits], mode: Callable[[], TradingMode], journal: Journal | None = None,
                 base_currency: str = "EUR", retry: RetryPolicy | None = None, sleep: Callable[[float], None] = time.sleep,
                 realized_vol: Callable[[str], float | None] | None = None,
                 approval_required: Callable[[Order], bool] | None = None, cost_per_unit: Callable[[Instrument, float], float] | None = None,
                 proposal_ttl: Callable[[], timedelta | None] | None = None, wall_clock: Callable[[], datetime] | None = None) -> None:
        self.registry, self.brokers, self.portfolio, self.risk, self.fx = registry, brokers, portfolio, risk, fx
        self._limits, self._mode = limits, mode
        self.journal = journal or Journal()
        self.base = base_currency
        self.retry, self._sleep = retry or RetryPolicy(), sleep
        self._realized_vol = realized_vol or (lambda instrument_id: None)
        self._approval_required = approval_required or (lambda order: False)
        self._cost_per_unit = cost_per_unit or (lambda instrument, price: 0.0)
        self._proposal_ttl = proposal_ttl or (lambda: None)
        self._wall_clock = wall_clock  # proposals expire in real time, whatever the timestamps of the bars that produced them
        self._rationale: dict = {}  # set while a signal is being turned into orders
        self.orders: dict[str, Order] = {}
        self._client_ids: dict[str, str] = {}  # client order id -> order id (duplicate-order guard)
        self._entry_protection: dict[str, tuple[Key, tuple]] = {}
        self.protections: dict[Key, Protection] = {}
        self.approvals: dict[str, PendingApproval] = {}
        self._n = 0

    # ------------------------------------------------------------------ helpers
    def _new_id(self, prefix: str = "o") -> str:
        self._n += 1
        return f"{prefix}{self._n:07d}"

    def _fx(self, currency: str, ts: datetime) -> float:
        return self.fx.quote(currency, self.base, ts).rate

    def _record(self, order: Order) -> Order:
        self.orders[order.id] = order
        self.journal.order(order)
        return order

    def _reject(self, order: Order, reason: str, ts: datetime, alert: bool = True) -> Order:
        rejected = order.model_copy(update={"status": OrderStatus.REJECTED, "reason": f"{order.reason or ''} | rejected: {reason}".strip(" |"),
                                            "updated_at": ts})
        if alert:
            self.journal.alert(ts, "order_rejected", "warning", f"Order rejected: {order.side.value} {order.qty:g} {order.instrument_id}", reason)
        return self._record(rejected)

    # ------------------------------------------------------------------ market events (simulated venues)
    def on_bar_open(self, instrument_id: str, ts: datetime, open_: float, volume: float, delta: pd.Timedelta) -> None:
        for adapter in self.brokers.all():
            if hasattr(adapter, "process_open") and self.registry.is_mapped(adapter.broker_id, instrument_id):
                self._handle_fills(adapter, adapter.process_open(instrument_id, ts, open_, volume, delta))

    def on_bar_close(self, instrument_id: str, ts_open: datetime, high: float, low: float, close: float, delta: pd.Timedelta) -> None:
        for adapter in self.brokers.all():
            if hasattr(adapter, "process_intrabar") and self.registry.is_mapped(adapter.broker_id, instrument_id):
                self._handle_fills(adapter, adapter.process_intrabar(instrument_id, ts_open, high, low, close, delta))
        ts = ts_open + delta
        self.portfolio.mark(instrument_id, close, ts)
        for key, prot in list(self.protections.items()):
            if key[1] == instrument_id:
                self._update_protection_at_close(key, prot, close, ts)

    def end_of_timestamp(self, ts: datetime, reconcile: bool = False) -> None:
        """Call once after all bars closing at `ts`: revalue, run circuit breakers, apply the emergency policy."""
        if reconcile:
            problems = self.portfolio.reconcile()
            if problems:
                self.journal.alert(ts, "reconciliation", "critical", "Positions do not reconcile with the broker", "; ".join(problems))
        state = self.portfolio.revalue(ts)
        actions = self.risk.monitor(state)
        if actions:
            self.execute_emergency(actions, ts)

    def poll_orders(self, ts: datetime) -> None:
        """Real brokers: pull order updates and turn filled-quantity changes into fills."""
        for old in [o for o in self.orders.values() if o.is_open and not o.is_simulated_venue]:
            adapter = self.brokers.get(old.broker_id)
            try:
                new = adapter.get_order_status(old)
            except BrokerError as exc:
                self.journal.alert(ts, "broker", "warning", f"Order status unavailable at {old.broker_id}", str(exc))
                continue
            self._ingest_update(adapter, old, new, ts)

    def _ingest_update(self, adapter, old: Order, new: Order, ts: datetime) -> None:
        delta = new.filled_qty - old.filled_qty
        self._record(new)
        if delta > 1e-12:
            price = ((new.avg_fill_price or 0) * new.filled_qty - (old.avg_fill_price or 0) * old.filled_qty) / delta
            fill = Fill(order_id=new.id, broker_id=new.broker_id, instrument_id=new.instrument_id, side=new.side, qty=delta, price=price,
                        commission=max(new.commission - old.commission, 0.0), ts=ts, strategy=new.strategy)
            self._apply_fill(fill, new)

    def _handle_fills(self, adapter, fills: list[Fill]) -> None:
        for fill in fills:
            order = adapter.get_order_status(self.orders[fill.order_id])
            self._record(order)
            self._apply_fill(fill, order)
        # Orders the venue closed without a fill (e.g. reduce-only with nothing left, margin rejection).
        for o in [o for o in self.orders.values() if o.is_open and o.broker_id == adapter.broker_id]:
            current = adapter.get_order_status(o)
            if current.status is not o.status:
                self._record(current)
                if current.status is OrderStatus.REJECTED:
                    self.journal.alert(current.updated_at, "order_rejected", "warning",
                                       f"Order rejected by {adapter.broker_id}: {o.side.value} {o.qty:g} {o.instrument_id}", current.reason or "")

    def _apply_fill(self, fill: Fill, order: Order) -> None:
        reason = (order.reason or "").split(" | ")[0] or None
        self.portfolio.on_fill(fill, reason)
        self.journal.fill(fill, reason)
        key: Key = (fill.broker_id, fill.instrument_id, fill.strategy)
        if order.id in self._entry_protection:
            _, (stop, target, trailing) = self._entry_protection[order.id]
            prot = self.protections.get(key)
            if prot is None:
                prot = self.protections[key] = Protection(fill.side.sign, stop, target, trailing)
            self._place_protection(key, prot, fill.ts)
            if not order.is_open:
                del self._entry_protection[order.id]
        if self.portfolio.qty(key) == 0 and key in self.protections:
            self._cancel_protection(key, fill.ts)
            del self.protections[key]

    # ------------------------------------------------------------------ protective orders
    def _cancel(self, order: Order | None, ts: datetime) -> None:
        if order is None:
            return
        current = self.orders.get(order.id, order)
        if not current.is_open:
            return
        try:
            self._record(self.brokers.get(current.broker_id).cancel_order(current))
        except BrokerError as exc:
            self.journal.alert(ts, "broker", "critical", f"Could not cancel order {current.id}", str(exc))

    def _cancel_protection(self, key: Key, ts: datetime) -> None:
        prot = self.protections[key]
        self._cancel(prot.stop_order, ts)
        self._cancel(prot.target_order, ts)
        prot.stop_order = prot.target_order = None

    def _protective_order(self, key: Key, prot: Protection, kind: OrderType, price: float, reason: str, qty: float, ts: datetime) -> Order | None:
        broker_id, instrument_id, strategy = key
        inst = self.registry.get(instrument_id)
        qty = inst.round_qty(qty)  # a venue cannot hold an order for a fraction of a lot
        if qty <= 0:
            return None
        prot.version += 1
        oid = self._new_id()
        order = Order(id=oid, client_order_id=f"{oid}:{reason}", broker_id=broker_id, instrument_id=instrument_id,
                      side=Side.SELL if prot.direction > 0 else Side.BUY, qty=qty, type=kind, reduce_only=True, strategy=strategy,
                      stop_price=inst.round_price(price) if kind is OrderType.STOP else None,
                      limit_price=inst.round_price(price) if kind is OrderType.LIMIT else None, reason=reason, created_at=ts, updated_at=ts)
        adapter = self.brokers.get(broker_id)
        if adapter.unsupported_reasons(self._for_broker(order, adapter)):
            return None
        # Protective orders also pass the risk engine (they are always risk-reducing).
        intent = OrderIntent(order, inst, price, self._fx(inst.quote_currency, ts), None)
        if not self.risk.evaluate(intent, self.portfolio.state(ts)).approved:
            return None
        return self._submit(order, ts)

    def _place_protection(self, key: Key, prot: Protection, ts: datetime) -> None:
        """(Re)place one stop and one target covering the whole open slice."""
        s = self.portfolio.slices.get(key)
        if s is None:
            return
        self._cancel_protection(key, ts)
        qty = abs(s.qty)
        base_stop = s.avg_price - prot.direction * prot.stop_distance
        prot.stop_price = base_stop if prot.stop_price is None else (max if prot.direction > 0 else min)(prot.stop_price, base_stop)
        prot.stop_order = self._protective_order(key, prot, OrderType.STOP, prot.stop_price, "stop_loss" if prot.stop_price == base_stop else "trailing_stop", qty, ts)
        prot.soft_stop = prot.stop_order is None or prot.stop_order.status is OrderStatus.REJECTED
        if prot.soft_stop:
            prot.stop_order = None
        if prot.take_profit_distance:
            prot.target_order = self._protective_order(key, prot, OrderType.LIMIT, s.avg_price + prot.direction * prot.take_profit_distance,
                                                       "take_profit", qty, ts)

    def _update_protection_at_close(self, key: Key, prot: Protection, close: float, ts: datetime) -> None:
        if prot.stop_price is None or self.portfolio.qty(key) == 0:
            return
        if prot.soft_stop and (close - prot.stop_price) * prot.direction <= 0:
            self._close_slice(key, "stop_loss", ts)
            return
        if prot.trailing_distance:
            candidate = close - prot.direction * prot.trailing_distance
            if (candidate - prot.stop_price) * prot.direction > 0:
                prot.stop_price = candidate
                if not prot.soft_stop:
                    self._cancel(prot.stop_order, ts)
                    prot.stop_order = self._protective_order(key, prot, OrderType.STOP, candidate, "trailing_stop", abs(self.portfolio.qty(key)), ts)
                    if prot.stop_order is None:
                        prot.soft_stop = True

    def _close_slice(self, key: Key, reason: str, ts: datetime) -> Order | None:
        qty = self.portfolio.qty(key)
        if qty == 0:
            return None
        broker_id, instrument_id, strategy = key
        inst = self.registry.get(instrument_id)
        oid = self._new_id()
        order = Order(id=oid, client_order_id=f"{oid}:{reason}", broker_id=broker_id, instrument_id=instrument_id,
                      side=Side.SELL if qty > 0 else Side.BUY, qty=abs(qty), reduce_only=True, strategy=strategy, reason=reason,
                      created_at=ts, updated_at=ts)
        intent = OrderIntent(order, inst, self.portfolio.prices[instrument_id], self._fx(inst.quote_currency, ts), None)
        if not self.risk.evaluate(intent, self.portfolio.state(ts)).approved:
            return self._reject(order, "risk engine refused a closing order", ts)
        return self._submit(order, ts)

    # ------------------------------------------------------------------ signals
    def on_signal(self, signal: Signal, dep: Deployment, mode_override: TradingMode | None = None) -> list[Order]:
        """Turn a strategy signal into orders according to the trading mode. Returns the orders created."""
        self.journal.signal(signal)
        mode = mode_override or self._mode()
        if mode is TradingMode.MANUAL:
            return []
        ts, key = signal.ts, dep.key
        inst = self.registry.get(dep.instrument_id)
        adapter = self.brokers.get(dep.broker_id)
        current = self.portfolio.qty(key)
        direction = (current > 0) - (current < 0)
        target = signal.target
        if target < 0 and not (inst.shortable and adapter.capabilities.supports_short):
            target = 0
        if target == direction:
            return []
        waiting = [a for a in self.approvals.values() if (a.order.broker_id, a.order.instrument_id, a.order.strategy) == key]
        if signal.action.value == "HOLD" and (direction == 0 or waiting):
            return []  # no fresh decision on this bar: keep whatever is already pending
        # A new signal supersedes approvals still waiting for the same slice.
        for approval in waiting:
            self.resolve_approval(approval.id, False, "system:superseded", ts)

        bar_tag = signal.meta.get("bar_open", ts.isoformat())
        created: list[Order] = []
        if direction != 0:
            oid = self._new_id()
            close = Order(id=oid, client_order_id=f"{dep.id}|{bar_tag}|close", broker_id=dep.broker_id, instrument_id=inst.id,
                          side=Side.SELL if current > 0 else Side.BUY, qty=abs(current), reduce_only=True, strategy=dep.strategy.name,
                          signal_id=signal.id, reason="signal", created_at=ts, updated_at=ts)
            intent = OrderIntent(close, inst, signal.price, self._fx(inst.quote_currency, ts), None)
            created.append(self._route(close, intent, None, mode, ts))
        # Entries are taken only on the bar where the strategy's target changes (never re-entered after a stop-out).
        if target != 0 and signal.action.value != "HOLD":
            state = self.portfolio.state(ts)
            if direction != 0:  # the slice being reversed is closed by the order above
                state.exposures = [e for e in state.exposures if (e.broker_id, e.instrument_id, e.strategy) != key]
            limits = self._limits()
            fx_rate = self._fx(inst.quote_currency, ts)
            cost = self._cost_per_unit(inst, signal.price)
            qty = position_size(state.equity, limits.risk_per_trade, signal.stop_distance or 0.0, signal.price, inst, fx_rate,
                                max_notional=self.risk.headroom(dep.broker_id, inst, dep.strategy.name, state),
                                realized_vol=self._realized_vol(inst.id), target_vol=limits.target_volatility, cost_per_unit=cost)
            if qty <= 0:
                self.journal.alert(ts, "sizing", "info", f"No position taken in {inst.id}",
                                   "the size allowed by the risk budget and exposure limits is below the minimum tradable quantity")
                return created
            oid = self._new_id()
            entry = Order(id=oid, client_order_id=f"{dep.id}|{bar_tag}|open", broker_id=dep.broker_id, instrument_id=inst.id,
                          side=Side.BUY if target > 0 else Side.SELL, qty=qty, strategy=dep.strategy.name, signal_id=signal.id,
                          reason="signal", created_at=ts, updated_at=ts)
            intent = OrderIntent(entry, inst, signal.price, fx_rate, signal.stop_distance, cost_per_unit=cost)
            protection = (signal.stop_distance, signal.take_profit_distance, signal.trailing_distance)
            self._rationale = {"strategy": dep.strategy.name, "strategy_version": dep.strategy.version, "category": dep.strategy.category,
                               "description": dep.strategy.description, "params": dep.strategy.params.model_dump(), "timeframe": dep.timeframe,
                               "signal_time": signal.ts.isoformat(), "signal_price": signal.price, "regime": signal.regime, "confidence": signal.confidence,
                               "deployment": dep.id, **{k: v for k, v in signal.meta.items() if k in ("opportunity", "validation", "data")}}
            created.append(self._route(entry, intent, protection, mode, ts, pending_close=direction != 0))
        return created

    def _route(self, order: Order, intent: OrderIntent, protection: tuple | None, mode: TradingMode, ts: datetime,
               pending_close: bool = False) -> Order:
        if order.client_order_id in self._client_ids:  # the same signal was already acted on
            return self.orders[self._client_ids[order.client_order_id]]
        state = self.portfolio.state(ts)
        if pending_close:
            # The slice being reversed is closed by the order just created; judge the entry against the book without it.
            key = (order.broker_id, order.instrument_id, order.strategy)
            state.exposures = [e for e in state.exposures if (e.broker_id, e.instrument_id, e.strategy) != key]
        decision = self.risk.evaluate(intent, state)
        if not decision.approved:
            self._client_ids[order.client_order_id] = order.id
            return self._reject(order, "; ".join(decision.reasons), ts)
        if mode is TradingMode.SEMI_AUTO or self._approval_required(order):
            self._client_ids[order.client_order_id] = order.id
            pending = order.model_copy(update={"status": OrderStatus.PENDING_APPROVAL})
            ttl = self._proposal_ttl()
            # An order that only reduces risk (closing a position) never lapses: letting it expire would leave the risk on.
            expires = (self._wall_clock() if self._wall_clock else ts) + ttl if ttl and not order.reduce_only else None
            approval = PendingApproval(self._new_id("a"), pending, intent, protection, ts, expires, dict(self._rationale))
            self._rationale = {}
            self.approvals[approval.id] = approval
            self.journal.alert(ts, "approval", "info", f"Approval needed: {order.side.value} {order.qty:g} {order.instrument_id}", order.strategy or "")
            return self._record(pending)
        if protection:
            self._entry_protection[order.id] = ((order.broker_id, order.instrument_id, order.strategy), protection)
        return self._submit(order, ts)

    def resolve_approval(self, approval_id: str, approve: bool, actor: str, ts: datetime) -> Order:
        approval = self.approvals.pop(approval_id, None)
        if approval is None:
            raise KeyError(f"No pending approval {approval_id}")
        order = approval.order.model_copy(update={"status": OrderStatus.NEW, "created_at": ts, "updated_at": ts})
        if approve and approval.expires_at is not None and ts > approval.expires_at:
            return self._record(order.model_copy(update={"status": OrderStatus.CANCELLED, "reason": f"{order.reason} | proposal expired before it was approved"}))
        if not approve:
            return self._record(order.model_copy(update={"status": OrderStatus.CANCELLED, "reason": f"{order.reason} | declined by {actor}"}))
        # Conditions may have changed while the order waited: the risk engine decides again, now.
        decision = self.risk.evaluate(replace(approval.intent, order=order), self.portfolio.state(ts))
        if not decision.approved:
            return self._reject(order, "; ".join(decision.reasons), ts)
        if approval.protection:
            self._entry_protection[order.id] = ((order.broker_id, order.instrument_id, order.strategy), approval.protection)
        del self._client_ids[order.client_order_id]
        return self._submit(order, ts)

    def expire_approvals(self, ts: datetime) -> list[Order]:
        """Proposals nobody decided on in time lapse. Nothing is sent."""
        out = []
        for a in [a for a in self.approvals.values() if a.expires_at is not None and ts > a.expires_at]:
            del self.approvals[a.id]
            # The client id stays registered: a proposal that lapsed is not made again for the same signal bar.
            out.append(self._record(a.order.model_copy(update={"status": OrderStatus.CANCELLED, "updated_at": ts, "reason": f"{a.order.reason} | proposal expired"})))
        return out

    # ------------------------------------------------------------------ submission
    def _submit(self, order: Order, ts: datetime) -> Order:
        """Send to the broker exactly once. Transient failures are retried with the same client id,
        after first asking the broker whether the earlier attempt actually arrived."""
        if order.client_order_id in self._client_ids:
            return self.orders[self._client_ids[order.client_order_id]]
        self._client_ids[order.client_order_id] = order.id
        self._record(order)
        try:
            adapter = self.brokers.get(order.broker_id)
        except BrokerError as exc:
            return self._reject(order, str(exc), ts)
        to_send = self._for_broker(order, adapter)
        last_error = ""
        for attempt in range(self.retry.max_attempts):
            try:
                return self._record(self.brokers.submit(to_send).model_copy(update={"reduce_only": order.reduce_only}))
            except RetryableBrokerError as exc:
                last_error = str(exc)
                try:
                    found = adapter.find_order(order.client_order_id, order.instrument_id)
                except BrokerError:
                    found = None
                if found is not None:
                    return self._record(found.model_copy(update={"id": order.id, "strategy": order.strategy, "signal_id": order.signal_id,
                                                                 "reason": order.reason, "reduce_only": order.reduce_only}))
                if attempt < self.retry.max_attempts - 1:
                    self._sleep(exc.retry_after or min(self.retry.backoff_seconds * 2 ** attempt, self.retry.max_backoff_seconds))
            except (UnsupportedOperation, OrderRejected) as exc:
                self._entry_protection.pop(order.id, None)
                return self._reject(order, str(exc), ts)
            except BrokerError as exc:
                self._entry_protection.pop(order.id, None)
                return self._reject(order, f"broker error: {exc}", ts)
        self._entry_protection.pop(order.id, None)
        self.journal.alert(ts, "broker", "critical", f"Broker {order.broker_id} unreachable", last_error)
        return self._reject(order, f"broker unreachable after {self.retry.max_attempts} attempts: {last_error}", ts, alert=False)

    def _for_broker(self, order: Order, adapter) -> Order:
        """`reduce_only` describes the order's role for OUR strategy slice. On a netting venue that enforces
        reduce-only natively, the flag is dropped when another strategy also holds the instrument there,
        because the venue would judge it against the combined net position instead of this slice."""
        caps = adapter.capabilities
        if not order.reduce_only or not caps.supports_reduce_only or caps.per_strategy_positions:
            return order
        shared = any(k[0] == order.broker_id and k[1] == order.instrument_id and k[2] != order.strategy for k in self.portfolio.slices)
        return order.model_copy(update={"reduce_only": False}) if shared else order

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        """Everything needed to resume after a restart without losing track of orders, stops or approvals."""
        keep = {o.id for o in self.orders.values() if o.is_open or o.status is OrderStatus.PENDING_APPROVAL}
        keep |= set(list(self.orders)[-1000:])
        return {
            "n": self._n, "orders": [self.orders[i].model_dump(mode="json") for i in self.orders if i in keep],
            "client_ids": dict(list(self._client_ids.items())[-20000:]),
            "entry_protection": [[oid, list(key), list(p)] for oid, (key, p) in self._entry_protection.items()],
            "protections": [[list(key), p.direction, p.stop_distance, p.take_profit_distance, p.trailing_distance, p.stop_price,
                             p.stop_order.id if p.stop_order else None, p.target_order.id if p.target_order else None, p.soft_stop]
                            for key, p in self.protections.items()],
            "approvals": [[a.id, a.order.model_dump(mode="json"), a.intent.price, a.intent.fx_rate, a.intent.stop_distance,
                           list(a.protection) if a.protection else None, a.created_at.isoformat(),
                           a.expires_at.isoformat() if a.expires_at else None, a.rationale, a.intent.cost_per_unit] for a in self.approvals.values()],
        }

    def load_state(self, state: dict) -> None:
        self._n = state["n"]
        self.orders = {o["id"]: Order.model_validate(o) for o in state["orders"]}
        self._client_ids = dict(state["client_ids"])
        self._entry_protection = {oid: (tuple(key), tuple(p)) for oid, key, p in state["entry_protection"]}
        self.protections = {}
        for key, direction, stop, target, trailing, stop_price, stop_id, target_id, soft in state["protections"]:
            self.protections[tuple(key)] = Protection(direction, stop, target, trailing, stop_price, self.orders.get(stop_id),
                                                      self.orders.get(target_id), soft)
        self.approvals = {}
        for aid, order, price, fx_rate, stop_distance, protection, created, *more in state["approvals"]:
            expires, rationale, cost = (more + [None, {}, 0.0])[:3] if more else (None, {}, 0.0)
            o = Order.model_validate(order)
            intent = OrderIntent(o, self.registry.get(o.instrument_id), price, fx_rate, stop_distance, cost_per_unit=cost or 0.0)
            self.approvals[aid] = PendingApproval(aid, o, intent, tuple(protection) if protection else None, datetime.fromisoformat(created),
                                                  datetime.fromisoformat(expires) if expires else None, rationale or {})

    # ------------------------------------------------------------------ emergency handling
    def engage_kill_switch(self, reason: str, actor: str, ts: datetime) -> None:
        self.execute_emergency(self.risk.engage_kill_switch(ts, reason, actor), ts)

    def execute_emergency(self, actions: list[EmergencyAction], ts: datetime) -> None:
        """Runs only the actions the user's emergency policy authorised."""
        protective = {o.id for p in self.protections.values() for o in (p.stop_order, p.target_order) if o}
        if EmergencyAction.CANCEL_OPEN_ORDERS in actions:
            for approval_id in list(self.approvals):
                self.resolve_approval(approval_id, False, "system:emergency", ts)
            for o in [o for o in self.orders.values() if o.is_open and o.id not in protective]:  # stops stay in place
                self._cancel(o, ts)
                self._entry_protection.pop(o.id, None)
        if EmergencyAction.FLATTEN_POSITIONS in actions:
            for key in list(self.portfolio.slices):
                if key in self.protections:
                    self._cancel_protection(key, ts)
                self._close_slice(key, "emergency", ts)
            self.journal.alert(ts, "risk", "critical", "Emergency policy: closing all positions", "")

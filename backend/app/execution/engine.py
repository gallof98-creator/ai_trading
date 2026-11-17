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
from ..risk.models import EmergencyAction, Exposure, OrderIntent, PortfolioState, RiskEventRecord
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


EXTERNAL_STRATEGY = "manual:broker"  # positions the user opened directly at a broker
EXTERNAL_CONFIRM_SECONDS = 20.0


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
        self._auto_exit: Callable[[Order], bool] = lambda order: False  # set by the runtime
        # Why an order may not be sent from this account at all (None = it may). Set by the runtime: entries resting on
        # weaker evidence than "validated" are refused on real-money accounts.
        self._entry_block: Callable[[Order], str | None] = lambda order: None
        self._wall_clock = wall_clock  # proposals expire in real time, whatever the timestamps of the bars that produced them
        self._rationale: dict = {}  # set while a signal is being turned into orders
        self._external_seen: dict[tuple[str, str], tuple[float, float, datetime]] = {}  # differences with the broker awaiting confirmation
        self._unconfirmed: dict[str, str] = {}  # order id -> when: sent, no answer, unknown whether the broker has it
        self._working: dict[str, float] = {}  # order id -> signed notional (base currency) of entries sent but not yet filled
        self.orders: dict[str, Order] = {}
        self._client_ids: dict[str, str] = {}  # client order id -> order id (duplicate-order guard)
        self._entry_protection: dict[str, tuple[Key, tuple]] = {}
        self.protections: dict[Key, Protection] = {}
        self.approvals: dict[str, PendingApproval] = {}
        self._n = 0

    def _risk_state(self, ts: datetime) -> PortfolioState:
        """The book the risk engine judges: open positions PLUS entry orders already sent and not yet filled.
        Without the second part, several orders sent before any of them fills could together exceed a limit."""
        state = self.portfolio.state(ts)
        for oid in list(self._working):
            o = self.orders.get(oid)
            if o is None or not o.is_open:
                del self._working[oid]
                continue
            remaining = self._working[oid] * max(0.0, 1 - o.filled_qty / o.qty)
            inst = self.registry.get(o.instrument_id)
            state.exposures.append(Exposure(o.broker_id, o.instrument_id, o.strategy, inst.asset_class.value, inst.quote_currency, remaining,
                                            abs(remaining) * inst.margin_rate))
        return state

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
        self.check_soft_exits(instrument_id, close, ts)  # targets the venue could not hold are watched on the close as well

    def end_of_timestamp(self, ts: datetime, reconcile: bool = False) -> None:
        """Call once after all bars closing at `ts`: revalue, run circuit breakers, apply the emergency policy."""
        if reconcile:
            self.sync_with_broker(ts)
            # A difference waiting for its confirmation as the user's own action at the broker is not a fault (for a few
            # minutes at most: one that is never confirmed becomes one).
            pending = {k for k, (_, _, seen) in self._external_seen.items() if ts - seen < timedelta(minutes=5)}
            # Nor is one on an instrument with one of our orders on its way: the broker may show the fill before we read it.
            protective = {o.id for p in self.protections.values() for o in (p.stop_order, p.target_order) if o is not None}
            pending |= {(o.broker_id, o.instrument_id) for o in self.orders.values()
                        if o.is_open and not o.is_simulated_venue and o.id not in protective}
            problems = self.portfolio.reconcile(pending)
            if problems:
                self.journal.alert(ts, "reconciliation", "critical", "Positions do not reconcile with the broker", "; ".join(problems))
        state = self.portfolio.revalue(ts)
        actions = self.risk.monitor(state)
        if actions:
            self.execute_emergency(actions, ts)

    def poll_orders(self, ts: datetime) -> None:
        """Real brokers: pull order updates and turn filled-quantity changes into fills."""
        if self._unconfirmed:
            self._resolve_unconfirmed(ts)
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
        if self.portfolio.qty(key) == 0:
            # A closing proposal still waiting for approval has nothing left to close (a stop or target got there first).
            for aid in [a.id for a in self.approvals.values() if a.order.reduce_only and (a.order.broker_id, a.order.instrument_id, a.order.strategy) == key]:
                gone = self.approvals.pop(aid)
                self._record(gone.order.model_copy(update={"status": OrderStatus.CANCELLED, "updated_at": fill.ts,
                                                           "reason": f"{gone.order.reason} | position already closed"}))

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

    def check_soft_exits(self, instrument_id: str, price: float, ts: datetime) -> list[Order]:
        """Stops and targets that could not be left resting at the broker (fractional shares, crypto at Alpaca, a target
        next to a stop on the same shares) are watched here: called on every price the platform receives for the
        instrument, it closes the position at market once the price is through the level."""
        out = []
        for key, prot in list(self.protections.items()):
            if key[1] != instrument_id or prot.stop_price is None or self.portfolio.qty(key) == 0:
                continue
            s = self.portfolio.slices[key]
            order = None
            if prot.soft_stop and (price - prot.stop_price) * prot.direction <= 0:
                order = self._close_slice(key, "stop_loss", ts)
            elif prot.take_profit_distance and prot.target_order is None and \
                    (price - (s.avg_price + prot.direction * prot.take_profit_distance)) * prot.direction >= 0:
                order = self._close_slice(key, "take_profit", ts)
            if order is not None:
                out.append(order)
        return out

    def unwatched_positions(self) -> list[Key]:
        """Positions whose stop or target depends on the platform seeing prices (nothing rests at the broker for them)."""
        return [key for key, prot in self.protections.items()
                if self.portfolio.qty(key) != 0 and (prot.soft_stop or (prot.take_profit_distance and prot.target_order is None))]

    def _close_slice(self, key: Key, reason: str, ts: datetime) -> Order | None:
        qty = self.portfolio.qty(key)
        if qty == 0:
            return None
        broker_id, instrument_id, strategy = key
        # A closing market order that is already on its way (e.g. sent after hours) is never sent a second time:
        # at a broker that does not enforce reduce-only, a duplicate could turn the position the other way.
        for o in self.orders.values():
            if o.is_open and o.reduce_only and o.type is OrderType.MARKET and (o.broker_id, o.instrument_id, o.strategy) == key:
                return o
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
            state = self._risk_state(ts)
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
                               "deployment": dep.id, **{k: v for k, v in signal.meta.items() if k in ("opportunity", "validation", "data", "fundamentals", "filters")}}
            created.append(self._route(entry, intent, protection, mode, ts, pending_close=direction != 0))
        return created

    def _route(self, order: Order, intent: OrderIntent, protection: tuple | None, mode: TradingMode, ts: datetime,
               pending_close: bool = False) -> Order:
        if order.client_order_id in self._client_ids:  # the same signal was already acted on
            return self.orders[self._client_ids[order.client_order_id]]
        blocked = None if order.reduce_only else self._entry_block(order)
        if not order.reduce_only and (order.broker_id, order.instrument_id) in self.unconfirmed_instruments():
            blocked = "an earlier order on this instrument may have reached the broker without an answer; no new entry until it is found or ruled out"
        if blocked:
            self._client_ids[order.client_order_id] = order.id
            return self._reject(order, blocked, ts)
        state = self._risk_state(ts)
        if pending_close:
            # The slice being reversed is closed by the order just created; judge the entry against the book without it.
            key = (order.broker_id, order.instrument_id, order.strategy)
            state.exposures = [e for e in state.exposures if (e.broker_id, e.instrument_id, e.strategy) != key]
        decision = self.risk.evaluate(intent, state)
        if not decision.approved:
            self._client_ids[order.client_order_id] = order.id
            return self._reject(order, "; ".join(decision.reasons), ts)
        if order.reduce_only and order.reason == "signal" and self._auto_exit(order):
            pass  # the user chose to let strategy exits close positions on paper accounts without asking
        elif mode is TradingMode.SEMI_AUTO or self._approval_required(order):
            self._client_ids[order.client_order_id] = order.id
            pending = order.model_copy(update={"status": OrderStatus.PENDING_APPROVAL})
            ttl = self._proposal_ttl()
            timeframe = self._rationale.get("timeframe")
            if ttl and timeframe:
                # A signal from a daily bar stays actionable far longer than one from a one-minute bar: the proposal lives
                # at least half a bar of its timeframe (never more than a day). Price and risk are re-checked on approval anyway.
                from ..market_data.timeframes import tf_delta

                ttl = max(ttl, min(tf_delta(timeframe).to_pytimedelta() / 2, timedelta(hours=24)))
            # An order that only reduces risk (closing a position) never lapses: letting it expire would leave the risk on.
            expires = (self._wall_clock() if self._wall_clock else ts) + ttl if ttl and not order.reduce_only else None
            approval = PendingApproval(self._new_id("a"), pending, intent, protection, ts, expires, dict(self._rationale))
            self._rationale = {}
            self.approvals[approval.id] = approval
            self.journal.alert(ts, "approval", "info", f"Approval needed: {order.side.value} {order.qty:g} {order.instrument_id}", order.strategy or "")
            return self._record(pending)
        if protection:
            self._entry_protection[order.id] = ((order.broker_id, order.instrument_id, order.strategy), protection)
        if decision.risk_increasing:
            self._working[order.id] = intent.signed_notional
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
        decision = self.risk.evaluate(replace(approval.intent, order=order), self._risk_state(ts))
        if not decision.approved:
            return self._reject(order, "; ".join(decision.reasons), ts)
        if approval.protection:
            self._entry_protection[order.id] = ((order.broker_id, order.instrument_id, order.strategy), approval.protection)
        if decision.risk_increasing:
            self._working[order.id] = approval.intent.signed_notional
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
        if order.reduce_only:
            covered = self._broker_holding(order, adapter, ts)
            if covered is not None and covered <= 0:
                # Nothing left to close at the broker (the user closed it there): sending would open the opposite position.
                self._entry_protection.pop(order.id, None)
                rejected = self._reject(order, "the position is no longer held at the broker (closed outside the platform); nothing was sent", ts)
                self.sync_with_broker(ts, only=(order.broker_id, order.instrument_id))
                return rejected
            if covered is not None and covered < order.qty - 1e-12:
                order = order.model_copy(update={"qty": covered, "reason": f"{order.reason or ''} | reduced to the {covered:g} held at the broker".strip(" |")})
                self._record(order)
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
                    return self._adopt(order, found, adapter, ts)
                if attempt < self.retry.max_attempts - 1:
                    self._sleep(exc.retry_after or min(self.retry.backoff_seconds * 2 ** attempt, self.retry.max_backoff_seconds))
            except (UnsupportedOperation, OrderRejected) as exc:
                if attempt > 0:
                    # A refusal of a RETRY may mean "this client id is already used": the first attempt did arrive (its
                    # answer was lost, and the lookup in between failed). Ask before believing the refusal.
                    try:
                        found = adapter.find_order(order.client_order_id, order.instrument_id)
                    except BrokerError:
                        found = None
                        self._unconfirmed[order.id] = ts.isoformat()  # looked up again later; no new entry meanwhile
                    if found is not None:
                        return self._adopt(order, found, adapter, ts)
                self._entry_protection.pop(order.id, None)
                return self._reject(order, str(exc), ts)
            except BrokerError as exc:
                self._entry_protection.pop(order.id, None)
                return self._reject(order, f"broker error: {exc}", ts)
        self._entry_protection.pop(order.id, None)
        self.journal.alert(ts, "broker", "critical", f"Broker {order.broker_id} unreachable: order outcome unknown",
                           f"{order.side.value} {order.qty:g} {order.instrument_id} may or may not have reached the broker. It is looked up on every "
                           f"order check; no new entry on this instrument is sent until it is. Last error: {last_error}")
        # The request may have arrived even though no answer came back. Until the broker says whether it has this client id,
        # the outcome is unknown: it is looked up again (see poll_orders) and the instrument takes no new entry meanwhile.
        self._unconfirmed[order.id] = ts.isoformat()
        return self._reject(order, f"broker unreachable after {self.retry.max_attempts} attempts, outcome unknown: {last_error}", ts, alert=False)

    def _broker_holding(self, order: Order, adapter, ts: datetime) -> float | None:
        """How much of the position this closing order reduces is actually held at the broker, in the order's direction;
        None when it need not or cannot be checked (simulated venue, venue enforcing reduce-only, positions unreadable)."""
        caps = adapter.capabilities
        if caps.is_simulated or caps.supports_reduce_only:
            return None
        try:
            if hasattr(adapter, "invalidate"):
                adapter.invalidate()  # the broker's current position, not a cached one
            held = sum(p.qty for p in adapter.get_positions() if p.instrument_id == order.instrument_id)
        except BrokerError:
            return None  # an exit is never held back because the positions could not be read
        if getattr(adapter, "stale", False):
            return None
        return max(0.0, held if order.side is Side.SELL else -held)

    def sync_with_broker(self, ts: datetime, only: tuple[str, str] | None = None, confirmed: bool = False, fresh: bool = False) -> list[str]:
        """Take into the books what the user did directly at a broker. A position closed or reduced there is booked as an
        exit ("closed_at_broker"), at the broker's own fill price when it can be found, and its stops, targets and closing
        proposals are withdrawn. A position opened there is adopted as a manual position (no stop is placed for it).
        Instruments with a platform order on its way are left for the next check. Returns what was taken over."""
        notes: list[str] = []
        for adapter in self.brokers.all():
            if adapter.capabilities.is_simulated or (only and adapter.broker_id != only[0]):
                continue
            try:
                if fresh and hasattr(adapter, "invalidate"):
                    adapter.invalidate()  # read the broker's positions now, not a cached copy
                if not adapter.is_connected() or getattr(adapter, "stale", False):
                    continue  # old figures could look like a change: nothing is taken over from them
                reported = {}
                for p in adapter.get_positions():
                    reported.setdefault(p.instrument_id, []).append(p)
            except BrokerError:
                continue
            internal: dict[str, float] = {}
            for (b, iid, _), sl in self.portfolio.slices.items():
                if b == adapter.broker_id:
                    internal[iid] = internal.get(iid, 0.0) + sl.qty
            protective = {o.id for p in self.protections.values() for o in (p.stop_order, p.target_order) if o is not None}
            in_flight = {o.instrument_id for o in self.orders.values() if o.is_open and o.broker_id == adapter.broker_id and o.id not in protective}
            for iid in sorted(set(internal) | set(reported)):
                if (only and iid != only[1]) or iid.startswith("UNMAPPED:") or iid in in_flight:
                    continue
                a = internal.get(iid, 0.0)
                b = sum(p.qty for p in reported.get(iid, []))
                inst = self.registry.get(iid)
                if abs(a - b) <= max(inst.lot_size / 2, 1e-9):
                    continue
                if inst.asset_class.value == "crypto" and 0 < a and 0 < a - b <= 0.01 * a:
                    continue  # the fee taken in coins: booked by the portfolio reconciliation
                same_side = a == 0 or b == 0 or (a > 0) == (b > 0)
                if not same_side:
                    continue  # reversed outside the platform: left as a discrepancy for a person to look at
                # Taken over only when the same difference is seen again at least EXTERNAL_CONFIRM_SECONDS later: a
                # broker's figures read a moment before one of our own fills must never be mistaken for the user's action.
                seen = self._external_seen.get((adapter.broker_id, iid))
                if confirmed:  # a person asked for it (resetting the reconciliation breaker), on figures just read
                    seen = (a, b, ts - timedelta(seconds=EXTERNAL_CONFIRM_SECONDS))
                if not seen or abs(seen[0] - a) > 1e-9 or abs(seen[1] - b) > 1e-9:
                    self._external_seen[(adapter.broker_id, iid)] = (a, b, ts)
                    continue
                if (ts - seen[2]).total_seconds() < EXTERNAL_CONFIRM_SECONDS:
                    continue
                self._external_seen.pop((adapter.broker_id, iid), None)
                if abs(b) < abs(a):
                    notes += self._book_external_close(adapter, iid, abs(a) - abs(b), 1 if a > 0 else -1, reported.get(iid, []), ts)
                else:
                    notes += self._adopt_external_position(adapter, iid, b - a, reported.get(iid, []), ts)
        return notes

    def _book_external_close(self, adapter, iid: str, qty: float, direction: int, positions: list, ts: datetime) -> list[str]:
        price, source = None, "estimated from the last known price"
        finder = getattr(adapter, "closed_outside", None)
        if finder is not None:
            try:
                found = finder(iid, "sell" if direction > 0 else "buy", set(self._client_ids))
            except BrokerError:
                found = None
            if found:
                price, source = found, "the broker's fill price"
        if price is None:
            price = next((p.last_price for p in positions if p.last_price), None) or self.portfolio.prices.get(iid)
        if not price:
            self.journal.alert(ts, "reconciliation", "critical", f"Position changed at {adapter.broker_id} for {iid}, price unknown",
                               "it could not be taken into the books; it stays a discrepancy")
            return []
        keys = sorted([k for k, sl in self.portfolio.slices.items() if k[0] == adapter.broker_id and k[1] == iid and sl.qty * direction > 0],
                      key=lambda k: abs(self.portfolio.slices[k].qty))
        left, notes = qty, []
        # Manual positions first, then the smallest strategy positions: a partial close outside the platform is most
        # likely the user's own position, and otherwise leaves the larger strategy positions intact.
        keys.sort(key=lambda k: not str(k[2]).startswith("manual"))
        for key in keys:
            if left <= 1e-12:
                break
            take = min(left, abs(self.portfolio.slices[key].qty))
            oid = self._new_id()
            order = Order(id=oid, client_order_id=f"{oid}:closed_at_broker", broker_id=adapter.broker_id, instrument_id=iid,
                          side=Side.SELL if direction > 0 else Side.BUY, qty=take, status=OrderStatus.FILLED, filled_qty=take, avg_fill_price=price,
                          reduce_only=True, strategy=key[2], is_simulated=False, reason="closed_at_broker", created_at=ts, updated_at=ts)
            self._client_ids[order.client_order_id] = oid
            self._record(order)
            self._apply_fill(Fill(order_id=oid, broker_id=adapter.broker_id, instrument_id=iid, side=order.side, qty=take, price=price,
                                  commission=0.0, ts=ts, strategy=key[2]), order)
            if self.portfolio.qty(key) != 0 and key in self.protections:
                self._place_protection(key, self.protections[key], ts)  # resize the stop to what is left
            left -= take
            notes.append(f"{iid} {key[2]}: {take:g} closed at {adapter.broker_id} ({source} {price:g})")
        if notes:
            self.journal.alert(ts, "reconciliation", "warning", f"Position closed outside the platform: {iid}", "; ".join(notes))
        return notes

    def _adopt_external_position(self, adapter, iid: str, qty: float, positions: list, ts: datetime) -> list[str]:
        price = next((p.avg_price for p in positions if p.avg_price), None) or self.portfolio.prices.get(iid)
        if not price:
            return []
        oid = self._new_id()
        order = Order(id=oid, client_order_id=f"{oid}:opened_at_broker", broker_id=adapter.broker_id, instrument_id=iid,
                      side=Side.BUY if qty > 0 else Side.SELL, qty=abs(qty), status=OrderStatus.FILLED, filled_qty=abs(qty), avg_fill_price=price,
                      strategy=EXTERNAL_STRATEGY, is_simulated=False, reason="opened_at_broker", created_at=ts, updated_at=ts)
        self._client_ids[order.client_order_id] = oid
        self._record(order)
        self._apply_fill(Fill(order_id=oid, broker_id=adapter.broker_id, instrument_id=iid, side=order.side, qty=abs(qty), price=price,
                              commission=0.0, ts=ts, strategy=EXTERNAL_STRATEGY), order)
        note = f"{iid}: {qty:g} opened at {adapter.broker_id} outside the platform, followed as a manual position (no stop placed)"
        self.journal.alert(ts, "reconciliation", "warning", f"Position opened outside the platform: {iid}", note)
        return [note]

    def _adopt(self, order: Order, found: Order, adapter, ts: datetime) -> Order:
        """The broker has this order although the request seemed to fail. It becomes ours under our id, and whatever it
        has already filled is booked as fills (recording it as already filled would leave the books without them)."""
        sent = order.model_copy(update={"status": OrderStatus.SUBMITTED, "broker_order_id": found.broker_order_id, "filled_qty": 0.0,
                                        "avg_fill_price": None, "commission": 0.0, "is_simulated": found.is_simulated, "updated_at": ts})
        self._record(sent)
        current = found.model_copy(update={"id": order.id, "strategy": order.strategy, "signal_id": order.signal_id, "reason": order.reason,
                                           "reduce_only": order.reduce_only, "client_order_id": order.client_order_id})
        self._ingest_update(adapter, sent, current, ts)
        return self.orders[order.id]

    def _resolve_unconfirmed(self, ts: datetime) -> None:
        for oid, since in list(self._unconfirmed.items()):
            order = self.orders.get(oid)
            if order is None:
                self._unconfirmed.pop(oid, None)
                continue
            try:
                adapter = self.brokers.get(order.broker_id)
                found = adapter.find_order(order.client_order_id, order.instrument_id)
            except BrokerError:
                continue  # still unknown: asked again next time
            if found is not None:
                self._adopt(order, found, adapter, ts)
                self.journal.alert(ts, "broker", "warning", f"Order found at {order.broker_id} after all: {order.side.value} {order.qty:g} {order.instrument_id}",
                                   "it is now followed as a normal order")
            self._unconfirmed.pop(oid, None)  # found, or the broker answered that it does not have it

    def unconfirmed_instruments(self) -> set[tuple[str, str]]:
        return {(self.orders[o].broker_id, self.orders[o].instrument_id) for o in self._unconfirmed if o in self.orders}

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
            "n": self._n, "working": dict(self._working), "unconfirmed": dict(self._unconfirmed), "orders": [self.orders[i].model_dump(mode="json") for i in self.orders if i in keep],
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
        self._working = dict(state.get("working", {}))
        self._unconfirmed = dict(state.get("unconfirmed", {}))
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

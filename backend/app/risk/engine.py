"""Centralised risk engine.

Every order passes through `evaluate` before it can reach a broker. The engine depends only on
domain models and user-configured limits - it imports nothing from the strategy, AI or ML
packages, so it keeps working when those are down, and none of them can change its limits.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from ..database.settings_store import RiskLimits
from .models import EmergencyAction, OrderIntent, PortfolioState, RiskDecision, RiskEventRecord

TOLERANCE = 1e-9
EventSink = Callable[[RiskEventRecord], None]


class RiskEngine:
    def __init__(self, limits: Callable[[], RiskLimits], sink: EventSink | None = None) -> None:
        self._limits = limits
        self._sink = sink or (lambda event: None)
        self.kill_switch_reason: str | None = None
        self.breakers: dict[str, dict] = {}  # name -> {"since", "detail"}
        self._daily_breaker_day: date | None = None

    # ------------------------------------------------------------------ state
    @property
    def kill_switch_on(self) -> bool:
        return self.kill_switch_reason is not None

    @property
    def halted(self) -> bool:
        return self.kill_switch_on or bool(self.breakers)

    def _emit(self, ts: datetime, kind: str, severity: str, **detail) -> None:
        self._sink(RiskEventRecord(ts, kind, severity, detail))

    def engage_kill_switch(self, ts: datetime, reason: str, actor: str) -> list[EmergencyAction]:
        """Global stop. Blocks all new risk immediately and returns the actions the configured
        emergency policy authorises. It never liquidates unless the user chose that policy."""
        self.kill_switch_reason = reason
        self._emit(ts, "kill_switch_on", "critical", reason=reason, actor=actor)
        return self._policy_actions()

    def release_kill_switch(self, ts: datetime, actor: str) -> None:
        self.kill_switch_reason = None
        self._emit(ts, "kill_switch_off", "warning", actor=actor)

    def reset_breaker(self, name: str, ts: datetime, actor: str) -> None:
        if self.breakers.pop(name, None) is not None:
            self._emit(ts, "breaker_reset", "warning", breaker=name, actor=actor)

    def _policy_actions(self) -> list[EmergencyAction]:
        policy = self._limits().emergency_policy
        if policy == "cancel_open_orders":
            return [EmergencyAction.CANCEL_OPEN_ORDERS]
        if policy == "flatten_positions":
            return [EmergencyAction.CANCEL_OPEN_ORDERS, EmergencyAction.FLATTEN_POSITIONS]
        return []

    # ------------------------------------------------------------------ circuit breakers
    def monitor(self, state: PortfolioState) -> list[EmergencyAction]:
        """Call on every portfolio revaluation. Trips circuit breakers and returns the emergency
        actions to execute (only on the transition into a tripped state)."""
        limits = self._limits()
        day = state.ts.date()
        # The daily-loss breaker belongs to one trading day and clears when the next one starts.
        if "daily_loss" in self.breakers and self._daily_breaker_day != day:
            self.breakers.pop("daily_loss")
            self._emit(state.ts, "breaker_reset", "info", breaker="daily_loss", actor="system:new_day")
        tripped = False
        if state.day_start_equity > 0:
            daily_loss = 1 - state.equity / state.day_start_equity
            if daily_loss >= limits.max_daily_loss - TOLERANCE and "daily_loss" not in self.breakers:
                self._trip("daily_loss", state.ts, loss=daily_loss, limit=limits.max_daily_loss)
                self._daily_breaker_day = day
                tripped = True
        if state.peak_equity > 0:
            drawdown = 1 - state.equity / state.peak_equity
            if drawdown >= limits.max_drawdown - TOLERANCE and "drawdown" not in self.breakers:
                self._trip("drawdown", state.ts, drawdown=drawdown, limit=limits.max_drawdown)
                tripped = True
        if not state.reconciled and "reconciliation" not in self.breakers:
            self._trip("reconciliation", state.ts, message="internal positions differ from broker-reported positions")
            tripped = True
        return self._policy_actions() if tripped else []

    def _trip(self, name: str, ts: datetime, **detail) -> None:
        self.breakers[name] = {"since": ts.isoformat(), **detail}
        self._emit(ts, "breaker_tripped", "critical", breaker=name, **detail)

    # ------------------------------------------------------------------ pre-trade check
    def evaluate(self, intent: OrderIntent, state: PortfolioState) -> RiskDecision:
        limits, order, inst = self._limits(), intent.order, intent.instrument
        current = state.net_for(order.broker_id, inst.id)
        delta = intent.signed_notional
        after = current + delta
        # Risk-reducing: the position shrinks towards zero without crossing it.
        reducing = order.reduce_only or (current != 0 and current * delta < 0 and abs(delta) <= abs(current) + TOLERANCE)
        if reducing:
            return RiskDecision(True, ["risk-reducing order"], risk_increasing=False)

        reasons: list[str] = []
        eq = state.equity
        if self.kill_switch_on:
            reasons.append(f"kill switch engaged: {self.kill_switch_reason}")
        for name in self.breakers:
            reasons.append(f"circuit breaker tripped: {name}")
        if eq <= 0:
            reasons.append("account equity is not positive")
        else:
            loss = intent.loss_at_stop
            if loss is None:
                reasons.append("order has no stop-loss distance")
            elif loss > limits.risk_per_trade * eq * (1 + 1e-6):
                reasons.append(f"risk at stop {loss / eq:.4%} exceeds risk per trade {limits.risk_per_trade:.4%}")

            def over(value: float, limit_fraction: float, label: str) -> None:
                if value > limit_fraction * eq * (1 + 1e-6):
                    reasons.append(f"{label} {value / eq:.2%} would exceed limit {limit_fraction:.2%}")

            added = abs(after) - abs(current)  # growth in absolute exposure caused by this order
            over(abs(after), limits.max_exposure_per_asset, f"exposure in {inst.id}")
            strat = order.strategy
            over(sum(abs(e.signed_notional) for e in state.exposures if e.strategy == strat) + added,
                 limits.max_exposure_per_strategy, f"exposure of strategy {strat}")
            over(sum(abs(e.signed_notional) for e in state.exposures if e.broker_id == order.broker_id) + added,
                 limits.max_exposure_per_broker, f"exposure at broker {order.broker_id}")
            over(state.gross_exposure + added, limits.max_gross_leverage, "gross exposure (leverage)")
            over(state.margin_used + added * inst.margin_rate, limits.max_margin_utilization, "margin utilisation")

            open_positions = {(e.broker_id, e.instrument_id) for e in state.exposures if e.signed_notional}
            if (order.broker_id, inst.id) not in open_positions and len(open_positions) >= limits.max_concurrent_positions:
                reasons.append(f"maximum of {limits.max_concurrent_positions} concurrent positions reached")

            # Correlated cluster: other positions that effectively point the same way as the new one.
            direction = 1 if after > 0 else -1
            cluster = abs(after)
            for e in state.exposures:
                if e.instrument_id == inst.id:
                    continue
                corr = state.correlation(inst.id, e.instrument_id)
                if abs(corr) >= limits.correlation_threshold and e.signed_notional * corr * direction > 0:
                    cluster += abs(e.signed_notional)
            if cluster > abs(after):
                over(cluster, limits.max_correlated_exposure, "correlated exposure")

        if reasons:
            self._emit(state.ts, "order_rejected", "warning", order_id=order.id, instrument_id=inst.id, strategy=order.strategy,
                       side=order.side.value, qty=order.qty, reasons=reasons)
        return RiskDecision(not reasons, reasons, risk_increasing=True)

    def headroom(self, broker_id: str, instrument, strategy: str | None, state: PortfolioState) -> float:
        """Largest additional notional (base currency) a new position in `instrument` may add without
        breaching any exposure limit. Used by position sizing; `evaluate` remains the final gate."""
        limits, eq = self._limits(), state.equity
        if eq <= 0:
            return 0.0
        room = [
            limits.max_exposure_per_asset * eq - abs(state.net_for(broker_id, instrument.id)),
            limits.max_exposure_per_strategy * eq - sum(abs(e.signed_notional) for e in state.exposures if e.strategy == strategy),
            limits.max_exposure_per_broker * eq - sum(abs(e.signed_notional) for e in state.exposures if e.broker_id == broker_id),
            limits.max_gross_leverage * eq - state.gross_exposure,
            (limits.max_margin_utilization * eq - state.margin_used) / instrument.margin_rate,
        ]
        return max(0.0, min(room)) * (1 - 1e-6)

    def status(self) -> dict:
        return {"halted": self.halted, "kill_switch": self.kill_switch_reason, "breakers": dict(self.breakers),
                "emergency_policy": self._limits().emergency_policy}

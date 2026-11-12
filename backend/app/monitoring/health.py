"""Operational monitors: broker connectivity and strategy performance changes."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..brokers.base import BrokerManager
from ..portfolio.engine import ClosedTrade

Alert = Callable[[datetime, str, str, str, str], None]


class BrokerMonitor:
    """Alerts on connectivity transitions (connected -> disconnected and back), not on every check."""

    def __init__(self, brokers: BrokerManager, alert: Alert) -> None:
        self.brokers, self._alert = brokers, alert
        self._last: dict[str, bool] = {}

    def check(self, ts: datetime) -> dict[str, bool]:
        status = {}
        for adapter in self.brokers.all():
            try:
                ok = bool(adapter.is_connected())
            except Exception:
                ok = False
            status[adapter.broker_id] = ok
            previous = self._last.get(adapter.broker_id)
            if previous is True and not ok:
                self._alert(ts, "broker", "critical", f"Broker disconnected: {adapter.broker_id}", "Orders cannot be routed until it reconnects.")
            elif previous is False and ok:
                self._alert(ts, "broker", "info", f"Broker reconnected: {adapter.broker_id}", "")
            self._last[adapter.broker_id] = ok
        return status


class StrategyPerformanceMonitor:
    """Flags a strategy whose most recent `window` trades lose money on average after the previous
    `window` made money (a simple, explainable change detector)."""

    def __init__(self, alert: Alert, window: int = 20) -> None:
        self._alert, self.window = alert, window
        self._flagged: dict[str, int] = {}

    def check(self, trades: list[ClosedTrade], ts: datetime) -> list[str]:
        by_strategy: dict[str, list[float]] = {}
        for t in trades:
            if t.strategy:
                by_strategy.setdefault(t.strategy, []).append(t.pnl)
        degraded = []
        for name, pnls in by_strategy.items():
            if len(pnls) < 2 * self.window:
                continue
            recent, before = pnls[-self.window:], pnls[-2 * self.window:-self.window]
            if sum(recent) < 0 < sum(before):
                degraded.append(name)
                if self._flagged.get(name) != len(pnls) // self.window:
                    self._flagged[name] = len(pnls) // self.window
                    self._alert(ts, "strategy", "warning", f"Performance change: {name}",
                                f"Last {self.window} trades: {sum(recent):.2f}; previous {self.window}: {sum(before):.2f} (base currency).")
        return degraded

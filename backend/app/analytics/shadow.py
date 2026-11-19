"""Shadow exits: for a trade closed by hand, the exit the system would have made.

The virtual position keeps the trade's entry, direction and quantity, and the stop, take profit and trailing distance it
had when it was closed by hand. It is walked forward bar by bar on market data until the first of:
  * the stop (a bar opening beyond it exits at that open: a gap);
  * the take profit (a bar opening beyond it exits at that open);
  * the strategy's own exit (its target is no longer the position's direction at a bar close), at that close;
  * `max_days` without any of them: closed at the last close, reason "expired".
Only bars that START at or after the manual close are used, so nothing before the decision is replayed. When the stop and
the target both lie inside one bar, the order inside the bar is unknown: `same_bar` decides (the stop first by default,
the conservative reading). The trailing stop moves at each bar close, as in live trading.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class ShadowResult:
    reason: str | None  # stop_loss | take_profit | signal | expired; None while still open
    price: float | None
    ts: pd.Timestamp | None
    bars: int


def simulate_exit(bars: pd.DataFrame, start: pd.Timestamp, direction: int, stop: float | None, target: float | None,
                  trailing: float | None = None, strategy_target: pd.Series | None = None, bar_delta: pd.Timedelta | None = None,
                  max_days: float = 60.0, same_bar: str = "stop_first", now: pd.Timestamp | None = None) -> ShadowResult:
    """Walk `bars` (OHLC indexed by bar start, UTC) from `start`. `strategy_target`: the strategy's target per bar (by bar
    start), evaluated at each bar's close. Returns the exit, or reason None while no exit has happened yet."""
    data = bars[bars.index >= start]
    stop_level = stop
    n = 0
    last_close = None
    for ts, row in data.iterrows():
        n += 1
        o, h, low, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        if direction > 0:
            stop_gap = stop_level is not None and o <= stop_level
            target_gap = target is not None and o >= target
            stop_hit = stop_level is not None and low <= stop_level
            target_hit = target is not None and h >= target
        else:
            stop_gap = stop_level is not None and o >= stop_level
            target_gap = target is not None and o <= target
            stop_hit = stop_level is not None and h >= stop_level
            target_hit = target is not None and low <= target
        if stop_gap:
            return ShadowResult("stop_loss", o, ts, n)
        if target_gap:
            return ShadowResult("take_profit", o, ts, n)
        if stop_hit and target_hit:
            return ShadowResult("stop_loss", stop_level, ts, n) if same_bar == "stop_first" else ShadowResult("take_profit", target, ts, n)
        if stop_hit:
            return ShadowResult("stop_loss", stop_level, ts, n)
        if target_hit:
            return ShadowResult("take_profit", target, ts, n)
        if strategy_target is not None and ts in strategy_target.index and int(strategy_target.loc[ts]) != direction:
            return ShadowResult("signal", c, ts + (bar_delta or pd.Timedelta(0)), n)
        if trailing:
            candidate = c - direction * trailing
            if stop_level is None or (candidate - stop_level) * direction > 0:
                stop_level = candidate
        last_close = (ts, c)
        if (ts - start) >= pd.Timedelta(days=max_days):
            return ShadowResult("expired", c, ts + (bar_delta or pd.Timedelta(0)), n)
    if now is not None and last_close is None and (now - start) >= pd.Timedelta(days=max_days):
        return ShadowResult("expired", None, None, 0)
    return ShadowResult(None, None, None, n)


def shadow_r(direction: int, entry: float, exit_price: float, qty: float, multiplier: float, risk_quote: float | None,
             costs_quote: float) -> float | None:
    """R of the virtual exit: price move on the same quantity, minus the trade's own costs, over its initial risk."""
    if not risk_quote:
        return None
    return ((exit_price - entry) * direction * qty * multiplier - costs_quote) / risk_quote

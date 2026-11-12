"""Signal engine: runs a strategy over completed bars and emits the Signal for the latest one."""
from __future__ import annotations

import pandas as pd

from ..domain.models import Signal, SignalAction
from ..strategies.base import Strategy


def _opt(value) -> float | None:
    return None if pd.isna(value) else float(value)


def signal_from_row(ts: pd.Timestamp, row, strategy: Strategy, instrument_id: str, timeframe: str, bar_close_time: pd.Timestamp) -> Signal:
    return Signal(
        ts=bar_close_time.to_pydatetime(), instrument_id=instrument_id, timeframe=timeframe, strategy=strategy.name,
        strategy_version=strategy.version, action=SignalAction(row.action), target=int(row.target), price=float(row.close),
        stop_distance=_opt(row.stop_distance), take_profit_distance=_opt(row.take_profit_distance),
        trailing_distance=_opt(row.trailing_distance),
        confidence=None,  # rule-based strategies have no calibrated probability; never invent one
        meta={"bar_open": ts.isoformat(), "atr": _opt(row.atr)},
    )


class SignalEngine:
    def latest(self, bars: pd.DataFrame, strategy: Strategy, instrument_id: str, timeframe: str) -> Signal | None:
        """Signal for the most recent COMPLETED bar in `bars`, or None while warming up.
        The signal is timestamped at that bar's close - the first moment it could be known."""
        from ..market_data.timeframes import tf_delta

        if len(bars) < strategy.warmup():
            return None
        frame = strategy.generate(bars)
        ts = frame.index[-1]
        return signal_from_row(ts, frame.iloc[-1], strategy, instrument_id, timeframe, ts + tf_delta(timeframe))

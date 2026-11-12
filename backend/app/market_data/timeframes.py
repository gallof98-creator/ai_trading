"""Timeframes, causal resampling and look-ahead-safe multi-timeframe alignment.

Convention: every bar is indexed by its OPEN time (UTC). A bar opened at T on timeframe D
is only known at T + D. All alignment helpers enforce that.
"""
from __future__ import annotations

import pandas as pd

TIMEFRAMES: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(weeks=1),
}
_PANDAS_RULE = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "W-MON"}
OHLCV = ["open", "high", "low", "close", "volume"]


def tf_delta(tf: str) -> pd.Timedelta:
    try:
        return TIMEFRAMES[tf]
    except KeyError:
        raise ValueError(f"Unsupported timeframe {tf!r}; supported: {list(TIMEFRAMES)}") from None


def resample(df: pd.DataFrame, source_tf: str, target_tf: str, drop_incomplete: bool = True) -> pd.DataFrame:
    """Aggregate OHLCV bars to a higher timeframe.

    With `drop_incomplete`, the trailing bar is removed unless the source data covers it fully,
    so a still-forming higher-timeframe bar never leaks into signals.
    """
    src, tgt = tf_delta(source_tf), tf_delta(target_tf)
    if tgt < src:
        raise ValueError("Cannot resample to a lower timeframe")
    if tgt == src:
        return df[OHLCV].copy()
    rule = _PANDAS_RULE[target_tf]
    out = (
        df[OHLCV]
        .resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )
    if drop_incomplete and len(out):
        last_source_close = df.index[-1] + src
        if out.index[-1] + tgt > last_source_close:
            out = out.iloc[:-1]
    return out


def align_higher_timeframe(ltf: pd.DataFrame, htf: pd.DataFrame, ltf_tf: str, htf_tf: str, prefix: str | None = None) -> pd.DataFrame:
    """Attach, to each lower-timeframe bar, the most recent higher-timeframe row that had
    already CLOSED by the time the lower-timeframe bar closed. Never looks ahead."""
    prefix = prefix or f"{htf_tf}_"
    left = pd.DataFrame({"_avail": ltf.index + tf_delta(ltf_tf)}, index=ltf.index)
    right = htf.add_prefix(prefix)
    right["_avail"] = htf.index + tf_delta(htf_tf)
    merged = pd.merge_asof(left.reset_index(names="_ts"), right.sort_values("_avail"), on="_avail", direction="backward")
    merged = merged.set_index("_ts").drop(columns="_avail")
    merged.index.name = ltf.index.name
    return ltf.join(merged)

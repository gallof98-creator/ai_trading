"""Feature engineering and labelling.

Features at bar t use bars <= t only. Labels look FORWARD by construction, so every label carries
the bar index at which it becomes known (`t1`); cross-validation purges on it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..strategies import indicators as ind


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    c, v = df["close"], df["volume"]
    ret = np.log(c).diff()
    mid, upper, lower = ind.bollinger(c, 20, 2.0)
    adx, plus, minus = ind.adx(df, 14)
    line, signal = ind.macd(c)
    atr14 = ind.atr(df, 14)
    f = pd.DataFrame(index=df.index)
    for n in (1, 5, 20, 60):
        f[f"ret_{n}"] = np.log(c / c.shift(n))
    f["vol_20"] = ret.rolling(20).std()
    f["vol_ratio_5_60"] = ret.rolling(5).std() / ret.rolling(60).std()
    f["rsi_14"] = ind.rsi(c, 14) / 100
    f["adx_14"] = adx / 100
    f["di_diff"] = (plus - minus) / 100
    f["atr_pct"] = atr14 / c
    f["bb_position"] = (c - lower) / (upper - lower).replace(0, np.nan)
    f["zscore_50"] = ind.zscore(c, 50)
    f["macd_hist"] = (line - signal) / c
    f["range_pct"] = (df["high"] - df["low"]) / c
    f["dist_high_50"] = c / df["high"].rolling(50).max() - 1
    f["dist_low_50"] = c / df["low"].rolling(50).min() - 1
    vol_sd = v.rolling(50).std()
    f["volume_z"] = ((v - v.rolling(50).mean()) / vol_sd.replace(0, np.nan)).fillna(0.0).where(vol_sd.notna())
    return f.replace([np.inf, -np.inf], np.nan)


def triple_barrier_labels(df: pd.DataFrame, horizon: int = 20, atr_multiple: float = 2.0, atr_period: int = 14) -> pd.DataFrame:
    """Label each bar 1 if price reaches +k*ATR before -k*ATR within `horizon` bars, 0 if the lower
    barrier comes first; if neither is hit, by the sign of the return at the horizon.
    Returns columns `label` and `t1` (integer position of the bar at which the label is decided).
    The last `horizon` bars cannot be labelled and are dropped."""
    close, high, low = (df[k].to_numpy() for k in ("close", "high", "low"))
    atr = ind.atr(df, atr_period).to_numpy()
    n = len(df)
    labels, t1 = np.full(n, np.nan), np.full(n, -1, dtype=int)
    for i in range(n - horizon):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        up, down = close[i] + atr_multiple * atr[i], close[i] - atr_multiple * atr[i]
        outcome, end = None, i + horizon
        for j in range(i + 1, i + horizon + 1):
            hit_up, hit_down = high[j] >= up, low[j] <= down
            if hit_up or hit_down:
                outcome, end = (0.0 if hit_down else 1.0), j  # both in one bar: assume the adverse one came first
                break
        labels[i] = outcome if outcome is not None else float(close[i + horizon] > close[i])
        t1[i] = end
    out = pd.DataFrame({"label": labels, "t1": t1}, index=df.index)
    return out[out["label"].notna()].astype({"label": int})


def make_dataset(df: pd.DataFrame, horizon: int = 20, atr_multiple: float = 2.0) -> tuple[pd.DataFrame, pd.Series, np.ndarray, np.ndarray]:
    """Aligned (features, labels, t0 positions, t1 positions) with incomplete rows removed."""
    features = build_features(df)
    labels = triple_barrier_labels(df, horizon, atr_multiple)
    position = pd.Series(np.arange(len(df)), index=df.index)
    keep = features.dropna().index.intersection(labels.index)
    return features.loc[keep], labels.loc[keep, "label"], position.loc[keep].to_numpy(), labels.loc[keep, "t1"].to_numpy()

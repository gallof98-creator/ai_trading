"""Technical indicators. Every function is causal: the value at bar t uses bars <= t only."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def roc(s: pd.Series, n: int) -> pd.Series:
    return s / s.shift(n) - 1


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain, loss = wilder(delta.clip(lower=0), n), wilder(-delta.clip(upper=0), n)
    out = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    return out.where(loss != 0, 100.0).where(gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.where(prev.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    return line, line.ewm(span=signal, adjust=False, min_periods=signal).mean()


def adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (ADX, +DI, -DI)."""
    up, down = df["high"].diff(), -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = wilder(true_range(df), n)
    plus_di, minus_di = 100 * wilder(plus_dm, n) / tr, 100 * wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx, n), plus_di, minus_di


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (middle, upper, lower)."""
    mid, sd = sma(close, n), close.rolling(n, min_periods=n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def zscore(s: pd.Series, n: int) -> pd.Series:
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return (s - sma(s, n)) / sd.replace(0, np.nan)


def prior_high(df: pd.DataFrame, n: int) -> pd.Series:
    """Highest high of the n bars BEFORE the current one (the current bar is excluded)."""
    return df["high"].rolling(n, min_periods=n).max().shift(1)


def prior_low(df: pd.DataFrame, n: int) -> pd.Series:
    return df["low"].rolling(n, min_periods=n).min().shift(1)


def confirmed_pivots(df: pd.DataFrame, k: int) -> tuple[pd.Series, pd.Series]:
    """Most recent swing high / swing low, each known only k bars after it formed.
    A swing high is a bar whose high is the maximum of the k bars either side of it."""
    window = 2 * k + 1
    centre_high, centre_low = df["high"].shift(k), df["low"].shift(k)
    is_high = centre_high == df["high"].rolling(window, min_periods=window).max()
    is_low = centre_low == df["low"].rolling(window, min_periods=window).min()
    return centre_high.where(is_high).ffill(), centre_low.where(is_low).ffill()


def latch(entry_long, exit_long, entry_short, exit_short) -> np.ndarray:
    """Position state machine: hold a position from its entry condition until its exit condition.
    Processes bars strictly in order, so the state at t depends on conditions at <= t only."""
    el, xl, es, xs = (np.asarray(pd.Series(a).fillna(False), dtype=bool) for a in (entry_long, exit_long, entry_short, exit_short))
    out = np.zeros(len(el), dtype=int)
    state = 0
    for i in range(len(el)):
        if state == 1 and xl[i]:
            state = 0
        elif state == -1 and xs[i]:
            state = 0
        if state == 0:
            if el[i] and not xl[i]:
                state = 1
            elif es[i] and not xs[i]:
                state = -1
        out[i] = state
    return out

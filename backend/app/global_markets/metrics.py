"""Daily metrics per instrument: the inputs of the scanner, the rankings and the market analysis.
Every value at date D uses bars up to and including D only."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..quant_ai.regime import detect_regimes
from ..strategies import indicators as ind

MIN_BARS = 30
LOOKBACK_BARS = 420  # enough for 12-month momentum, 200-day average and the regime's volatility rank

# name -> (label, type, description). The scanner accepts exactly these fields plus the derived ones below.
FIELDS: dict[str, tuple[str, str, str]] = {
    "close": ("Price", "number", "Last close, in the instrument's quote currency"),
    "change_1d": ("1-day change", "percent", "Close versus previous close"),
    "gap": ("Opening gap", "percent", "Open versus previous close"),
    "volume": ("Volume", "number", "Shares traded on the last bar"),
    "avg_volume_20": ("Average daily volume (20d)", "number", "Mean volume of the 20 bars before the last one"),
    "rel_volume": ("Relative volume", "number", "Last volume divided by the 20-day average; 2 means twice the usual"),
    "dollar_volume_20": ("Average traded value (20d)", "number", "Mean of close x volume over 20 bars, in quote currency"),
    "volatility_20": ("Volatility (20d, annualised)", "percent", "Standard deviation of daily log returns x sqrt(252)"),
    "atr_14": ("ATR (14)", "number", "Average true range"),
    "atr_pct": ("ATR % of price", "percent", "ATR(14) divided by the close"),
    "atr_ratio": ("Volatility expansion ratio", "number", "ATR(5) / ATR(40); above 1.25 means volatility is expanding"),
    "rsi_14": ("RSI (14)", "number", "Wilder's relative strength index"),
    "macd_hist": ("MACD histogram % of price", "percent", "(MACD line - signal) / close"),
    "dist_sma_20": ("Distance from 20-day average", "percent", "Close / SMA(20) - 1"),
    "dist_sma_50": ("Distance from 50-day average", "percent", "Close / SMA(50) - 1"),
    "dist_sma_200": ("Distance from 200-day average", "percent", "Close / SMA(200) - 1"),
    "adx_14": ("Trend strength ADX (14)", "number", "Above 25 indicates a trend"),
    "trend_up": ("Uptrend", "bool", "Close above SMA(50) and SMA(50) above SMA(200)"),
    "trend_down": ("Downtrend", "bool", "Close below SMA(50) and SMA(50) below SMA(200)"),
    "ret_5d": ("5-day return", "percent", ""), "ret_1m": ("1-month return", "percent", "21 trading days"),
    "ret_3m": ("3-month return", "percent", "63 trading days"), "ret_6m": ("6-month return", "percent", "126 trading days"),
    "ret_12m": ("12-month return", "percent", "252 trading days"),
    "mom_12_1": ("Momentum 12-1", "percent", "Return from 12 months ago to 1 month ago (skips the latest month)"),
    "dist_high_52w": ("Distance from 52-week high", "percent", "Close / highest high of 252 bars - 1"),
    "breakout_20": ("20-day breakout", "bool", "Close above the highest high of the previous 20 bars"),
    "breakout_55": ("55-day breakout", "bool", "Close above the highest high of the previous 55 bars"),
    "breakdown_20": ("20-day breakdown", "bool", "Close below the lowest low of the previous 20 bars"),
    "regime": ("Market regime", "text", "Headline regime from the regime detector"),
    "bars": ("History length", "number", "Stored daily bars"),
}
DERIVED: dict[str, tuple[str, str, str]] = {
    "rel_strength": ("Relative strength rank", "number", "Percentile (0-100) of the 6-month return among all instruments with metrics"),
    "group_ret_1m": ("Sector 1-month return", "percent", "Average 1-month return of the instrument's sector (needs sector data)"),
}
META: dict[str, tuple[str, str, str]] = {
    "market": ("Market", "text", ""), "country": ("Country", "text", ""), "currency": ("Currency", "text", ""),
    "asset_type": ("Instrument type", "text", ""), "sector": ("Sector", "text", ""), "industry": ("Industry", "text", ""),
}


def _f(x) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else float(x)


def compute_metrics(bars: pd.DataFrame) -> dict | None:
    """Metrics for the last bar of `bars`. Fields that need more history than is available are None."""
    if len(bars) < MIN_BARS:
        return None
    b = bars.iloc[-LOOKBACK_BARS:]
    c, h, l, o, v = b["close"], b["high"], b["low"], b["open"], b["volume"]
    n = len(b)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])

    def back(k: int) -> float | None:
        return float(c.iloc[-1 - k]) if n > k else None

    def ret(k: int) -> float | None:
        base = back(k)
        return last / base - 1 if base else None

    sma = {k: _f(c.rolling(k).mean().iloc[-1]) if n >= k else None for k in (20, 50, 200)}
    avg_vol = _f(v.iloc[-21:-1].mean()) if n >= 21 else None
    atr14, atr5, atr40 = (_f(ind.atr(b, k).iloc[-1]) if n > k else None for k in (14, 5, 40))
    line, signal = ind.macd(c)
    prior_high = {k: _f(h.iloc[-1 - k:-1].max()) if n > k else None for k in (20, 55)}
    prior_low20 = _f(l.iloc[-21:-1].min()) if n > 20 else None
    log_ret = np.log(c).diff()
    regime = detect_regimes(b)["primary"].iloc[-1]
    m12, m1 = back(252), back(21)
    out = {
        "close": last, "change_1d": last / prev - 1, "gap": float(o.iloc[-1]) / prev - 1, "volume": float(v.iloc[-1]), "avg_volume_20": avg_vol,
        "rel_volume": float(v.iloc[-1]) / avg_vol if avg_vol else None,
        "dollar_volume_20": _f((c * v).iloc[-20:].mean()) if n >= 20 else None,
        "volatility_20": _f(log_ret.iloc[-20:].std(ddof=1) * math.sqrt(252)) if n > 20 else None,
        "atr_14": atr14, "atr_pct": atr14 / last if atr14 else None, "atr_ratio": atr5 / atr40 if atr5 and atr40 else None,
        "rsi_14": _f(ind.rsi(c, 14).iloc[-1]), "macd_hist": _f((line.iloc[-1] - signal.iloc[-1]) / last) if n >= 35 else None,
        "dist_sma_20": last / sma[20] - 1 if sma[20] else None, "dist_sma_50": last / sma[50] - 1 if sma[50] else None,
        "dist_sma_200": last / sma[200] - 1 if sma[200] else None, "adx_14": _f(ind.adx(b, 14)[0].iloc[-1]),
        "trend_up": bool(sma[50] and sma[200] and last > sma[50] > sma[200]) if sma[200] else None,
        "trend_down": bool(sma[50] and sma[200] and last < sma[50] < sma[200]) if sma[200] else None,
        "ret_5d": ret(5), "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126), "ret_12m": ret(252),
        "mom_12_1": m1 / m12 - 1 if m12 and m1 else None,
        "dist_high_52w": last / float(h.iloc[-252:].max()) - 1 if n >= 252 else None,
        "breakout_20": last > prior_high[20] if prior_high[20] else None, "breakout_55": last > prior_high[55] if prior_high[55] else None,
        "breakdown_20": last < prior_low20 if prior_low20 else None,
        "regime": None if regime == "unknown" else str(regime), "bars": int(len(bars)),
    }
    return {k: (bool(x) if isinstance(x, (bool, np.bool_)) else _f(x) if isinstance(x, (int, float, np.floating)) and not isinstance(x, bool) else x)
            for k, x in out.items()}

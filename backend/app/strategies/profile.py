"""Per-asset-class trading rules applied on top of a strategy's signals (the crypto profile).

The same function transforms the strategy's signal frame in the validation backtest, the opportunity scan and live
trading, so what is tested is what is traded. Every rule is off unless the profile is enabled and the rule switched on.

Entry rules remove a whole position whose opening bar fails them (the strategy then waits for its next entry; it never
enters late). Exit rules end a position early. Nothing here uses a bar that was not complete at the decision:
  * regime: the trend of a reference instrument (bitcoin) on DAILY bars: close against its EMA, and optionally ADX above
    a threshold. A bar uses the regime of the last daily bar CLOSED before its own close;
  * volatility-normalised entry (mean reversion): the close must be at least k ATR away from its mean;
  * breakout confirmation: volume of the signal bar above m x its average (the strategies already require a close
    beyond the level);
  * mean-reversion exits: at the return to the mean (close crossing its moving average) and/or after N bars;
  * trend / breakout: partial take profit (fraction at +x R) and an ATR trailing stop on the rest (carried in the frame
    for the engine to manage);
  * limit (maker) entries with a fallback to market after N bars (carried in the frame for the engine).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind

REVERSION, TREND_LIKE, BREAKOUT = "mean_reversion", ("trend_following", "momentum", "breakout", "volatility"), "breakout"


def regime_daily(daily: pd.DataFrame, ema_period: int, adx_period: int, adx_min: float | None) -> pd.Series:
    """+1 strong uptrend, -1 strong downtrend, 0 otherwise, per daily bar (by bar START; known at that bar's close)."""
    close = daily["close"]
    ema = ind.ema(close, ema_period)
    direction = np.sign(close - ema).fillna(0)
    if adx_min:
        adx = ind.adx(daily, adx_period)[0]
        direction = direction.where(adx >= adx_min, 0)
    direction[ema.isna()] = 0
    return direction.astype(int)


def align_regime(regime: pd.Series, index: pd.DatetimeIndex, bar_delta: pd.Timedelta) -> pd.Series:
    """The regime each bar of `index` may use: that of the last daily bar closed at or before the bar's own close."""
    if regime is None or regime.empty:
        return pd.Series(0, index=index)
    known_at = regime.index + pd.Timedelta(days=1)  # a daily bar is known at its close
    closes = index + bar_delta
    pos = np.searchsorted(known_at.as_unit("ns").asi8, closes.as_unit("ns").asi8, side="right") - 1
    values = np.where(pos >= 0, regime.to_numpy()[np.clip(pos, 0, None)], 0)
    return pd.Series(values.astype(int), index=index)


def _runs(target: np.ndarray):
    k = 0
    while k < len(target):
        if target[k] == 0:
            k += 1
            continue
        end = k
        while end + 1 < len(target) and target[end + 1] == target[k]:
            end += 1
        yield k, end
        k = end + 1


def apply_profile(frame: pd.DataFrame, bars: pd.DataFrame, family: str, cfg, regime: pd.Series | None = None,
                  atr_period: int = 14) -> pd.DataFrame:
    """The strategy's frame with the profile's rules applied. `cfg`: a CryptoProfile (or any object with its fields);
    `regime`: per bar of `frame` (see align_regime), required by the regime filter."""
    if not getattr(cfg, "enabled", False):
        return frame
    out = frame.copy()
    target = out["target"].to_numpy(copy=True)
    close = bars["close"].reindex(out.index)
    atr = out["atr"] if "atr" in out else ind.atr(bars, atr_period).reindex(out.index)
    mean = ind.sma(close, cfg.mean_period)
    volume_ok = None
    if family == BREAKOUT and cfg.breakout_confirm and "volume" in bars:
        vol = bars["volume"].reindex(out.index)
        volume_ok = (vol >= cfg.breakout_volume_mult * vol.rolling(cfg.breakout_lookback, min_periods=cfg.breakout_lookback).mean().shift(1)).to_numpy()
    reg = regime.reindex(out.index, fill_value=0).to_numpy() if regime is not None else None
    dev = ((close - mean).abs() / atr).to_numpy()
    above_mean = (close - mean).to_numpy()
    for start, end in list(_runs(target)):
        side = int(np.sign(target[start]))
        blocked = False
        if family == REVERSION and reg is not None and cfg.regime_filter != "off" and reg[start] != 0:
            blocked = cfg.regime_filter == "disable" or side != reg[start]
        if family == REVERSION and cfg.vol_entry and not (np.isfinite(dev[start]) and dev[start] >= cfg.vol_entry_atr):
            blocked = True
        if volume_ok is not None and not bool(volume_ok[start]):
            blocked = True
        if blocked:
            target[start:end + 1] = 0
            continue
        if family == REVERSION:
            stop_at = end + 1
            if cfg.reversion_exit_at_mean:
                back = [j for j in range(start + 1, end + 1) if np.isfinite(above_mean[j]) and above_mean[j] * side >= 0]
                if back:
                    stop_at = min(stop_at, back[0])
            if cfg.time_stop_bars:
                stop_at = min(stop_at, start + cfg.time_stop_bars)
            target[stop_at:end + 1] = 0
    out["target"] = target
    change = out["target"].diff().fillna(out["target"])
    out["action"] = np.select([change > 0, change < 0], ["BUY", "SELL"], "HOLD")
    if family in TREND_LIKE and cfg.trend_partial:
        out["partial_fraction"] = cfg.partial_fraction
        out["partial_at_r"] = cfg.partial_at_r
        if cfg.trailing_atr:
            out["trailing_distance"] = atr * cfg.trailing_atr
    if cfg.maker_entries:
        out["maker_offset_bps"] = cfg.maker_offset_bps
        out["maker_fallback_bars"] = cfg.maker_fallback_bars
    return out

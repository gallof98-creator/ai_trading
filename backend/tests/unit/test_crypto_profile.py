"""Crypto profile: regime filter (no look-ahead), volatility-normalised entries, reversion exits, breakout confirmation,
partial take profit and trailing, limit entries with fallback, 24/7 sessions. Off by default; other assets untouched."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.database.settings_store import CryptoProfile
from app.market_data.calendars import get_calendar
from app.market_data.timeframes import resample
from app.strategies.profile import align_regime, apply_profile, regime_daily


def daily(closes, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="1D", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift().fillna(c.iloc[0]), "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0})


def test_regime_is_the_trend_of_the_reference_and_a_bar_only_uses_daily_bars_already_closed():
    up = daily(np.linspace(100, 200, 260))
    reg = regime_daily(up, ema_period=50, adx_period=14, adx_min=None)
    assert reg.iloc[:49].eq(0).all() and reg.iloc[-1] == 1
    down = daily(np.linspace(200, 100, 260))
    assert regime_daily(down, 50, 14, None).iloc[-1] == -1
    # Hourly bars of day D (before its close at midnight) see the regime of day D-1, never of day D itself.
    reg = pd.Series([1, -1], index=pd.DatetimeIndex(["2025-03-01", "2025-03-02"], tz="UTC"))
    hours = pd.date_range("2025-03-02 00:00", periods=25, freq="1h", tz="UTC")
    aligned = align_regime(reg, hours, pd.Timedelta(hours=1))
    assert aligned.iloc[0] == 1 and aligned.iloc[22] == 1 and aligned.iloc[23] == -1   # the 23:00 bar closes at midnight: day 2 known


def frame_for(closes, targets, atr=1.0):
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="1h", tz="UTC")
    bars = pd.DataFrame({"open": closes, "high": np.array(closes) + 0.5, "low": np.array(closes) - 0.5, "close": closes, "volume": 100.0}, index=idx)
    t = pd.Series(targets, index=idx)
    change = t.diff().fillna(t)
    frame = pd.DataFrame({"target": t, "action": np.select([change > 0, change < 0], ["BUY", "SELL"], "HOLD"), "close": bars["close"],
                          "atr": atr, "stop_distance": 2.0, "take_profit_distance": np.nan, "trailing_distance": np.nan}, index=idx)
    return frame, bars


def prof(**kw):
    return CryptoProfile(enabled=True, mean_period=3, **kw)


def test_disabled_profile_changes_nothing():
    frame, bars = frame_for([10, 9, 8, 9, 10, 11], [0, 1, 1, 1, 1, 0])
    assert apply_profile(frame, bars, "mean_reversion", CryptoProfile()).equals(frame)


def test_regime_filter_lets_mean_reversion_trade_only_with_the_trend_or_not_at_all():
    frame, bars = frame_for([10] * 8, [0, 1, 1, 0, -1, -1, 0, 0])
    down = pd.Series(-1, index=frame.index)
    only = apply_profile(frame, bars, "mean_reversion", prof(regime_filter="trend_only"), down)
    assert only["target"].tolist() == [0, 0, 0, 0, -1, -1, 0, 0]          # the long against the downtrend is removed
    off = apply_profile(frame, bars, "mean_reversion", prof(regime_filter="disable"), down)
    assert off["target"].eq(0).all()
    trend = apply_profile(frame, bars, "trend_following", prof(regime_filter="disable"), down)
    assert trend["target"].tolist() == frame["target"].tolist()            # only mean reversion is filtered


def test_mean_reversion_enters_only_far_enough_from_the_mean_in_atr_and_exits_at_the_mean_or_after_n_bars():
    frame, bars = frame_for([10, 10, 10, 7, 8, 9, 10.5, 11, 11], [0, 0, 0, 1, 1, 1, 1, 1, 0], atr=1.0)
    far = apply_profile(frame, bars, "mean_reversion", prof(vol_entry=True, vol_entry_atr=2.0))
    assert far["target"].tolist() == frame["target"].tolist()             # 7 is 2 ATR below its 3-bar mean of 9
    near = apply_profile(frame, bars, "mean_reversion", prof(vol_entry=True, vol_entry_atr=5.0))
    assert near["target"].eq(0).all()
    at_mean = apply_profile(frame, bars, "mean_reversion", prof(reversion_exit_at_mean=True))
    assert at_mean["target"].tolist() == [0, 0, 0, 1, 1, 0, 0, 0, 0]      # 9 >= mean(8, 9, 10 window) reached on bar 5
    timed = apply_profile(frame, bars, "mean_reversion", prof(time_stop_bars=2))
    assert timed["target"].tolist() == [0, 0, 0, 1, 1, 0, 0, 0, 0]
    assert timed["action"].tolist()[3] == "BUY" and timed["action"].tolist()[5] == "SELL"


def test_breakouts_need_volume_and_trend_trades_carry_partial_take_profit_and_trailing():
    frame, bars = frame_for([10] * 25, [0] * 22 + [1, 1, 1])
    bars.loc[bars.index[22], "volume"] = 120.0
    weak = apply_profile(frame, bars, "breakout", prof(breakout_confirm=True, breakout_volume_mult=1.5, breakout_lookback=20))
    assert weak["target"].eq(0).all()
    bars.loc[bars.index[22], "volume"] = 300.0
    strong = apply_profile(frame, bars, "breakout", prof(breakout_confirm=True, breakout_volume_mult=1.5, breakout_lookback=20))
    assert strong["target"].tolist() == frame["target"].tolist()
    t = apply_profile(frame, bars, "trend_following", prof(trend_partial=True, partial_fraction=0.5, partial_at_r=1.0, trailing_atr=3.0,
                                                           maker_entries=True, maker_offset_bps=10, maker_fallback_bars=2))
    assert (t["partial_fraction"] == 0.5).all() and (t["trailing_distance"] == 3.0).all() and (t["maker_offset_bps"] == 10).all()


def test_crypto_runs_around_the_clock_and_its_daily_bars_are_utc_days():
    cal = get_calendar("24x7")
    for when in ("2025-03-01 03:00", "2025-03-02 23:30", "2025-12-25 12:00"):   # Saturday night, Sunday, a holiday
        assert cal.is_open(pd.Timestamp(when, tz="UTC"))
    minutes = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                           index=pd.date_range("2025-03-01 00:00", periods=2 * 24 * 60, freq="1min", tz="UTC"))
    days = resample(minutes, "1m", "1d")
    assert list(days.index) == [pd.Timestamp("2025-03-01", tz="UTC"), pd.Timestamp("2025-03-02", tz="UTC")] and (days["volume"] == 1440).all()
    from app.instruments.registry import InstrumentRegistry, default_instruments
    from app.market_data.fx import FxRateProvider
    from app.paper_trading.broker import SimulatedBroker

    reg = InstrumentRegistry()
    for i in default_instruments():
        reg.register(i)
    sim = SimulatedBroker("p", reg, FxRateProvider(pegs={"USD": "EUR", "USDT": "EUR"}), 1000.0)
    crypto = next(i.id for i in reg.all() if i.asset_class.value == "crypto")
    stock = next(i.id for i in reg.all() if i.asset_class.value == "stock" and i.calendar != "24x7")
    sunday_night = pd.Timestamp("2025-03-02 03:00", tz="UTC").to_pydatetime()
    assert sim._session_open(crypto, sunday_night, intraday=True) and not sim._session_open(stock, sunday_night, intraday=True)

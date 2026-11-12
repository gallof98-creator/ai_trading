import numpy as np
import pandas as pd
import pytest

from app.domain.models import SignalAction
from app.market_data.sources import synthetic_bars
from app.signals.engine import SignalEngine
from app.strategies import indicators as ind
from app.strategies.base import Strategy
from app.strategies.registry import UnknownStrategyError, create_strategy, get_strategy_class, list_strategies, register

NAMES = [s["name"] for s in list_strategies()]
DATA = synthetic_bars("1h", 1500, seed=11).bars


def frame(close, high=None, low=None, volume=None):
    close = pd.Series(close, dtype=float, index=pd.date_range("2024-01-01", periods=len(close), freq="1h", tz="UTC"))
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "high": close + 0.5 if high is None else high,
                         "low": close - 0.5 if low is None else low, "close": close, "volume": 100.0 if volume is None else volume}, index=close.index)


# ---------- indicators against hand-computed values ----------
def test_sma_ema_roc():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ind.sma(s, 3).tolist()[2:] == [2, 3, 4] and ind.sma(s, 3).iloc[:2].isna().all()
    e = ind.ema(s, 3)  # alpha = 0.5, seeded with the first value
    assert e.iloc[2:].tolist() == pytest.approx([2.25, 3.125, 4.0625])
    assert ind.roc(s, 2).iloc[2:].tolist() == pytest.approx([2.0, 1.0, 2 / 3])


def test_rsi_extremes_and_known_value():
    assert ind.rsi(pd.Series(np.arange(1.0, 40)), 14).iloc[-1] == 100
    assert ind.rsi(pd.Series(np.arange(40.0, 1, -1)), 14).iloc[-1] == 0
    # Alternating +1/-1 moves: in steady state, right after a gain, Wilder's averages give RS = 14/13.
    alt = ind.rsi(pd.Series([10.0, 11] * 300), 14)
    assert alt.iloc[-1] == pytest.approx(100 * 14 / 27) and alt.iloc[-2] == pytest.approx(100 * 13 / 27)
    assert alt.dropna().between(0, 100).all()


def test_true_range_and_atr():
    df = pd.DataFrame({"high": [10.0, 12, 11], "low": [9.0, 10.5, 8], "close": [9.5, 11.5, 9]})
    assert ind.true_range(df).tolist()[1:] == [2.5, 3.5] and np.isnan(ind.true_range(df).iloc[0])
    flat = frame([100.0] * 60)
    assert ind.atr(flat, 14).iloc[-1] == pytest.approx(1.0)  # every bar's range is exactly 1


def test_bollinger_zscore_and_channels():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    mid, up, lo = ind.bollinger(s, 3, 2)
    sd = np.std([4, 5, 6])
    assert (mid.iloc[-1], up.iloc[-1], lo.iloc[-1]) == pytest.approx((5, 5 + 2 * sd, 5 - 2 * sd))
    assert ind.zscore(s, 3).iloc[-1] == pytest.approx(1 / sd)
    df = frame([1, 2, 3, 4, 5, 6])
    assert ind.prior_high(df, 3).iloc[-1] == 5.5 and ind.prior_low(df, 3).iloc[-1] == 2.5  # excludes the current bar


def test_adx_detects_trend_and_direction():
    up = frame(np.linspace(100, 200, 200))
    adx, plus, minus = ind.adx(up, 14)
    assert adx.iloc[-1] > 60 and plus.iloc[-1] > minus.iloc[-1]
    chop = frame(100 + np.tile([0.0, 1.0], 100))
    assert ind.adx(chop, 14)[0].iloc[-1] < 20


def test_pivots_are_only_known_after_confirmation():
    highs = pd.Series([1.0, 2, 5, 2, 1, 1, 1, 1])
    df = frame(highs - 0.5, high=highs.set_axis(frame(highs).index), low=(highs - 1).set_axis(frame(highs).index))
    resistance, _ = ind.confirmed_pivots(df, 2)
    assert resistance.iloc[:4].isna().all()  # peak at index 2 is unknown until 2 bars later
    assert resistance.iloc[4] == 5.0


def test_latch_state_machine():
    T, F = True, False
    out = ind.latch([T, F, F, F, F, F], [F, F, T, F, F, F], [F, F, F, F, T, F], [F, F, F, F, F, T])
    assert out.tolist() == [1, 1, 0, 0, -1, 0]
    assert ind.latch([T, T], [T, F], [F, F], [F, F]).tolist() == [0, 1]  # entry and exit on the same bar -> stay flat


# ---------- registry ----------
def test_library_contains_the_specified_strategies():
    expected = {"ema_crossover", "sma_crossover", "macd", "adx_trend", "rsi_momentum", "rate_of_change", "momentum_breakout",
                "bollinger_reversion", "rsi_reversion", "zscore_reversion", "atr_breakout", "volatility_expansion",
                "volatility_contraction", "donchian_breakout", "support_resistance_breakout", "volume_breakout"}
    assert set(NAMES) == expected | {"mtf_trend_filter"}
    assert {s["category"] for s in list_strategies()} == {"trend", "momentum", "mean_reversion", "volatility", "breakout", "multi_timeframe"}
    assert all(s["description"] and s["versions"] == ["1.0.0"] and "properties" in s["params_schema"] for s in list_strategies())


def test_parameter_validation_and_versioning():
    assert create_strategy("ema_crossover", {"fast": 5, "slow": 30}).params.fast == 5
    for bad in ({"fast": 50, "slow": 20}, {"fast": -1}, {"unknown_param": 1}, {"stop_atr": 0}):
        with pytest.raises(ValueError):
            create_strategy("ema_crossover", bad)
    with pytest.raises(UnknownStrategyError):
        create_strategy("nope")
    with pytest.raises(UnknownStrategyError):
        get_strategy_class("macd", "9.9.9")

    class V2(get_strategy_class("macd")):
        version = "1.1.0"

    register(V2)
    try:
        assert get_strategy_class("macd") is V2 and get_strategy_class("macd", "1.0.0") is not V2
        with pytest.raises(ValueError, match="already registered"):
            register(type("Clash", (get_strategy_class("macd", "1.0.0"),), {"version": "1.1.0"}))
    finally:
        from app.strategies import registry
        del registry._REGISTRY["macd"]["1.1.0"]


# ---------- behaviour ----------
def test_crossover_direction_on_obvious_trends():
    up, down = frame(np.linspace(100, 300, 300)), frame(np.linspace(300, 100, 300))
    for name in ("ema_crossover", "sma_crossover", "adx_trend", "rate_of_change"):
        s = create_strategy(name)
        assert s.generate(up).target.iloc[-1] == 1, name
        assert s.generate(down).target.iloc[-1] == -1, name
        assert create_strategy(name, {"allow_short": False}).generate(down).target.min() == 0


def test_donchian_entry_and_exit_bars():
    close = [10.0] * 30 + [12.0] * 5 + [8.0] * 5
    sig = create_strategy("donchian_breakout", {"entry": 20, "exit": 10, "take_profit_atr": None}).generate(frame(close))
    assert sig.target.iloc[29] == 0 and sig.target.iloc[30] == 1 and sig.action.iloc[30] == "BUY"
    assert sig.target.iloc[35] == -1 and sig.action.iloc[35] == "SELL"  # close below prior lows: exit and reverse
    assert (sig.action.iloc[31:35] == "HOLD").all()
    assert sig.take_profit_distance.isna().all() and (sig.stop_distance.dropna() > 0).all()


def test_mean_reversion_fades_extremes_and_exits_at_mean():
    close = [100.0 + (i % 2) * 0.2 for i in range(60)] + [90.0] + [100.0] * 5
    sig = create_strategy("bollinger_reversion").generate(frame(close))
    assert sig.target.iloc[59] == 0 and sig.target.iloc[60] == 1  # close far below the lower band -> long
    assert sig.target.iloc[61] == 0  # back at the mean -> flat
    z = create_strategy("zscore_reversion").generate(frame(close[:60] + [110.0] + [100.0] * 5))
    assert z.target.iloc[60] == -1


def test_volume_breakout_needs_volume_confirmation():
    close = [10.0] * 30 + [12.0] * 5
    quiet = create_strategy("volume_breakout").generate(frame(close))
    loud = create_strategy("volume_breakout").generate(frame(close, volume=pd.Series([100.0] * 30 + [400.0] * 5).to_numpy()))
    assert quiet.target.max() == 0 and loud.target.iloc[30] == 1


def test_no_position_without_a_stop_distance():
    sig = create_strategy("rate_of_change", {"period": 1, "threshold": 0, "atr_period": 50}).generate(frame(np.linspace(1, 2, 60)))
    assert (sig.target.iloc[:50] == 0).all() and sig.target.iloc[-1] == 1


# ---------- the core guarantee: no look-ahead ----------
@pytest.mark.parametrize("name", NAMES)
def test_signals_do_not_change_when_future_bars_are_removed(name):
    """A signal at bar t computed on bars[:t+1] must equal the signal at t computed on the full history."""
    strategy = create_strategy(name, {"trailing_atr": 3.0})
    full = strategy.generate(DATA)
    assert set(full.target.unique()) <= {-1, 0, 1}
    assert full.target.abs().sum() > 0, "strategy never trades on the test data"
    for cut in (400, 777, 1203, 1499):
        part = strategy.generate(DATA.iloc[:cut])
        pd.testing.assert_frame_equal(part, full.iloc[:cut], check_exact=False, rtol=1e-9)


@pytest.mark.parametrize("name", NAMES)
def test_signals_do_not_react_to_future_price_changes(name):
    strategy = create_strategy(name)
    tampered = DATA.copy()
    tampered.iloc[900:, :4] *= 3.0
    tampered.iloc[900:, 4] *= 10
    pd.testing.assert_frame_equal(strategy.generate(DATA).iloc[:900], strategy.generate(tampered).iloc[:900])


def test_signal_engine_emits_at_bar_close_without_invented_confidence():
    engine, strategy = SignalEngine(), create_strategy("ema_crossover", {"fast": 5, "slow": 20})
    assert engine.latest(DATA.iloc[:10], strategy, "X", "1h") is None  # warming up
    sig = engine.latest(DATA.iloc[:500], strategy, "X", "1h")
    assert sig.ts == (DATA.index[499] + pd.Timedelta(hours=1)).to_pydatetime()
    assert sig.confidence is None and sig.action in set(SignalAction) and sig.stop_distance > 0
    assert sig.strategy == "ema_crossover" and sig.strategy_version == "1.0.0"


def test_abstract_contract():
    with pytest.raises(TypeError):
        Strategy()


def test_multi_timeframe_filter_only_keeps_trades_aligned_with_completed_higher_timeframe_trend():
    from app.market_data.timeframes import align_higher_timeframe, resample

    s = create_strategy("mtf_trend_filter", {"base": "ema_crossover", "base_params": {"fast": 5, "slow": 20}, "filter_timeframe": "4h"})
    base = create_strategy("ema_crossover", {"fast": 5, "slow": 20}).generate(DATA).target
    filtered = s.generate(DATA).target
    assert filtered.abs().sum() > 0 and (filtered.abs() <= base.abs()).all()
    htf = resample(DATA, "1h", "4h")
    trend = np.sign(ind.ema(htf.close, 10) - ind.ema(htf.close, 30)).rename("trend").to_frame()
    aligned = align_higher_timeframe(DATA[["close"]], trend, "1h", "4h", prefix="h_")["h_trend"]
    held = filtered != 0
    assert (filtered[held] == aligned[held]).all()
    for bad in ({"filter_timeframe": "7h"}, {"base": "nope"}, {"base": "mtf_trend_filter"}, {"base_params": {"fast": 99, "slow": 3}}):
        with pytest.raises(ValueError):
            create_strategy("mtf_trend_filter", bad)
    with pytest.raises(ValueError, match="higher than the chart"):
        create_strategy("mtf_trend_filter", {"filter_timeframe": "1h"}).generate(DATA)

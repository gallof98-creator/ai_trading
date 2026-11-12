"""The built-in strategy library (16 strategies in 5 families)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from . import indicators as ind
from .base import Strategy, StrategyParams
from .registry import register

sign = np.sign


class _FastSlow(StrategyParams):
    fast: int = Field(20, ge=2, le=400)
    slow: int = Field(50, ge=3, le=1000)

    @model_validator(mode="after")
    def _order(self):
        if self.fast >= self.slow:
            raise ValueError("fast must be smaller than slow")
        return self


# ------------------------------------------------------------------ trend following
@register
class EmaCrossover(Strategy):
    name, category = "ema_crossover", "trend"
    description = "Long while the fast EMA is above the slow EMA, short while below."
    Params = _FastSlow

    def target(self, df):
        return sign(ind.ema(df.close, self.params.fast) - ind.ema(df.close, self.params.slow))


@register
class SmaCrossover(Strategy):
    name, category = "sma_crossover", "trend"
    description = "Long while the fast SMA is above the slow SMA, short while below."
    Params = _FastSlow

    def target(self, df):
        return sign(ind.sma(df.close, self.params.fast) - ind.sma(df.close, self.params.slow))


@register
class Macd(Strategy):
    name, category = "macd", "trend"
    description = "Long while the MACD line is above its signal line, short while below."

    class Params(StrategyParams):
        fast: int = Field(12, ge=2, le=200)
        slow: int = Field(26, ge=3, le=400)
        signal: int = Field(9, ge=2, le=100)

    def target(self, df):
        line, sig = ind.macd(df.close, self.params.fast, self.params.slow, self.params.signal)
        return sign(line - sig)


@register
class AdxTrend(Strategy):
    name, category = "adx_trend", "trend"
    description = "Trades in the direction of +DI/-DI only while ADX shows a trend; flat otherwise."

    class Params(StrategyParams):
        period: int = Field(14, ge=5, le=100)
        threshold: float = Field(25.0, ge=5, le=60)

    def target(self, df):
        adx, plus, minus = ind.adx(df, self.params.period)
        return sign(plus - minus).where(adx > self.params.threshold, 0)


# ------------------------------------------------------------------ momentum
@register
class RsiMomentum(Strategy):
    name, category = "rsi_momentum", "momentum"
    description = "Enters long when RSI rises above the upper level (short below the lower level); exits at the midline."

    class Params(StrategyParams):
        period: int = Field(14, ge=2, le=100)
        upper: float = Field(60.0, gt=50, lt=100)
        lower: float = Field(40.0, gt=0, lt=50)

    def target(self, df):
        r = ind.rsi(df.close, self.params.period)
        return ind.latch(r > self.params.upper, r < 50, r < self.params.lower, r > 50)


@register
class RateOfChange(Strategy):
    name, category = "rate_of_change", "momentum"
    description = "Long when the n-bar rate of change exceeds the threshold, short when below its negative."

    class Params(StrategyParams):
        period: int = Field(20, ge=1, le=500)
        threshold: float = Field(0.02, ge=0, le=1)

    def target(self, df):
        r = ind.roc(df.close, self.params.period)
        return np.where(r > self.params.threshold, 1, np.where(r < -self.params.threshold, -1, 0))


@register
class MomentumBreakout(Strategy):
    name, category = "momentum_breakout", "momentum"
    description = "Enters when the close makes a new n-bar closing high (low) with positive (negative) momentum; exits on a cross of the exit SMA."

    class Params(StrategyParams):
        lookback: int = Field(50, ge=5, le=500)
        momentum: int = Field(10, ge=1, le=200)
        exit_sma: int = Field(20, ge=2, le=300)

    def target(self, df):
        p, c = self.params, df.close
        hi = c.rolling(p.lookback, min_periods=p.lookback).max().shift(1)
        lo = c.rolling(p.lookback, min_periods=p.lookback).min().shift(1)
        mom, exit_ma = ind.roc(c, p.momentum), ind.sma(c, p.exit_sma)
        return ind.latch((c > hi) & (mom > 0), c < exit_ma, (c < lo) & (mom < 0), c > exit_ma)


# ------------------------------------------------------------------ mean reversion
@register
class BollingerReversion(Strategy):
    name, category = "bollinger_reversion", "mean_reversion"
    description = "Buys closes below the lower band and sells closes above the upper band; exits at the middle band."

    class Params(StrategyParams):
        period: int = Field(20, ge=5, le=300)
        width: float = Field(2.0, gt=0.5, le=5)

    def target(self, df):
        mid, upper, lower = ind.bollinger(df.close, self.params.period, self.params.width)
        return ind.latch(df.close < lower, df.close >= mid, df.close > upper, df.close <= mid)


@register
class RsiReversion(Strategy):
    name, category = "rsi_reversion", "mean_reversion"
    description = "Buys oversold RSI and sells overbought RSI; exits at the exit level."

    class Params(StrategyParams):
        period: int = Field(14, ge=2, le=100)
        oversold: float = Field(30.0, gt=0, lt=50)
        overbought: float = Field(70.0, gt=50, lt=100)
        exit_level: float = Field(50.0, gt=20, lt=80)

    def target(self, df):
        p, r = self.params, ind.rsi(df.close, self.params.period)
        return ind.latch(r < p.oversold, r >= p.exit_level, r > p.overbought, r <= p.exit_level)


@register
class ZScoreReversion(Strategy):
    name, category = "zscore_reversion", "mean_reversion"
    description = "Fades closes more than `entry` standard deviations from the rolling mean; exits inside `exit` deviations."

    class Params(StrategyParams):
        period: int = Field(30, ge=5, le=500)
        entry: float = Field(2.0, gt=0.5, le=6)
        exit: float = Field(0.5, ge=0, le=3)

    def target(self, df):
        p, z = self.params, ind.zscore(df.close, self.params.period)
        return ind.latch(z < -p.entry, z >= -p.exit, z > p.entry, z <= p.exit)


# ------------------------------------------------------------------ volatility
@register
class AtrBreakout(Strategy):
    name, category = "atr_breakout", "volatility"
    description = "Enters when the close moves more than k ATRs away from its EMA (Keltner breakout); exits on a cross back through the EMA."

    class Params(StrategyParams):
        period: int = Field(20, ge=5, le=300)
        multiplier: float = Field(2.0, gt=0.25, le=10)

    def target(self, df):
        p = self.params
        mid, band = ind.ema(df.close, p.period), ind.atr(df, p.period) * p.multiplier
        return ind.latch(df.close > mid + band, df.close < mid, df.close < mid - band, df.close > mid)


@register
class VolatilityExpansion(Strategy):
    name, category = "volatility_expansion", "volatility"
    description = "When short-term ATR expands relative to long-term ATR, follows the direction of the recent move; exits when volatility normalises."

    class Params(StrategyParams):
        short: int = Field(5, ge=2, le=100)
        long: int = Field(50, ge=10, le=500)
        ratio: float = Field(1.5, gt=1, le=5)
        direction: int = Field(5, ge=1, le=100)

    def target(self, df):
        p = self.params
        ratio = ind.atr(df, p.short) / ind.atr(df, p.long)
        move = df.close - df.close.shift(p.direction)
        calm = ratio < 1.0
        return ind.latch((ratio > p.ratio) & (move > 0), calm, (ratio > p.ratio) & (move < 0), calm)


@register
class VolatilityContraction(Strategy):
    name, category = "volatility_contraction", "volatility"
    description = "Squeeze breakout: after Bollinger band width falls into its lowest quantile, trades the first close outside the bands; exits at the middle band."

    class Params(StrategyParams):
        period: int = Field(20, ge=5, le=300)
        width: float = Field(2.0, gt=0.5, le=5)
        lookback: int = Field(120, ge=20, le=1000)
        quantile: float = Field(0.2, gt=0, lt=0.5)
        memory: int = Field(10, ge=1, le=100, description="Bars for which a squeeze stays armed")

    def target(self, df):
        p = self.params
        mid, upper, lower = ind.bollinger(df.close, p.period, p.width)
        bw = (upper - lower) / mid
        squeezed = bw <= bw.rolling(p.lookback, min_periods=p.lookback).quantile(p.quantile)
        armed = squeezed.astype(float).rolling(p.memory, min_periods=1).max().shift(1) > 0
        return ind.latch(armed & (df.close > upper), df.close < mid, armed & (df.close < lower), df.close > mid)


# ------------------------------------------------------------------ breakout
@register
class DonchianBreakout(Strategy):
    name, category = "donchian_breakout", "breakout"
    description = "Enters on a close beyond the prior n-bar high/low; exits on a close beyond the prior exit-channel low/high."

    class Params(StrategyParams):
        entry: int = Field(20, ge=2, le=500)
        exit: int = Field(10, ge=1, le=500)

    def target(self, df):
        p, c = self.params, df.close
        return ind.latch(c > ind.prior_high(df, p.entry), c < ind.prior_low(df, p.exit),
                         c < ind.prior_low(df, p.entry), c > ind.prior_high(df, p.exit))


@register
class SupportResistanceBreakout(Strategy):
    name, category = "support_resistance_breakout", "breakout"
    description = "Enters on a close through the most recent confirmed swing high (resistance) or swing low (support); exits on a close back through the opposite level."

    class Params(StrategyParams):
        pivot: int = Field(5, ge=2, le=50, description="Bars either side that define a swing point")
        buffer: float = Field(0.001, ge=0, le=0.05, description="Fraction beyond the level required to confirm")

    def target(self, df):
        p, c = self.params, df.close
        resistance, support = ind.confirmed_pivots(df, p.pivot)
        return ind.latch(c > resistance * (1 + p.buffer), c < support, c < support * (1 - p.buffer), c > resistance)


@register
class VolumeBreakout(Strategy):
    name, category = "volume_breakout", "breakout"
    description = "Donchian breakout that is only taken when volume exceeds a multiple of its recent average."

    class Params(StrategyParams):
        entry: int = Field(20, ge=2, le=500)
        exit: int = Field(10, ge=1, le=500)
        volume_period: int = Field(20, ge=2, le=300)
        volume_multiple: float = Field(1.5, ge=1, le=10)

    def target(self, df):
        p, c = self.params, df.close
        confirmed = df.volume > p.volume_multiple * ind.sma(df.volume, p.volume_period).shift(1)
        return ind.latch((c > ind.prior_high(df, p.entry)) & confirmed, c < ind.prior_low(df, p.exit),
                         (c < ind.prior_low(df, p.entry)) & confirmed, c > ind.prior_high(df, p.exit))


# ------------------------------------------------------------------ multi-timeframe
@register
class MultiTimeframeTrendFilter(Strategy):
    name, category = "mtf_trend_filter", "multi_timeframe"
    description = ("Runs any library strategy on the chart timeframe but only keeps positions that agree with the EMA trend of a "
                   "higher timeframe. Only COMPLETED higher-timeframe bars are used.")

    class Params(StrategyParams):
        base: str = Field("donchian_breakout", description="Strategy that generates entries on the chart timeframe")
        base_params: dict = Field(default_factory=dict)
        filter_timeframe: str = Field("4h", description="Higher timeframe used for the trend filter")
        filter_fast: int = Field(10, ge=2, le=200)
        filter_slow: int = Field(30, ge=3, le=500)

        @model_validator(mode="after")
        def _check(self):
            from ..market_data.timeframes import TIMEFRAMES
            from .registry import create_strategy

            if self.filter_timeframe not in TIMEFRAMES:
                raise ValueError(f"unknown timeframe {self.filter_timeframe}")
            if self.filter_fast >= self.filter_slow:
                raise ValueError("filter_fast must be smaller than filter_slow")
            if self.base == "mtf_trend_filter":
                raise ValueError("base strategy cannot be the filter itself")
            try:
                create_strategy(self.base, self.base_params)  # validates the nested configuration
            except KeyError as exc:
                raise ValueError(str(exc)) from None
            return self

    def _chart_timeframe(self, df: pd.DataFrame) -> str:
        from ..market_data.timeframes import TIMEFRAMES

        step = pd.Series(df.index).diff().min()
        for tf, delta in TIMEFRAMES.items():
            if delta == step:
                return tf
        raise ValueError(f"Cannot determine the chart timeframe from bar spacing {step}")

    def warmup(self) -> int:
        from ..market_data.timeframes import tf_delta

        return 3 * self.params.filter_slow * int(tf_delta(self.params.filter_timeframe) / pd.Timedelta(minutes=1)) + 5

    def target(self, df):
        from ..market_data.timeframes import align_higher_timeframe, resample, tf_delta
        from .registry import create_strategy

        p = self.params
        base = pd.Series(np.asarray(create_strategy(p.base, p.base_params).target(df)), index=df.index).fillna(0)
        if len(df) < 2:
            return base * 0
        tf = self._chart_timeframe(df)
        if tf_delta(p.filter_timeframe) <= tf_delta(tf):
            raise ValueError("filter_timeframe must be higher than the chart timeframe")
        htf = resample(df, tf, p.filter_timeframe)
        trend = sign(ind.ema(htf.close, p.filter_fast) - ind.ema(htf.close, p.filter_slow)).rename("trend").to_frame()
        aligned = align_higher_timeframe(df[["close"]], trend, tf, p.filter_timeframe, prefix="htf_")["htf_trend"]
        return base.where(base == aligned, 0)

"""Market regime detection. Rule-based statistics (transparent, causal) plus an optional
Gaussian-mixture clusterer for unsupervised regime discovery."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

from ..strategies import indicators as ind

TREND = ("strong_uptrend", "strong_downtrend", "sideways")
VOL_LEVEL = ("high_volatility", "low_volatility", "normal_volatility")
VOL_DYNAMICS = ("volatility_expansion", "volatility_contraction", "stable_volatility")


def detect_regimes(df: pd.DataFrame, adx_period: int = 14, adx_threshold: float = 25.0, vol_window: int = 20, rank_window: int = 250,
                   high_quantile: float = 0.8, low_quantile: float = 0.2, expansion_ratio: float = 1.25, contraction_ratio: float = 0.8) -> pd.DataFrame:
    """Classify every bar on three independent axes, using only data up to that bar:

    trend         strong_uptrend / strong_downtrend (ADX above threshold, direction from +DI vs -DI) / sideways
    vol_level     high / low / normal: realised volatility's percentile rank within the trailing `rank_window` bars
    vol_dynamics  expansion / contraction / stable: short-term ATR relative to longer-term ATR
    `primary` is one headline label: the trend if there is one, else the volatility dynamics, else the level.
    """
    adx, plus, minus = ind.adx(df, adx_period)
    trending = adx > adx_threshold
    trend = np.select([trending & (plus > minus), trending & (minus > plus)], TREND[:2], TREND[2])
    vol = np.log(df["close"]).diff().rolling(vol_window, min_periods=vol_window).std()
    rank = vol.rolling(rank_window, min_periods=max(vol_window * 2, rank_window // 4)).rank(pct=True)
    level = np.select([rank >= high_quantile, rank <= low_quantile], VOL_LEVEL[:2], VOL_LEVEL[2])
    ratio = ind.atr(df, 5) / ind.atr(df, vol_window * 2)
    dynamics = np.select([ratio >= expansion_ratio, ratio <= contraction_ratio], VOL_DYNAMICS[:2], VOL_DYNAMICS[2])
    out = pd.DataFrame({"trend": trend, "vol_level": level, "vol_dynamics": dynamics, "adx": adx, "realized_vol": vol,
                        "vol_rank": rank, "atr_ratio": ratio}, index=df.index)
    out["primary"] = np.where(out["trend"] != "sideways", out["trend"],
                              np.where(out["vol_dynamics"] != "stable_volatility", out["vol_dynamics"],
                                       np.where(out["vol_level"] != "normal_volatility", out["vol_level"], "sideways")))
    known = adx.notna() & rank.notna() & ratio.notna()
    out.loc[~known, ["trend", "vol_level", "vol_dynamics", "primary"]] = "unknown"
    return out


def regime_summary(df: pd.DataFrame) -> dict:
    """Current regime and how often each regime occurred in the sample."""
    r = detect_regimes(df)
    known = r[r["primary"] != "unknown"]
    last = r.iloc[-1]
    return {
        "current": {k: last[k] for k in ("trend", "vol_level", "vol_dynamics", "primary")},
        "as_of": r.index[-1].isoformat(),
        "indicators": {k: (None if pd.isna(last[k]) else float(last[k])) for k in ("adx", "realized_vol", "vol_rank", "atr_ratio")},
        "distribution": {k: float(v) for k, v in known["primary"].value_counts(normalize=True).items()} if len(known) else {},
        "bars_in_current_regime": int((r["primary"] != last["primary"]).iloc[::-1].cumsum().eq(0).sum()),
    }


class RegimeClusterer:
    """Unsupervised regimes via a Gaussian mixture on (return, volatility, trend strength).
    Fitted on a training window only; `predict` then labels later bars without refitting."""

    def __init__(self, n_regimes: int = 3, window: int = 20, seed: int = 0) -> None:
        self.n_regimes, self.window, self.seed = n_regimes, window, seed
        self._scaler, self._model = StandardScaler(), None

    def _features(self, df: pd.DataFrame) -> pd.DataFrame:
        ret = np.log(df["close"]).diff()
        return pd.DataFrame({"ret": ret.rolling(self.window).mean(), "vol": ret.rolling(self.window).std(),
                             "adx": ind.adx(df, 14)[0]}, index=df.index).dropna()

    def fit(self, train: pd.DataFrame) -> "RegimeClusterer":
        x = self._features(train)
        self._model = GaussianMixture(self.n_regimes, covariance_type="full", random_state=self.seed, n_init=3)
        self._model.fit(self._scaler.fit_transform(x))
        # Order cluster ids by volatility so that label 0 is always the calmest regime.
        order = np.argsort(self._scaler.inverse_transform(self._model.means_)[:, 1])
        self._relabel = {int(old): new for new, old in enumerate(order)}
        return self

    def predict(self, df: pd.DataFrame) -> pd.Series:
        if self._model is None:
            raise RuntimeError("Clusterer is not fitted")
        x = self._features(df)
        return pd.Series([self._relabel[int(c)] for c in self._model.predict(self._scaler.transform(x))], index=x.index, name="regime")

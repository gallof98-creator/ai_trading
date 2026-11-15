"""Statistics that separate a real edge from luck, for results measured on unseen data.

Three questions, three tools:

* Could this Sharpe ratio have come from a strategy with no edge?  -> `sharpe_p_value` (probabilistic Sharpe ratio,
  Bailey and Lopez de Prado 2012: it accounts for the length of the sample and for skewed, fat-tailed returns).
* Many strategy / instrument / timeframe combinations were tested. How many of the "winners" are flukes?
  -> `benjamini_hochberg` keeps the expected share of false discoveries among the accepted ones below a chosen rate.
* How good does the best of N worthless strategies look?  -> `expected_max_sharpe` (the benchmark of the deflated
  Sharpe ratio, Bailey and Lopez de Prado 2014). Reported next to a result so its size can be judged.

Everything here works on the Sharpe ratio per observation (not annualised) and needs no external library.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329
MIN_OBSERVATIONS = 30  # below this a Sharpe ratio is not tested at all: the answer is "not enough evidence"


def return_stats(returns) -> dict:
    """Summary of a return series: observations, Sharpe per observation, skewness, kurtosis (3 = normal) and the
    one-sided p-value of "the true Sharpe ratio is zero or negative"."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = int(len(r))
    out = {"observations": n, "sharpe_per_period": None, "skew": None, "kurtosis": None, "p_value": None}
    if n < 3:
        return out
    sd = float(r.std(ddof=1))
    if sd <= 0:
        return out
    mean = float(r.mean())
    z = (r - mean) / float(r.std(ddof=0))
    sharpe, skew, kurt = mean / sd, float((z ** 3).mean()), float((z ** 4).mean())
    out.update(sharpe_per_period=sharpe, skew=skew, kurtosis=kurt, p_value=sharpe_p_value(sharpe, n, skew, kurt))
    return out


def sharpe_p_value(sharpe: float, n: int, skew: float = 0.0, kurtosis: float = 3.0, benchmark: float = 0.0) -> float | None:
    """One-sided p-value that the true Sharpe ratio is not above `benchmark` (1 - probabilistic Sharpe ratio).
    None when the sample is too short to say anything."""
    if n < MIN_OBSERVATIONS or sharpe is None or not math.isfinite(sharpe):
        return None
    variance = 1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe ** 2
    if variance <= 0:
        return None
    return 1 - _N.cdf((sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(variance))


def benjamini_hochberg(p_values: dict, rate: float) -> set:
    """The keys accepted when the expected share of false discoveries among them is held at `rate`.
    A missing p-value (None) counts as a test that was run and never accepted."""
    m = len(p_values)
    ranked = sorted((p, k) for k, p in p_values.items() if p is not None)
    cut = 0
    for i, (p, _) in enumerate(ranked, start=1):
        if p <= rate * i / m:
            cut = i
    return {k for _, k in ranked[:cut]}


def expected_max_sharpe(trials: int, sharpe_std: float) -> float:
    """The Sharpe ratio (per observation) that the best of `trials` strategies WITHOUT any edge is expected to show,
    given how much the measured Sharpe ratios vary across the trials."""
    if trials < 2 or not sharpe_std or sharpe_std <= 0:
        return 0.0
    return sharpe_std * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / trials) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (trials * math.e)))

"""The statistics behind strategy promotion: significance, multiple testing, robustness."""
import math

import numpy as np
import pandas as pd
import pytest

from app.backtesting.stats import benjamini_hochberg, expected_max_sharpe, return_stats, sharpe_p_value
from app.backtesting.walk_forward import WalkForwardSpec, stressed, walk_forward
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset, synthetic_bars
from app.paper_trading.broker import ExecutionModel

BTC = "CRYPTO:BTC/USDT"


def test_p_value_shrinks_with_more_evidence_and_grows_with_fat_tails():
    # The same Sharpe ratio per day is weak evidence on 60 days and strong evidence on 1,000.
    assert sharpe_p_value(0.1, 60) > 0.2 and sharpe_p_value(0.1, 1000) < 0.001
    assert sharpe_p_value(0.0, 500) == pytest.approx(0.5) and sharpe_p_value(-0.1, 500) > 0.95
    # Negative skew and fat tails make the same Sharpe ratio less certain.
    assert sharpe_p_value(0.1, 250, skew=-1.5, kurtosis=9) > sharpe_p_value(0.1, 250)
    assert sharpe_p_value(0.1, 250) == pytest.approx(1 - 0.5 * (1 + math.erf(0.1 * math.sqrt(249) / math.sqrt(1 + 0.005) / math.sqrt(2))))
    assert sharpe_p_value(0.5, 29) is None  # too few observations: no verdict, not a pass


def test_return_stats_on_a_known_series():
    rng = np.random.default_rng(4)
    noise = return_stats(rng.normal(0, 0.01, 2000))
    edge = return_stats(rng.normal(0.002, 0.01, 2000))
    assert noise["observations"] == 2000 and abs(noise["sharpe_per_period"]) < 0.06 and noise["p_value"] > 0.01
    assert edge["sharpe_per_period"] == pytest.approx(0.2, abs=0.05) and edge["p_value"] < 1e-6 and edge["kurtosis"] == pytest.approx(3, abs=0.4)
    assert return_stats([0.01, 0.01, 0.01])["p_value"] is None and return_stats([])["observations"] == 0  # constant or empty: nothing to test


def test_false_discovery_control_rejects_what_plain_thresholds_accept():
    """200 strategies without any edge and 5 real ones. A plain 5% threshold lets about ten flukes through; the
    false-discovery control keeps the real ones and (almost) none of the flukes."""
    rng = np.random.default_rng(11)
    p = {f"noise{i}": float(v) for i, v in enumerate(rng.uniform(0, 1, 200))}
    p.update({f"real{i}": 1e-5 * (i + 1) for i in range(5)})
    naive = {k for k, v in p.items() if v <= 0.05}
    accepted = benjamini_hochberg(p, 0.10)
    assert len([k for k in naive if k.startswith("noise")]) >= 5
    assert {k for k in accepted if k.startswith("real")} == {f"real{i}" for i in range(5)}
    assert len([k for k in accepted if k.startswith("noise")]) <= 1
    # More tests make the bar higher for the same p-value; a missing p-value counts as a test and is never accepted.
    assert benjamini_hochberg({"a": 0.04}, 0.10) == {"a"}
    assert benjamini_hochberg({"a": 0.04, **{f"n{i}": None for i in range(9)}}, 0.10) == set()
    assert benjamini_hochberg({}, 0.10) == set()


def test_the_best_of_many_worthless_strategies_looks_good():
    one, many = expected_max_sharpe(1, 0.05), expected_max_sharpe(1000, 0.05)
    assert one == 0.0 and many == pytest.approx(0.05 * 3.26, rel=0.02) and expected_max_sharpe(10, 0.05) < expected_max_sharpe(100, 0.05) < many
    # Simulation: the largest of N Sharpe ratios drawn without any edge is close to the formula.
    rng = np.random.default_rng(2)
    best = np.mean([rng.normal(0, 0.05, 200).max() for _ in range(400)])
    assert expected_max_sharpe(200, 0.05) == pytest.approx(best, rel=0.05)


def test_stressed_costs_multiply_every_cost_and_leave_the_original_alone():
    base = ExecutionModel()
    hard = stressed(base, 2.0)
    assert (hard.spread_bps, hard.slippage_bps) == (2 * base.spread_bps, 2 * base.slippage_bps) and hard.participation_rate == base.participation_rate
    assert all(hard.commissions[k].rate == 2 * c.rate and hard.commissions[k].per_unit == 2 * c.per_unit for k, c in base.commissions.items())
    assert ExecutionModel().spread_bps == base.spread_bps


def spec(data, **kw):
    base = dict(strategy="ema_crossover", instrument_id=BTC, timeframe="1h", data=data, initial_capital=50_000.0, n_folds=3,
                param_grid={"fast": [5, 10, 20], "slow": [30, 60]}, static_fx={"USDT": 1.0}, min_trades=5)
    return WalkForwardSpec(**{**base, **kw})


def test_walk_forward_reports_significance_and_robustness_without_changing_the_basic_result():
    data = synthetic_bars("1h", 4000, seed=21)
    plain, robust = walk_forward(spec(data)), walk_forward(spec(data, robustness=True))
    assert "robustness" not in plain["summary"] and plain["folds"] == robust["folds"]  # the extra runs change nothing that was reported before
    assert plain["summary"]["out_of_sample_metrics"] == robust["summary"]["out_of_sample_metrics"]
    stats, r = robust["summary"]["out_of_sample_statistics"], robust["summary"]["robustness"]
    assert stats == plain["summary"]["out_of_sample_statistics"] and stats["observations"] > 60 and 0 <= stats["p_value"] <= 1
    assert r["parameter_runs"] == 6 * robust["summary"]["folds_with_parameters"] and 0 <= r["profitable_parameter_share"] <= 1
    # Doubling the costs can only make the same trades worse.
    assert r["return_with_stressed_costs"] <= robust["summary"]["out_of_sample_metrics"]["total_return"] + 1e-9 and r["cost_multiplier"] == 2.0
    assert r["net_profit_without_best_trade"] == pytest.approx(r["net_profit"] - r["best_trade"]) and r["best_trade"] >= 0 or r["net_profit"] <= 0


def test_a_strategy_without_an_edge_is_not_significant():
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.standard_normal(4000) * 0.005))  # driftless random walk
    idx = pd.date_range("2023-01-01", periods=4000, freq="1h", tz="UTC", name="ts")
    bars = pd.DataFrame({"open": np.concatenate([[100], close[:-1]]), "close": close, "volume": 1e9}, index=idx)
    bars["high"], bars["low"] = bars[["open", "close"]].max(axis=1) * 1.001, bars[["open", "close"]].min(axis=1) * 0.999
    out = walk_forward(spec(Dataset(bars, DataKind.SIMULATED, "noise"), robustness=True))
    p = out["summary"].get("out_of_sample_statistics", {}).get("p_value")
    assert p is None or p > 0.05

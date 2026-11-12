import numpy as np
import pandas as pd
import pytest

from app.backtesting.engine import BacktestSpec, DeploymentSpec
from app.backtesting.walk_forward import WalkForwardSpec, compare_strategies, walk_forward
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset, synthetic_bars

BTC = "CRYPTO:BTC/USDT"
DATA = synthetic_bars("1h", 4000, seed=21)


def wf(**kw):
    base = dict(strategy="ema_crossover", instrument_id=BTC, timeframe="1h", data=DATA, initial_capital=50_000.0, n_folds=3,
                param_grid={"fast": [5, 10, 20], "slow": [30, 60]}, static_fx={"USDT": 1.0}, min_trades=5)
    return WalkForwardSpec(**{**base, **kw})


def test_walk_forward_folds_are_chronological_and_never_overlap_train_and_test():
    out = walk_forward(wf())
    assert out["combinations_tested"] == 6 and len(out["folds"]) == 3
    for f in out["folds"]:
        assert pd.Timestamp(f["train_start"]) < pd.Timestamp(f["train_end"]) < pd.Timestamp(f["test_start"]) <= pd.Timestamp(f["test_end"])
    tests = [(pd.Timestamp(f["test_start"]), pd.Timestamp(f["test_end"])) for f in out["folds"]]
    assert all(a[1] < b[0] for a, b in zip(tests, tests[1:]))  # test windows do not overlap
    assert tests[-1][1] == DATA.bars.index[-1]
    eq = out["out_of_sample_equity"]
    assert eq and pd.Timestamp(eq[0]["ts"]) > pd.Timestamp(out["folds"][0]["train_end"])  # OOS curve contains no in-sample period
    assert "walk_forward_efficiency" in out["summary"] or out["summary"]["folds_with_parameters"] == 0
    assert out["data_kind"] == "simulated" and "do not guarantee" in out["disclaimer"]


def test_walk_forward_is_reproducible_and_selection_cannot_see_test_data():
    a, b = walk_forward(wf()), walk_forward(wf())
    assert a == b
    tampered = DATA.bars.copy()
    first_test_start = pd.Timestamp(a["folds"][0]["test_start"])
    tampered.loc[tampered.index >= first_test_start, ["open", "high", "low", "close"]] *= 1.5
    c = walk_forward(wf(data=Dataset(tampered, DataKind.SIMULATED, "t")))
    # Fold 1 chooses its parameters on data that ends before the tampering starts: the choice must be identical.
    assert c["folds"][0]["best_params"] == a["folds"][0]["best_params"]
    assert c["folds"][0]["in_sample_score"] == a["folds"][0]["in_sample_score"]


def test_walk_forward_exposes_overfitting_on_pure_noise():
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.standard_normal(4000) * 0.005))  # driftless random walk: there is no edge to find
    idx = pd.date_range("2023-01-01", periods=4000, freq="1h", tz="UTC", name="ts")
    bars = pd.DataFrame({"open": np.concatenate([[100], close[:-1]]), "close": close, "volume": 1e9}, index=idx)
    bars["high"], bars["low"] = bars[["open", "close"]].max(axis=1) * 1.001, bars[["open", "close"]].min(axis=1) * 0.999
    out = walk_forward(wf(data=Dataset(bars, DataKind.SIMULATED, "noise"), param_grid={"fast": [3, 5, 8, 13, 21], "slow": [30, 50, 80]}))
    s = out["summary"]
    if s["folds_with_parameters"]:
        assert s["mean_in_sample_score"] > s["mean_out_of_sample_score"]  # the best in-sample fit does not hold up


def test_walk_forward_input_validation():
    with pytest.raises(ValueError, match="Not enough data"):
        walk_forward(wf(data=synthetic_bars("1h", 300, seed=1)))
    with pytest.raises(ValueError, match="no valid combination"):
        walk_forward(wf(param_grid={"fast": [50], "slow": [10]}))
    with pytest.raises(ValueError, match="exceed the limit"):
        walk_forward(wf(param_grid={"fast": list(range(2, 30)), "slow": list(range(40, 60))}))


def test_strategy_comparison_table():
    def spec(name, params=None):
        return BacktestSpec(initial_capital=50_000.0, static_fx={"USDT": 1.0}, data={(BTC, "1h"): DATA},
                            deployments=[DeploymentSpec(name, BTC, "1h", params or {})])

    rows = compare_strategies({"EMA": spec("ema_crossover"), "Donchian": spec("donchian_breakout"), "RSI MR": spec("rsi_reversion")})
    assert [r["label"] for r in rows] == ["EMA", "Donchian", "RSI MR"] and len({r["config_hash"] for r in rows}) == 3
    assert all({"sharpe_ratio", "max_drawdown", "number_of_trades", "profit_factor"} <= set(r) for r in rows)

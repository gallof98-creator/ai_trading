import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from app.backtesting.engine import BacktestSpec, DeploymentSpec, run_backtest
from app.machine_learning.features import build_features, make_dataset, triple_barrier_labels
from app.machine_learning.models import MODEL_NAMES, create_model
from app.machine_learning.training import detect_drift, expected_calibration_error, reference_bins, train_and_evaluate
from app.machine_learning.validation import PurgedWalkForwardSplit
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset, synthetic_bars
from app.quant_ai import analyst
from app.quant_ai.regime import RegimeClusterer, detect_regimes, regime_summary

BTC = "CRYPTO:BTC/USDT"
NOISE = synthetic_bars("1h", 3000, seed=42).bars


def frame(close, noise=0.3, seed=0):
    rng = np.random.default_rng(seed)
    close = pd.Series(np.asarray(close, dtype=float), index=pd.date_range("2024-01-01", periods=len(close), freq="1h", tz="UTC", name="ts"))
    o = close.shift(1).fillna(close.iloc[0])
    return pd.DataFrame({"open": o, "high": np.maximum(o, close) + noise * rng.random(len(close)),
                         "low": np.minimum(o, close) - noise * rng.random(len(close)), "close": close, "volume": 1000 + 100 * rng.random(len(close))})


def predictable_bars(n=3000, seed=1):
    """Returns with strong positive autocorrelation: tomorrow tends to repeat today (a learnable pattern)."""
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = 0.75 * r[i - 1] + 0.004 * rng.standard_normal()
    return frame(100 * np.exp(np.cumsum(r)), noise=0.05, seed=seed)


# ---------- regimes ----------
def test_regime_detection_on_constructed_markets():
    rng = np.random.default_rng(0)
    up = detect_regimes(frame(np.linspace(100, 400, 600))).iloc[-1]
    down = detect_regimes(frame(np.linspace(400, 100, 600))).iloc[-1]
    flat = detect_regimes(frame(100 + rng.standard_normal(600) * 0.3)).iloc[-1]
    assert (up.trend, down.trend, flat.trend) == ("strong_uptrend", "strong_downtrend", "sideways")
    assert up.primary == "strong_uptrend"
    calm_then_wild = np.concatenate([100 + rng.standard_normal(500) * 0.05, 100 + np.cumsum(rng.standard_normal(60) * 3.0)])
    wild = detect_regimes(frame(calm_then_wild, noise=0.02))
    assert wild.vol_level.iloc[-1] == "high_volatility"
    assert "volatility_expansion" in set(wild.vol_dynamics.iloc[500:520])
    wild_then_calm = np.concatenate([100 + np.cumsum(rng.standard_normal(500) * 2.0), np.full(80, 100.0) + rng.standard_normal(80) * 0.01])
    calm = detect_regimes(frame(wild_then_calm, noise=0.005))
    assert calm.vol_level.iloc[-1] == "low_volatility" and "volatility_contraction" in set(calm.vol_dynamics.iloc[500:530])
    assert detect_regimes(frame(np.linspace(100, 110, 30))).primary.iloc[0] == "unknown"


def test_regimes_are_causal_and_summarised():
    full = detect_regimes(NOISE)
    for cut in (800, 1777, 2999):
        pd.testing.assert_frame_equal(detect_regimes(NOISE.iloc[:cut]), full.iloc[:cut])
    s = regime_summary(NOISE)
    assert set(s["current"]) == {"trend", "vol_level", "vol_dynamics", "primary"} and abs(sum(s["distribution"].values()) - 1) < 1e-9
    assert s["bars_in_current_regime"] >= 1


def test_regime_clusterer_is_fitted_on_training_data_only():
    model = RegimeClusterer(n_regimes=3, seed=0).fit(NOISE.iloc[:2000])
    a = model.predict(NOISE.iloc[1900:2500])
    tampered = NOISE.copy()
    tampered.iloc[2500:, :4] *= 2
    b = model.predict(tampered.iloc[1900:2500])
    pd.testing.assert_series_equal(a, b)
    assert set(a.unique()) <= {0, 1, 2}
    vol = np.log(NOISE.close).diff().rolling(20).std().reindex(a.index)
    assert vol[a == 0].mean() < vol[a == 2].mean()  # labels are ordered by volatility
    with pytest.raises(RuntimeError):
        RegimeClusterer().predict(NOISE)


# ---------- features, labels, leakage ----------
def test_features_do_not_use_future_bars():
    full = build_features(NOISE)
    for cut in (500, 1500, 2999):
        pd.testing.assert_frame_equal(build_features(NOISE.iloc[:cut]), full.iloc[:cut])
    tampered = NOISE.copy()
    tampered.iloc[2000:] *= 5
    pd.testing.assert_frame_equal(build_features(tampered).iloc[:2000], full.iloc[:2000])
    assert not np.isinf(full.to_numpy()[~np.isnan(full.to_numpy())]).any()


def test_triple_barrier_labels_hand_checked():
    close = [100.0] * 30 + [100, 101, 102, 103, 104, 105, 106, 107] + [107.0] * 10
    df = frame(close, noise=0.0)
    df["high"], df["low"] = df[["open", "close"]].max(axis=1) + 0.5, df[["open", "close"]].min(axis=1) - 0.5  # ATR = 1 on flat bars
    lab = triple_barrier_labels(df, horizon=5, atr_multiple=2.0)
    assert len(lab) == len(df) - 5 - 14  # no ATR for the first 14 bars, no future for the last 5
    i = 29  # close 100, barriers at 102 / 98 -> high of bar 31 (101.5)? no; bar 32 high 102.5 hits the upper barrier
    assert lab.label.iloc[i - 14] == 1 and lab.t1.iloc[i - 14] == 32
    assert (lab.t1.to_numpy() > np.arange(14, len(df) - 5)).all() and (lab.t1.to_numpy() <= np.arange(14, len(df) - 5) + 5).all()
    down = frame([100.0] * 30 + [99, 98, 97, 96, 95, 95, 95, 95], noise=0.0)
    down["high"], down["low"] = down[["open", "close"]].max(axis=1) + 0.5, down[["open", "close"]].min(axis=1) - 0.5
    assert triple_barrier_labels(down, 5, 2.0).label.iloc[29 - 14] == 0


def test_purged_split_never_trains_on_labels_that_overlap_the_test_window():
    X, y, t0, t1 = make_dataset(NOISE, horizon=20)
    n = 0
    for train, test in PurgedWalkForwardSplit(n_splits=5, embargo=20).split(t0, t1):
        n += 1
        assert train.max() < test.min()  # strictly chronological
        assert t1[train].max() < t0[test.min()] - 20  # every training label was decided before the test window (minus embargo)
        assert len(np.intersect1d(train, test)) == 0
    assert n == 5
    naive_overlap = (t1[: len(t0) // 6] >= t0[len(t0) // 6]).sum()
    assert naive_overlap > 0  # an unpurged split WOULD leak: the purge is doing real work
    with pytest.raises(ValueError):
        list(PurgedWalkForwardSplit(n_splits=50).split(t0[:300], t1[:300]))


# ---------- models ----------
@pytest.mark.parametrize("name", MODEL_NAMES)
def test_every_model_type_trains_reproducibly(name):
    X, y, _, _ = make_dataset(predictable_bars(1500), horizon=10)
    Xv, yv = X.to_numpy(), y.to_numpy()
    a = create_model(name, seed=3).fit(Xv[:900], yv[:900]).predict_proba(Xv[900:])[:, 1]
    b = create_model(name, seed=3).fit(Xv[:900], yv[:900]).predict_proba(Xv[900:])[:, 1]
    assert np.allclose(a, b) and ((a >= 0) & (a <= 1)).all() and a.std() > 0
    with pytest.raises(ValueError):
        create_model("magic")


def test_model_finds_no_edge_in_a_random_walk_and_says_so():
    record, _ = train_and_evaluate(NOISE, "logistic_regression", horizon=20, seed=0)
    assert 0.44 < record["metrics"]["auc"] < 0.56
    assert record["validated"] is False and record["calibrated"] is False and "No evidence" in record["verdict"]


def test_model_learns_a_real_pattern_out_of_sample_and_reports_evidence():
    record, model = train_and_evaluate(predictable_bars(), "gradient_boosting", horizon=10, seed=0)
    m = record["metrics"]
    assert m["auc"] > 0.6 and m["brier"] < m["baseline_brier"] and record["validated"] is True
    assert m["accuracy"] > m["baseline_accuracy"] - 0.02
    top = [f["feature"] for f in record["feature_importance"][:5]]
    assert any(f.startswith("ret_") or f in ("rsi_14", "macd_hist", "di_diff", "bb_position", "zscore_50") for f in top)
    assert sum(b["count"] for b in record["calibration_curve"]) == m["oos_samples"]
    assert model is not None and model.predict_proba(make_dataset(predictable_bars(), 10)[0].to_numpy()[:5]).shape == (5, 2)
    again, _ = train_and_evaluate(predictable_bars(), "gradient_boosting", horizon=10, seed=0)
    assert again["metrics"] == m and again["data_fingerprint"] == record["data_fingerprint"]
    with pytest.raises(ValueError, match="at least 600"):
        train_and_evaluate(NOISE.iloc[:400], "logistic_regression")


def test_calibration_error_and_drift_detection():
    rng = np.random.default_rng(0)
    p = rng.random(20000)
    assert expected_calibration_error((rng.random(20000) < p).astype(int), p)[0] < 0.02  # perfectly calibrated forecasts
    assert expected_calibration_error((rng.random(20000) < 0.5).astype(int), p)[0] > 0.2  # confident but uninformative
    feats = build_features(NOISE).dropna()
    ref = reference_bins(feats.iloc[:1500])
    same = detect_drift(ref, feats.iloc[:1500])
    assert same["status"] == "ok" and same["max_psi"] < 0.01
    shifted = feats.iloc[1500:].copy()
    shifted["vol_20"] *= 3
    drift = detect_drift(ref, shifted)
    assert drift["status"] == "alert" and "vol_20" in drift["drifted_features"]


# ---------- analyst ----------
def backtest(strategy="ema_crossover", params=None, data=NOISE):
    return run_backtest(BacktestSpec(initial_capital=100_000.0, static_fx={"USDT": 1.0},
                                     deployments=[DeploymentSpec(strategy, BTC, "1h", params or {"fast": 10, "slow": 40})],
                                     data={(BTC, "1h"): Dataset(data, DataKind.SIMULATED, "t")}))


def test_trade_analysis_breakdowns_are_complete_and_regime_lookup_is_causal():
    r = backtest()
    a = analyst.analyze_trades(r.trades, {BTC: NOISE})
    n = len(r.trades)
    for dim in ("by_exit_reason", "by_direction", "by_weekday", "by_entry_hour_utc", "by_regime_at_entry", "by_strategy", "by_instrument"):
        assert sum(g["trades"] for g in a[dim]) == n
        assert sum(g["net_profit"] for g in a[dim]) == pytest.approx(sum(t.pnl for t in r.trades))
    assert {g["group"] for g in a["by_exit_reason"]} <= {"signal", "stop_loss", "take_profit", "trailing_stop"}
    tampered = NOISE.copy()
    tampered.iloc[2500:, :4] *= 3
    early = [t for t in r.trades if pd.Timestamp(t.entry_ts) < NOISE.index[2400]]
    assert analyst.analyze_trades(early, {BTC: NOISE})["by_regime_at_entry"] == analyst.analyze_trades(early, {BTC: tampered})["by_regime_at_entry"]
    assert analyst.analyze_trades([]) == {"trades": 0}


def test_weakness_detection_flags_what_it_should():
    r = backtest()
    codes = {w["code"] for w in analyst.statistical_weaknesses(r.metrics, r.trades, r.costs)}
    assert "edge_not_significant" in codes  # a trend follower on a random walk has no real edge
    few = analyst.statistical_weaknesses({**r.metrics, "number_of_trades": 5}, r.trades[:5])
    assert few[0]["code"] == "small_sample" and few[0]["severity"] == "high"
    exp = analyst.explain_performance(r, {BTC: NOISE})
    assert exp["headline"].startswith("Net result") and exp["drivers"] and exp["warnings"]
    report = analyst.research_report("EMA on simulated data", r, {BTC: NOISE})
    assert "do not guarantee" in report["markdown"] and "## Statistical weaknesses" in report["markdown"] and r.config_hash in report["markdown"]


def test_comparison_ranking_carries_caveats():
    rows = [{"label": "A", "sharpe_ratio": 1.2, "number_of_trades": 80}, {"label": "B", "sharpe_ratio": 1.0, "number_of_trades": 90},
            {"label": "C", "sharpe_ratio": 3.0, "number_of_trades": 4}]
    out = analyst.compare(rows)
    assert out["ranking"] == ["A", "B"] and any("C: excluded" in n for n in out["notes"]) and any("not meaningful" in n for n in out["notes"])


def test_anomaly_detection_flags_shocks_only():
    assert analyst.detect_anomalies(frame(100 + np.random.default_rng(0).standard_normal(600) * 0.2, noise=0.1)) == []
    shocked = frame(np.concatenate([100 + np.random.default_rng(0).standard_normal(590) * 0.2, [100, 100, 100, 115, 115, 115, 115, 115, 115, 115]]), noise=0.1)
    shocked.iloc[595, shocked.columns.get_loc("volume")] = 50_000
    shocked.iloc[597, shocked.columns.get_loc("open")] = 125.0  # opens far from the previous close
    shocked.iloc[597, shocked.columns.get_loc("high")] = 125.5
    found = analyst.detect_anomalies(shocked)
    assert {"return", "gap", "volume"} <= {a["measure"] for a in found}
    assert all(a["robust_z"] > 5 for a in found)


def test_ai_proposals_cannot_touch_risk_or_live_settings_and_must_pass_validation():
    assert analyst.validate_proposal("ema_crossover", {"fast": 8, "slow": 30}) == []
    for bad in ({"fast": 8, "slow": 30, "risk_per_trade": 0.5}, {"max_drawdown": 0.99}, {"trading_mode": "auto"},
                {"live_trading_enabled": True}, {"emergency_policy": "block_new_risk"}, {"fast": 60, "slow": 10}, {"unknown": 1}):
        assert analyst.validate_proposal("ema_crossover", bad), bad
    assert analyst.validate_proposal("no_such_strategy", {})
    # The module cannot reach risk state or live authorization at all.
    code = ("import sys, app.quant_ai.analyst as a; "
            "bad=[n for n in vars(a) if n in ('RiskEngine','authorize_live_trading','LiveAuthorization','save_settings','TradingRuntime')]; "
            "mods=[m for m in sys.modules if m.startswith(('app.live_trading','app.risk.engine','app.execution.runtime'))]; "
            "print(bad, [m for m in mods if m=='app.live_trading.gate' or m=='app.execution.runtime']); sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).returncode == 0


def test_suggestions_are_judged_out_of_sample_and_noise_fails_the_gate():
    data = Dataset(NOISE, DataKind.SIMULATED, "noise")
    props = analyst.suggest_configurations("ema_crossover", BTC, "1h", data, 100_000.0, static_fx={"USDT": 1.0}, n_trials=8, seed=1)
    again = analyst.suggest_configurations("ema_crossover", BTC, "1h", data, 100_000.0, static_fx={"USDT": 1.0}, n_trials=8, seed=1)
    assert props == again  # seeded search is reproducible
    assert props and all(set(p["params"]) <= set(analyst._search_space("ema_crossover")) for p in props)
    assert all(p["validation"] == "failed" and p["validation_reasons"] for p in props)  # no edge exists in a random walk
    assert all(p["in_sample"]["sharpe_ratio"] is not None and "optimistically biased" in p["note"] for p in props)
    with pytest.raises(ValueError, match="800 bars"):
        analyst.suggest_configurations("ema_crossover", BTC, "1h", Dataset(NOISE.iloc[:500], DataKind.SIMULATED, "t"), 100_000.0)

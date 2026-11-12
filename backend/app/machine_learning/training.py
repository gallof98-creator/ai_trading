"""Model training, out-of-sample evaluation, calibration and drift detection.

A model is only reported as `validated` when its purged out-of-sample predictions beat the
no-skill baseline; only a validated AND calibrated model may attach a confidence to anything.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

from .features import make_dataset
from .models import create_model
from .validation import PurgedWalkForwardSplit

MIN_AUC = 0.53  # out-of-sample AUC needed before a model is called validated
MAX_CALIBRATION_ERROR = 0.05
PSI_WARNING, PSI_ALERT = 0.10, 0.25


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> tuple[float, list[dict]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece, curve = 0.0, []
    for b in range(bins):
        mask = idx == b
        if mask.any():
            predicted, observed = float(p[mask].mean()), float(y[mask].mean())
            ece += mask.mean() * abs(predicted - observed)
            curve.append({"predicted": predicted, "observed": observed, "count": int(mask.sum())})
    return float(ece), curve


def reference_bins(features: pd.DataFrame, bins: int = 10) -> dict[str, dict]:
    """Decile edges and shares of each feature in the training data (the drift reference)."""
    ref = {}
    for col in features.columns:
        edges = np.unique(np.quantile(features[col], np.linspace(0, 1, bins + 1))[1:-1])
        counts = np.bincount(np.digitize(features[col], edges), minlength=len(edges) + 1)
        ref[col] = {"edges": edges.tolist(), "shares": (counts / counts.sum()).tolist()}
    return ref


def population_stability_index(reference: dict, current: pd.Series) -> float:
    edges, expected = np.array(reference["edges"]), np.array(reference["shares"])
    counts = np.bincount(np.digitize(current.dropna(), edges), minlength=len(edges) + 1)
    actual = counts / max(counts.sum(), 1)
    e, a = np.clip(expected, 1e-4, None), np.clip(actual, 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


def detect_drift(reference: dict[str, dict], current_features: pd.DataFrame) -> dict:
    psi = {col: population_stability_index(ref, current_features[col]) for col, ref in reference.items() if col in current_features}
    worst = max(psi.values(), default=0.0)
    return {"psi": psi, "max_psi": worst, "status": "alert" if worst >= PSI_ALERT else "warning" if worst >= PSI_WARNING else "ok",
            "drifted_features": sorted([c for c, v in psi.items() if v >= PSI_ALERT])}


def train_and_evaluate(bars: pd.DataFrame, model_name: str, params: dict | None = None, horizon: int = 20, atr_multiple: float = 2.0,
                       n_splits: int = 5, seed: int = 0) -> tuple[dict, object | None]:
    """Returns (record, fitted_model). `record` is JSON-serialisable evidence about the model."""
    X, y, t0, t1 = make_dataset(bars, horizon, atr_multiple)
    if len(X) < 600:
        raise ValueError(f"Only {len(X)} usable samples; at least 600 are needed to train and validate a model")
    Xv, yv = X.to_numpy(dtype=float), y.to_numpy()
    oos_p, oos_y, last = [], [], None
    for train, test in PurgedWalkForwardSplit(n_splits, embargo=horizon).split(t0, t1):
        if len(np.unique(yv[train])) < 2:
            continue
        model = create_model(model_name, params, seed).fit(Xv[train], yv[train])
        oos_p.append(model.predict_proba(Xv[test])[:, 1])
        oos_y.append(yv[test])
        last = (model, test)
    if not oos_p:
        raise ValueError("No valid cross-validation fold (labels have a single class or too little history)")
    p, yy = np.concatenate(oos_p), np.concatenate(oos_y)
    base_rate = float(yy.mean())
    auc = float(roc_auc_score(yy, p)) if 0 < base_rate < 1 else 0.5
    ece, curve = expected_calibration_error(yy, p)
    metrics = {
        "oos_samples": int(len(yy)), "base_rate": base_rate, "auc": auc,
        "accuracy": float(accuracy_score(yy, p >= 0.5)), "baseline_accuracy": max(base_rate, 1 - base_rate),
        "brier": float(brier_score_loss(yy, p)), "baseline_brier": base_rate * (1 - base_rate),
        "log_loss": float(log_loss(yy, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])), "calibration_error": ece,
    }
    validated = auc >= MIN_AUC and metrics["brier"] < metrics["baseline_brier"]

    # Final model: fit on everything whose label is known before the calibration tail, calibrate on the tail.
    cut = int(len(Xv) * 0.8)
    fit_idx = np.where(t1[:cut] < t0[cut] - horizon)[0]
    final, calibrated_error = None, None
    if len(fit_idx) >= 200 and len(np.unique(yv[fit_idx])) == 2 and len(np.unique(yv[cut:])) == 2:
        base = create_model(model_name, params, seed).fit(Xv[fit_idx], yv[fit_idx])
        final = CalibratedClassifierCV(FrozenEstimator(base), method="sigmoid").fit(Xv[cut:], yv[cut:])
        # Calibration quality is judged on the out-of-fold predictions above, not on the data used to calibrate.
        calibrated_error = ece
    importance = []
    if last is not None:
        model, test = last
        perm = permutation_importance(model, Xv[test], yv[test], scoring="neg_brier_score", n_repeats=5, random_state=seed)
        importance = sorted(({"feature": f, "importance": float(m), "std": float(s)} for f, m, s in zip(X.columns, perm.importances_mean, perm.importances_std)),
                            key=lambda d: -d["importance"])
    record = {
        "model": model_name, "params": params or {}, "seed": seed, "horizon": horizon, "atr_multiple": atr_multiple,
        "features": list(X.columns), "samples": int(len(X)), "trained_through": bars.index[-1].isoformat(),
        "data_fingerprint": hashlib.sha256(np.ascontiguousarray(bars[["open", "high", "low", "close", "volume"]].to_numpy(dtype="float64")).tobytes()).hexdigest(),
        "metrics": metrics, "calibration_curve": curve, "feature_importance": importance,
        "validated": bool(validated), "calibrated": bool(validated and calibrated_error is not None and calibrated_error <= MAX_CALIBRATION_ERROR),
        "verdict": ("Out-of-sample predictions beat the no-skill baseline." if validated else
                    f"No evidence of predictive power out of sample (AUC {auc:.3f}, needs >= {MIN_AUC}). Do not use this model's output."),
        "feature_reference": reference_bins(X),
    }
    return record, final

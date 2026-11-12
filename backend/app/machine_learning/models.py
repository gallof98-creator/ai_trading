"""Model zoo behind one factory. Every model follows the scikit-learn classifier interface and is
seeded, so training is reproducible."""
from __future__ import annotations

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODEL_NAMES = ("logistic_regression", "random_forest", "gradient_boosting", "xgboost", "neural_network")


class TorchMLPClassifier(ClassifierMixin, BaseEstimator):
    """Small feed-forward network (PyTorch). Inputs are standardised with statistics from the
    training data only; training stops early on a chronological validation tail."""

    def __init__(self, hidden: int = 32, epochs: int = 60, lr: float = 1e-3, weight_decay: float = 1e-3, dropout: float = 0.1,
                 patience: int = 8, seed: int = 0) -> None:
        self.hidden, self.epochs, self.lr, self.weight_decay, self.dropout, self.patience, self.seed = hidden, epochs, lr, weight_decay, dropout, patience, seed

    def fit(self, X, y):
        import torch
        from torch import nn

        X, y = np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.float32)
        self.classes_ = np.array([0, 1])
        self.mean_, self.std_ = X.mean(axis=0), X.std(axis=0) + 1e-8
        torch.manual_seed(self.seed)
        torch.set_num_threads(1)
        Xs = torch.from_numpy((X - self.mean_) / self.std_)
        ys = torch.from_numpy(y)
        cut = int(len(Xs) * 0.85)  # the most recent 15% of the training window validates
        self.net_ = nn.Sequential(nn.Linear(X.shape[1], self.hidden), nn.ReLU(), nn.Dropout(self.dropout),
                                  nn.Linear(self.hidden, self.hidden), nn.ReLU(), nn.Linear(self.hidden, 1))
        opt = torch.optim.Adam(self.net_.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        loss_fn = nn.BCEWithLogitsLoss()
        best, best_state, stale = float("inf"), None, 0
        gen = torch.Generator().manual_seed(self.seed)
        for _ in range(self.epochs):
            self.net_.train()
            order = torch.randperm(cut, generator=gen)
            for i in range(0, cut, 64):
                batch = order[i:i + 64]
                opt.zero_grad()
                loss_fn(self.net_(Xs[batch]).squeeze(1), ys[batch]).backward()
                opt.step()
            self.net_.eval()
            with torch.no_grad():
                val = loss_fn(self.net_(Xs[cut:]).squeeze(1), ys[cut:]).item() if cut < len(Xs) else 0.0
            if val < best - 1e-5:
                best, stale = val, 0
                best_state = {k: v.clone() for k, v in self.net_.state_dict().items()}
            else:
                stale += 1
                if stale >= self.patience:
                    break
        if best_state is not None:
            self.net_.load_state_dict(best_state)
        self.net_.eval()
        return self

    def predict_proba(self, X):
        import torch

        with torch.no_grad():
            z = self.net_(torch.from_numpy(((np.asarray(X, dtype=np.float32) - self.mean_) / self.std_))).squeeze(1)
            p = torch.sigmoid(z).numpy().astype(float)
        return np.column_stack([1 - p, p])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def create_model(name: str, params: dict | None = None, seed: int = 0):
    p = dict(params or {})
    if name == "logistic_regression":
        return make_pipeline(StandardScaler(), LogisticRegression(C=p.get("C", 0.5), max_iter=1000, random_state=seed))
    if name == "random_forest":
        return RandomForestClassifier(n_estimators=p.get("n_estimators", 200), max_depth=p.get("max_depth", 5),
                                      min_samples_leaf=p.get("min_samples_leaf", 20), random_state=seed, n_jobs=1)
    if name == "gradient_boosting":
        return GradientBoostingClassifier(n_estimators=p.get("n_estimators", 150), max_depth=p.get("max_depth", 3),
                                          learning_rate=p.get("learning_rate", 0.05), subsample=p.get("subsample", 0.8), random_state=seed)
    if name == "xgboost":
        from xgboost import XGBClassifier

        return XGBClassifier(n_estimators=p.get("n_estimators", 200), max_depth=p.get("max_depth", 3), learning_rate=p.get("learning_rate", 0.05),
                             subsample=p.get("subsample", 0.8), colsample_bytree=p.get("colsample_bytree", 0.8), reg_lambda=p.get("reg_lambda", 1.0),
                             random_state=seed, n_jobs=1, eval_metric="logloss", tree_method="hist")
    if name == "neural_network":
        return TorchMLPClassifier(hidden=p.get("hidden", 32), epochs=p.get("epochs", 60), lr=p.get("lr", 1e-3), seed=seed)
    raise ValueError(f"Unknown model {name!r}; available: {', '.join(MODEL_NAMES)}")

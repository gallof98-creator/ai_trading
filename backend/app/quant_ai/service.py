"""Quant AI use-cases: market analysis, model training records, reports and gated proposals."""
from __future__ import annotations

import uuid
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtesting.engine import BacktestSpec, DeploymentSpec, run_backtest
from ..database.models import BacktestRun, ResearchItem
from ..machine_learning.features import build_features
from ..machine_learning.training import detect_drift, train_and_evaluate
from ..market_data.sources import Dataset
from ..strategies import indicators as ind
from ..strategies.registry import create_strategy
from . import analyst
from .regime import detect_regimes, regime_summary


def _round(series: pd.Series, n: int = 6) -> list:
    return [None if pd.isna(v) else round(float(v), n) for v in series]


def market_analysis(ds: Dataset, strategy: str | None = None, params: dict | None = None, limit: int = 500) -> dict:
    """Chart payload: candles, indicators, regime per bar and (optionally) a strategy's signals.
    Everything is computed on the full history first and then cut, so the visible tail is fully warmed up."""
    bars = ds.bars
    regimes = detect_regimes(bars)
    out = {
        "kind": ds.kind.value, "source": ds.source,
        "candles": [{"ts": ts.isoformat(), "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close),
                     "volume": float(r.volume)} for ts, r in bars.iloc[-limit:].iterrows()],
        "indicators": {"ema_20": _round(ind.ema(bars.close, 20).iloc[-limit:]), "ema_50": _round(ind.ema(bars.close, 50).iloc[-limit:]),
                       "rsi_14": _round(ind.rsi(bars.close, 14).iloc[-limit:], 2), "atr_14": _round(ind.atr(bars, 14).iloc[-limit:])},
        "regime": list(regimes["primary"].iloc[-limit:]), "regime_summary": regime_summary(bars),
        "anomalies": analyst.detect_anomalies(bars) if len(bars) > 150 else [], "signals": [],
    }
    if strategy:
        frame = create_strategy(strategy, params or {}).generate(bars).iloc[-limit:]
        out["signals"] = [{"ts": ts.isoformat(), "action": r.action, "price": float(r.close)} for ts, r in frame[frame.action != "HOLD"].iterrows()]
    return out


def _item(row: ResearchItem, with_payload: bool = True) -> dict:
    out = {"id": row.id, "kind": row.kind, "status": row.status, "title": row.title, "created_at": row.created_at.isoformat()}
    if with_payload:
        out["payload"] = row.payload
    return out


def list_items(session: Session, kind: str, limit: int = 50) -> list[dict]:
    rows = session.scalars(select(ResearchItem).where(ResearchItem.kind == kind).order_by(ResearchItem.created_at.desc()).limit(limit))
    return [_item(r) for r in rows]


def train_model(session: Session, ds: Dataset, instrument_id: str, timeframe: str, model: str, horizon: int, seed: int, model_dir: str) -> dict:
    record, fitted = train_and_evaluate(ds.bars, model, horizon=horizon, seed=seed)
    item_id = uuid.uuid4().hex
    record.update({"instrument_id": instrument_id, "timeframe": timeframe, "data_kind": ds.kind.value, "artifact": None})
    if fitted is not None and record["validated"]:
        Path(model_dir).mkdir(parents=True, exist_ok=True)
        path = Path(model_dir) / f"{item_id}.joblib"
        joblib.dump(fitted, path)
        record["artifact"] = str(path)
    status = "validated" if record["validated"] else "rejected"
    row = ResearchItem(id=item_id, kind="model", status=status, title=f"{model} · {instrument_id} {timeframe}", payload=record)
    session.add(row)
    session.flush()
    return _item(row)


def model_drift(session: Session, model_id: str, ds: Dataset) -> dict:
    row = session.get(ResearchItem, model_id)
    if row is None or row.kind != "model":
        raise LookupError("Unknown model")
    features = build_features(ds.bars).dropna()
    recent = features[features.index > pd.Timestamp(row.payload["trained_through"])]
    if len(recent) < 100:
        recent = features.iloc[-250:]
        basis = "last 250 bars (fewer than 100 bars since training)"
    else:
        basis = f"{len(recent)} bars since training"
    return {**detect_drift(row.payload["feature_reference"], recent), "basis": basis, "model_id": model_id}


def create_report(session: Session, backtest_id: str, instruments: list | None = None) -> dict:
    """Re-runs the stored configuration on the stored data (verifying the data fingerprint) and writes a report."""
    from ..backtesting.service import load_dataset
    from ..database.settings_store import RiskLimits
    from ..paper_trading.broker import ExecutionModel

    run = session.get(BacktestRun, backtest_id)
    if run is None:
        raise LookupError("Unknown backtest")
    cfg = run.config
    data, bars = {}, {}
    for key, meta in cfg["data"].items():
        iid, tf = key.split("|")
        ds = load_dataset(session, iid, tf, meta["first"], meta["last"])
        if ds.fingerprint() != meta["fingerprint"]:
            raise ValueError(f"Stored data for {iid} {tf} has changed since this backtest ran; run the backtest again")
        data[(iid, tf)], bars[iid] = ds, ds.bars
    result = run_backtest(BacktestSpec(
        initial_capital=cfg["capital"], base_currency=cfg["base_currency"], static_fx=cfg["static_fx"], data=data, instruments=instruments,
        execution=ExecutionModel.from_dict(cfg["execution"]), limits=RiskLimits(**cfg["limits"]),
        deployments=[DeploymentSpec(s, i, tf, p) for s, i, tf, p in cfg["deployments"]]))
    report = analyst.research_report(cfg.get("label") or "Backtest report", result, bars)
    report["backtest_id"] = backtest_id
    report["reproduced"] = bool(np.isclose(result.metrics["final_equity"], run.metrics["final_equity"]))
    row = ResearchItem(id=uuid.uuid4().hex, kind="report", status="done", title=report["title"], payload=report)
    session.add(row)
    session.flush()
    return _item(row)


def create_proposals(session: Session, ds: Dataset, strategy: str, instrument_id: str, timeframe: str, capital: float,
                     static_fx: dict[str, float], limits, n_trials: int, seed: int, instruments: list | None = None) -> list[dict]:
    out = []
    for p in analyst.suggest_configurations(strategy, instrument_id, timeframe, ds, capital, static_fx=static_fx, limits=limits, n_trials=n_trials, seed=seed,
                                            instruments=instruments):
        row = ResearchItem(id=uuid.uuid4().hex, kind="proposal", status="validated" if p["validation"] == "passed" else "failed_validation",
                           title=f"{strategy} · {instrument_id} {timeframe}", payload=p)
        session.add(row)
        session.flush()
        out.append(_item(row))
    return out


def proposal_for_apply(session: Session, proposal_id: str) -> ResearchItem:
    """Only proposals that passed validation, re-checked now against the schema, may be applied."""
    row = session.get(ResearchItem, proposal_id)
    if row is None or row.kind != "proposal":
        raise LookupError("Unknown proposal")
    if row.status != "validated":
        raise PermissionError(f"Proposal is '{row.status}' and cannot be applied")
    reasons = analyst.validate_proposal(row.payload["strategy"], row.payload["params"])
    if reasons:
        row.status = "failed_validation"
        raise PermissionError("Proposal no longer passes validation: " + "; ".join(reasons))
    return row

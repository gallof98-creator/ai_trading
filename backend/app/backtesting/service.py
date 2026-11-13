"""Backtesting use-cases over stored market data (kept out of the API routes)."""
from __future__ import annotations

import uuid

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import BacktestRun
from ..database.settings_store import RiskLimits
from ..market_data.quality import DataKind
from ..market_data.sources import Dataset, synthetic_bars
from ..market_data.store import BarStore
from ..market_data.timeframes import tf_delta
from ..paper_trading.broker import ExecutionModel
from .engine import BacktestResult, BacktestSpec, DeploymentSpec, run_backtest
from .walk_forward import WalkForwardSpec, compare_strategies, walk_forward

MAX_BARS = 30_000
_store = BarStore()


class DataUnavailable(LookupError):
    pass


def load_dataset(session: Session, instrument_id: str, timeframe: str, start: str | None = None, end: str | None = None) -> Dataset:
    ds = _store.load(session, instrument_id, timeframe, pd.Timestamp(start, tz="UTC") if start else None, pd.Timestamp(end, tz="UTC") if end else None)
    if ds is None:
        raise DataUnavailable(f"No stored bars for {instrument_id} {timeframe} in the requested period. Import or fetch data first.")
    if len(ds.bars) > MAX_BARS:
        raise ValueError(f"{len(ds.bars)} bars exceed the limit of {MAX_BARS} per run; choose a shorter period or a higher timeframe")
    return ds


def store_simulated(session: Session, instrument_id: str, timeframe: str, bars: int, seed: int, replace: bool) -> dict:
    """Store a seeded random-walk dataset for research. Refuses to overwrite market data unless told to."""
    existing = _store.load(session, instrument_id, timeframe)
    if existing is not None and not replace:
        raise ValueError(f"{instrument_id} {timeframe} already has {existing.kind.value} data; pass replace=true to delete it first")
    _store.delete_series(session, instrument_id, timeframe)
    ds = synthetic_bars(timeframe, bars, seed)
    _store.save(session, instrument_id, timeframe, ds)
    return {"bars": len(ds.bars), "kind": ds.kind.value, "source": ds.source, "fingerprint": ds.fingerprint()}


def build_spec(session: Session, capital: float, deployments: list[dict], base_currency: str, limits: RiskLimits, execution: dict | None,
               static_fx: dict[str, float], start: str | None, end: str | None, instruments: list | None = None) -> BacktestSpec:
    deps = [DeploymentSpec(d["strategy"], d["instrument_id"], d["timeframe"], d.get("params") or {}) for d in deployments]
    data = {}
    for d in deps:
        finest = min((x.timeframe for x in deps if x.instrument_id == d.instrument_id), key=tf_delta)
        if (d.instrument_id, finest) not in data:
            data[(d.instrument_id, finest)] = load_dataset(session, d.instrument_id, finest, start, end)
    return BacktestSpec(initial_capital=capital, deployments=deps, data=data, base_currency=base_currency, limits=limits,
                        execution=ExecutionModel(**execution) if execution else ExecutionModel(), static_fx=static_fx, instruments=instruments)


def run_and_store(session: Session, spec: BacktestSpec, label: str, actor: str) -> tuple[str, BacktestResult]:
    result = run_backtest(spec)
    run_id = uuid.uuid4().hex
    payload = result.to_dict()
    session.add(BacktestRun(id=run_id, status="done", metrics=payload["metrics"], config={
        "label": label, "actor": actor, "config_hash": result.config_hash, "capital": spec.initial_capital, "base_currency": spec.base_currency,
        "deployments": [[d.strategy, d.instrument_id, d.timeframe, d.params] for d in spec.deployments], "execution": spec.execution.to_dict(),
        "static_fx": spec.static_fx, "limits": spec.limits.model_dump(), "data": {f"{k[0]}|{k[1]}": {"fingerprint": v.fingerprint(), "kind": v.kind.value, "bars": len(v.bars),
                                                                 "first": v.bars.index[0].isoformat(), "last": v.bars.index[-1].isoformat()}
                                                for k, v in spec.data.items()},
        "result": payload}))
    return run_id, result


def list_runs(session: Session, limit: int = 50) -> list[dict]:
    rows = session.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit))
    return [{"id": r.id, "created_at": r.created_at.isoformat(), "label": r.config.get("label"), "deployments": r.config.get("deployments"),
             "config_hash": r.config.get("config_hash"), "metrics": r.metrics,
             "data_kinds": sorted({v["kind"] for v in r.config.get("data", {}).values()})} for r in rows]


def run_walk_forward(session: Session, body: dict, capital: float, base_currency: str, limits: RiskLimits, instruments: list | None = None) -> dict:
    data = load_dataset(session, body["instrument_id"], body["timeframe"], body.get("start"), body.get("end"))
    return walk_forward(WalkForwardSpec(
        strategy=body["strategy"], instrument_id=body["instrument_id"], timeframe=body["timeframe"], data=data, param_grid=body["param_grid"],
        initial_capital=capital, n_folds=body.get("n_folds", 4), metric=body.get("metric", "sharpe_ratio"), fixed_params=body.get("fixed_params") or {},
        base_currency=base_currency, limits=limits, static_fx=body.get("static_fx") or {}, instruments=instruments))


def run_comparison(session: Session, items: list[dict], capital: float, base_currency: str, limits: RiskLimits, static_fx: dict[str, float],
                   start: str | None, end: str | None, instruments: list | None = None) -> list[dict]:
    specs = {}
    for item in items:
        label = item.get("label") or f"{item['strategy']} {item['instrument_id']} {item['timeframe']}"
        specs[label] = build_spec(session, capital, [item], base_currency, limits, None, static_fx, start, end, instruments)
    return compare_strategies(specs)


def import_csv_series(session: Session, instrument, timeframe: str, csv_text: str, tz: str | None, replace: bool) -> dict:
    """Validate and store one CSV series as market data. Never merges into an existing series silently."""
    import io

    from ..market_data.calendars import get_calendar
    from ..market_data.quality import validate_bars
    from ..market_data.sources import load_csv

    raw = load_csv(io.StringIO(csv_text), tz=tz)
    clean, report = validate_bars(raw.bars, timeframe, get_calendar(instrument.calendar))
    if clean.empty:
        raise ValueError("The file contains no valid bars")
    existing = _store.load(session, instrument.id, timeframe)
    if existing is not None:
        if not replace:
            raise ValueError(f"{instrument.id} {timeframe} already has {existing.kind.value} data; choose 'replace' to overwrite it")
        _store.delete_series(session, instrument.id, timeframe)
    n = _store.save(session, instrument.id, timeframe, Dataset(clean, DataKind.HISTORICAL, "csv-import"))
    return {"bars_stored": n, "first": clean.index[0].isoformat(), "last": clean.index[-1].isoformat(), "quality": report.to_dict()}


def scan_universe(session: Session, strategy: str, params: dict, timeframe: str, instrument_ids: list[str], capital: float, base_currency: str,
                  limits: RiskLimits, execution: dict | None, static_fx: dict[str, float], start: str | None, end: str | None,
                  instruments: list | None = None) -> dict:
    """Run the same strategy independently on every instrument and summarise the distribution of results.
    Looking at many instruments and quoting the best ones is a selection bias; the summary reports the
    whole distribution for that reason."""
    import numpy as np

    rows, skipped = [], []
    for iid in instrument_ids:
        try:
            spec = build_spec(session, capital, [{"strategy": strategy, "instrument_id": iid, "timeframe": timeframe, "params": params}],
                              base_currency, limits, execution, static_fx, start, end, instruments)
            result = run_backtest(spec)
        except (DataUnavailable, ValueError, LookupError) as exc:
            skipped.append({"instrument_id": iid, "reason": str(exc)})
            continue
        m = result.metrics
        ds = next(iter(spec.data.values()))
        rows.append({"instrument_id": iid, "bars": len(ds.bars), "first": ds.bars.index[0].isoformat(), "last": ds.bars.index[-1].isoformat(),
                     "data_kind": ds.kind.value, "total_return": m["total_return"], "max_drawdown": m["max_drawdown"], "sharpe_ratio": m["sharpe_ratio"],
                     "profit_factor": m["profit_factor"], "win_rate": m["win_rate"], "number_of_trades": m["number_of_trades"]})
    summary: dict = {"instruments_tested": len(rows), "instruments_skipped": len(skipped)}
    if rows:
        returns = np.array([r["total_return"] for r in rows])
        sharpes = [r["sharpe_ratio"] for r in rows if r["sharpe_ratio"] is not None]
        summary.update({
            "share_profitable": float((returns > 0).mean()), "median_return": float(np.median(returns)), "mean_return": float(returns.mean()),
            "best_return": float(returns.max()), "worst_return": float(returns.min()),
            "median_sharpe": float(np.median(sharpes)) if sharpes else None, "total_trades": int(sum(r["number_of_trades"] for r in rows)),
        })
    notes = ["Each instrument is tested on its own with the full capital; this is not a portfolio simulation."]
    if len(rows) > 1:
        notes.append(f"With {len(rows)} instruments some will look good by chance alone. Judge the median and the share of profitable "
                     "instruments, not the best rows.")
    if any(r["data_kind"] == "simulated" for r in rows):
        notes.append("Some series are SIMULATED and say nothing about real markets.")
    from .metrics import DISCLAIMER

    return {"strategy": strategy, "params": params, "timeframe": timeframe, "rows": sorted(rows, key=lambda r: r["instrument_id"]),
            "skipped": skipped, "summary": summary, "notes": notes, "disclaimer": DISCLAIMER}

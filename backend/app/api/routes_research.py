"""Market data, backtesting and Quant AI endpoints."""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtesting import service as bt
from ..database.models import BacktestRun, ResearchItem, StrategyDeployment, User
from ..execution.runtime import RuntimeError_, TradingRuntime
from ..machine_learning.models import MODEL_NAMES
from ..market_data.calendars import get_calendar
from ..market_data.quality import validate_bars
from ..market_data.sources import Dataset, load_csv
from ..market_data.store import BarStore
from ..market_data.timeframes import TIMEFRAMES
from ..monitoring import audit
from ..quant_ai import service as ai
from ..quant_ai.regime import regime_summary
from ..security.roles import Role
from .deps import Container, get_container, get_runtime, get_session, require_role

router = APIRouter(prefix="/api", tags=["research"])
viewer, trader, admin = require_role(Role.VIEWER), require_role(Role.TRADER), require_role(Role.ADMIN)
_store = BarStore()


def guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except bt.DataUnavailable as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc).strip("'\"")) from None
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except (RuntimeError_, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


def _dataset(session: Session, rt: TradingRuntime, instrument_id: str, timeframe: str) -> Dataset:
    if timeframe not in TIMEFRAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported timeframe {timeframe}")
    try:
        rt.registry.get(instrument_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown instrument {instrument_id}") from None
    return guard(bt.load_dataset, session, instrument_id, timeframe)


# ---------------------------------------------------------------- market data
@router.get("/market/coverage")
def coverage(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return _store.coverage(session)


@router.get("/market/analysis")
def analysis(instrument_id: str, timeframe: str, strategy: str | None = None, limit: int = 400, _: User = Depends(viewer),
             session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    ds = _dataset(session, rt, instrument_id, timeframe)
    return guard(ai.market_analysis, ds, strategy, None, max(50, min(limit, 1500)))


@router.get("/market/quality")
def quality(instrument_id: str, timeframe: str, _: User = Depends(viewer), session: Session = Depends(get_session),
            rt: TradingRuntime = Depends(get_runtime)) -> dict:
    ds = _dataset(session, rt, instrument_id, timeframe)
    _, report = validate_bars(ds.bars, timeframe, get_calendar(rt.registry.get(instrument_id).calendar))
    return {**report.to_dict(), "kind": ds.kind.value, "source": ds.source, "fingerprint": ds.fingerprint()}


class CsvImport(BaseModel):
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    csv: str = Field(max_length=20_000_000)
    timezone: str | None = Field(None, max_length=64)
    replace: bool = False


@router.post("/market/import-csv")
def import_csv(body: CsvImport, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    def run() -> dict:
        inst = rt.registry.get(body.instrument_id)
        if body.timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {body.timeframe}")
        raw = load_csv(io.StringIO(body.csv), tz=body.timezone)
        clean, report = validate_bars(raw.bars, body.timeframe, get_calendar(inst.calendar))
        existing = _store.load(session, body.instrument_id, body.timeframe)
        if existing is not None and existing.kind.value == "simulated" and not body.replace:
            raise ValueError("This series currently holds SIMULATED data; pass replace=true to delete it before importing market data")
        if existing is not None and body.replace:
            from sqlalchemy import delete

            from ..database.models import BarRow
            session.execute(delete(BarRow).where(BarRow.instrument_id == body.instrument_id, BarRow.timeframe == body.timeframe))
        n = _store.save(session, body.instrument_id, body.timeframe, Dataset(clean, raw.kind, "csv-import"))
        audit.record(session, user.email, "market.csv_imported", instrument_id=body.instrument_id, timeframe=body.timeframe, bars=n)
        return {"bars_stored": n, "quality": report.to_dict()}

    return guard(run)


class SimulatedIn(BaseModel):
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    bars: int = Field(3000, ge=300, le=bt.MAX_BARS)
    seed: int = Field(1, ge=0, le=2**31)
    replace: bool = False


@router.post("/market/generate-simulated")
def generate_simulated(body: SimulatedIn, user: User = Depends(trader), session: Session = Depends(get_session),
                       rt: TradingRuntime = Depends(get_runtime)) -> dict:
    def run() -> dict:
        rt.registry.get(body.instrument_id)
        if rt.kinds.get(body.instrument_id) not in (None, "simulated"):
            raise ValueError("This instrument is receiving market data in the trading runtime; simulated data cannot replace it")
        if body.instrument_id in rt.replays:
            raise ValueError("A simulated replay is driving the paper engine for this instrument; its bars are already stored and usable for research")
        out = bt.store_simulated(session, body.instrument_id, body.timeframe, body.bars, body.seed, body.replace)
        audit.record(session, user.email, "market.simulated_generated", instrument_id=body.instrument_id, timeframe=body.timeframe, **out)
        return out

    return guard(run)


# ---------------------------------------------------------------- backtesting
class BacktestDeployment(BaseModel):
    strategy: str = Field(max_length=64)
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    params: dict = Field(default_factory=dict)
    label: str | None = Field(None, max_length=100)


class ExecutionIn(BaseModel):
    spread_bps: float = Field(2.0, ge=0, le=500)
    slippage_bps: float = Field(1.0, ge=0, le=500)
    participation_rate: float | None = Field(0.1, gt=0, le=1)
    financing_long_annual: float = Field(0.0, ge=0, le=5)
    financing_short_annual: float = Field(0.0, ge=0, le=5)


class BacktestIn(BaseModel):
    label: str = Field("Backtest", max_length=100)
    deployments: list[BacktestDeployment] = Field(min_length=1, max_length=20)
    initial_capital: float | None = Field(None, gt=0)
    start: str | None = None
    end: str | None = None
    execution: ExecutionIn = ExecutionIn()
    static_fx: dict[str, float] = Field(default_factory=dict)


def _capital(body_capital: float | None, rt: TradingRuntime) -> float:
    capital = body_capital or rt.user_settings.initial_paper_capital
    if capital is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "State the capital for this test, or set the initial paper capital in Settings")
    return capital


@router.post("/backtests")
def run_backtest(body: BacktestIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    capital = _capital(body.initial_capital, rt)

    def run() -> dict:
        spec = bt.build_spec(session, capital, [d.model_dump() for d in body.deployments], rt.user_settings.base_currency, rt.user_settings.risk,
                             body.execution.model_dump(), body.static_fx, body.start, body.end)
        run_id, result = bt.run_and_store(session, spec, body.label, user.email)
        return {"id": run_id, **result.to_dict()}

    return guard(run)


@router.get("/backtests")
def list_backtests(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return bt.list_runs(session)


@router.get("/backtests/{run_id}")
def get_backtest(run_id: str, _: User = Depends(viewer), session: Session = Depends(get_session)) -> dict:
    row = session.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown backtest")
    return {"id": row.id, "created_at": row.created_at.isoformat(), "label": row.config.get("label"), "deployments": row.config["deployments"],
            "data": row.config["data"], **row.config["result"]}


class WalkForwardIn(BaseModel):
    strategy: str = Field(max_length=64)
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    param_grid: dict[str, list] = Field(min_length=1)
    fixed_params: dict = Field(default_factory=dict)
    n_folds: int = Field(4, ge=2, le=10)
    metric: str = Field("sharpe_ratio", pattern=r"^(sharpe_ratio|sortino_ratio|calmar_ratio|profit_factor|total_return)$")
    initial_capital: float | None = Field(None, gt=0)
    static_fx: dict[str, float] = Field(default_factory=dict)
    start: str | None = None
    end: str | None = None


@router.post("/backtests/walk-forward")
def walk_forward(body: WalkForwardIn, _: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    capital = _capital(body.initial_capital, rt)
    return guard(bt.run_walk_forward, session, body.model_dump(), capital, rt.user_settings.base_currency, rt.user_settings.risk)


class CompareIn(BaseModel):
    items: list[BacktestDeployment] = Field(min_length=2, max_length=12)
    initial_capital: float | None = Field(None, gt=0)
    static_fx: dict[str, float] = Field(default_factory=dict)
    start: str | None = None
    end: str | None = None


@router.post("/backtests/compare")
def compare(body: CompareIn, _: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..quant_ai import analyst

    capital = _capital(body.initial_capital, rt)
    rows = guard(bt.run_comparison, session, [i.model_dump() for i in body.items], capital, rt.user_settings.base_currency, rt.user_settings.risk,
                 body.static_fx, body.start, body.end)
    return analyst.compare(rows)


# ---------------------------------------------------------------- Quant AI
@router.get("/ai/regime")
def regime(instrument_id: str, timeframe: str, _: User = Depends(viewer), session: Session = Depends(get_session),
           rt: TradingRuntime = Depends(get_runtime)) -> dict:
    ds = _dataset(session, rt, instrument_id, timeframe)
    return {**regime_summary(ds.bars), "data_kind": ds.kind.value}


class TrainIn(BaseModel):
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    model: str = Field(pattern="^(" + "|".join(MODEL_NAMES) + ")$")
    horizon: int = Field(20, ge=2, le=200)
    seed: int = Field(0, ge=0, le=2**31)


@router.post("/ai/models/train")
def train(body: TrainIn, _: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime),
          container: Container = Depends(get_container)) -> dict:
    ds = _dataset(session, rt, body.instrument_id, body.timeframe)
    item = guard(ai.train_model, session, ds, body.instrument_id, body.timeframe, body.model, body.horizon, body.seed, container.settings.model_dir)
    if item["status"] != "validated":
        rt.notifier.notify(rt.clock(), "model", "info", f"Model not validated: {item['title']}", item["payload"]["verdict"])
    return item


@router.get("/ai/models")
def models(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return ai.list_items(session, "model")


@router.get("/ai/models/{model_id}/drift")
def drift(model_id: str, _: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    row = session.get(ResearchItem, model_id)
    if row is None or row.kind != "model":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown model")
    ds = _dataset(session, rt, row.payload["instrument_id"], row.payload["timeframe"])
    out = guard(ai.model_drift, session, model_id, ds)
    if out["status"] == "alert":
        rt.notifier.notify(rt.clock(), "model", "warning", f"Model drift: {row.title}", f"Drifted features: {', '.join(out['drifted_features'])}")
    return out


class ReportIn(BaseModel):
    backtest_id: str = Field(max_length=32)


@router.post("/ai/reports")
def report(body: ReportIn, _: User = Depends(trader), session: Session = Depends(get_session)) -> dict:
    return guard(ai.create_report, session, body.backtest_id)


@router.get("/ai/reports")
def reports(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return ai.list_items(session, "report")


class SuggestIn(BaseModel):
    strategy: str = Field(max_length=64)
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    n_trials: int = Field(20, ge=5, le=60)
    seed: int = Field(0, ge=0, le=2**31)
    initial_capital: float | None = Field(None, gt=0)
    static_fx: dict[str, float] = Field(default_factory=dict)


@router.post("/ai/proposals")
def suggest(body: SuggestIn, _: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    capital = _capital(body.initial_capital, rt)
    ds = _dataset(session, rt, body.instrument_id, body.timeframe)
    return guard(ai.create_proposals, session, ds, body.strategy, body.instrument_id, body.timeframe, capital, body.static_fx,
                 rt.user_settings.risk, body.n_trials, body.seed)


@router.get("/ai/proposals")
def proposals(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return ai.list_items(session, "proposal")


class ApplyIn(BaseModel):
    broker_id: str = Field(max_length=64)


@router.post("/ai/proposals/{proposal_id}/apply")
def apply_proposal(proposal_id: str, body: ApplyIn, user: User = Depends(admin), session: Session = Depends(get_session),
                   rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Administrator applies a VALIDATED proposal. The deployment is created or updated but its
    active/inactive state is never changed here, and nothing but strategy parameters is touched."""
    row = guard(ai.proposal_for_apply, session, proposal_id)
    p = row.payload
    existing = session.scalar(select(StrategyDeployment).where(
        StrategyDeployment.strategy == p["strategy"], StrategyDeployment.instrument_id == p["instrument_id"],
        StrategyDeployment.broker_account_id == body.broker_id))
    session.commit()
    if existing is not None:
        guard(rt.update_deployment, existing.id, user.email, params=p["params"])
        deployment_id = existing.id
    else:
        deployment_id = guard(rt.add_deployment, p["strategy"], p["params"], p["instrument_id"], p["timeframe"], body.broker_id, user.email)
    row = session.get(ResearchItem, proposal_id)
    row.status = "applied"
    audit.record(session, user.email, "ai.proposal_applied", proposal_id=proposal_id, deployment_id=deployment_id, params=p["params"])
    return {"deployment_id": deployment_id, "activated": False}


@router.post("/ai/proposals/{proposal_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(proposal_id: str, user: User = Depends(trader), session: Session = Depends(get_session)) -> None:
    row = session.get(ResearchItem, proposal_id)
    if row is None or row.kind != "proposal":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown proposal")
    row.status = "dismissed"
    audit.record(session, user.email, "ai.proposal_dismissed", proposal_id=proposal_id)


@router.get("/ai/anomalies")
def anomalies(instrument_id: str, timeframe: str, _: User = Depends(viewer), session: Session = Depends(get_session),
              rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    from ..quant_ai import analyst

    return analyst.detect_anomalies(_dataset(session, rt, instrument_id, timeframe).bars)

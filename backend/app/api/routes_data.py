"""Data platform API: providers, ingestion, catalog and quality, stored datasets, point-in-time views and features.

Reads are open to viewers; starting an ingestion or copying a series into the backtest store needs a trader.
Nothing in this router can place, change or cancel an order.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtesting import service as bt
from ..database.models import StrategyDeployment, User
from ..datahub import features, views
from ..datahub.http import SourceDisabled, SourceError
from ..datahub.lake import DATASETS
from ..datahub.providers import PROBES
from ..datahub.service import SUPPORTED, DataHub, DataHubError
from ..execution.runtime import TradingRuntime
from ..market_data.timeframes import OHLCV, TIMEFRAMES
from ..monitoring import audit
from ..security.roles import Role
from .deps import Container, get_container, get_runtime, get_session, require_role

router = APIRouter(prefix="/api/data", tags=["data platform"])
viewer, trader = require_role(Role.VIEWER), require_role(Role.TRADER)

def get_hub(container: Container = Depends(get_container)) -> DataHub:
    return container.datahub


def guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc).strip("'\"")) from None
    except SourceDisabled as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except (DataHubError, SourceError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


def _list(text: str | None) -> list[str] | None:
    return [p.strip() for p in text.split(",") if p.strip()] if text else None


# ---------------------------------------------------------------- providers and monitoring
@router.get("/providers")
def providers(_: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> list[dict]:
    """Every source: what it offers for free, its limits and licence, whether it is ready, usage and recent availability."""
    return hub.provider_status()


@router.post("/providers/{name}/test")
def test_provider(name: str, user: User = Depends(trader), hub: DataHub = Depends(get_hub)) -> dict:
    """One small real request to the provider. The rows it returns are stored like any other."""
    if name not in PROBES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown data source {name}")
    probe = PROBES[name]
    out = guard(hub.ingest, name, probe["dataset"], probe["keys"], probe.get("timeframe", ""), actor=user.email, **probe.get("options", {}))
    result = out["results"][0]
    return {"ok": result["status"] in ("ok", "empty"), "status": result["status"], "message": result["message"] or f"{result['rows_in']} rows received",
            "requests": out["requests"], "seconds": out["seconds"], "quality": result["quality"]}


@router.get("/datasets")
def datasets(_: User = Depends(viewer)) -> dict:
    return {"datasets": [{"name": s.name, "description": s.description, "series_key": list(s.parts), "row_key": list(s.keys), "event_time": s.time}
                         for s in DATASETS.values()],
            "sources": {name: list(sets) for name, sets in SUPPORTED.items()}, "feature_sets": features.describe()}


@router.get("/catalog")
def catalog(dataset: str | None = None, provider: str | None = None, _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> list[dict]:
    """Stored series with coverage and freshness (`stale` = newer data is overdue for the series' cadence)."""
    return hub.catalog(dataset, provider)


@router.get("/runs")
def runs(limit: int = Query(100, ge=1, le=1000), provider: str | None = None, dataset: str | None = None, _: User = Depends(viewer),
         hub: DataHub = Depends(get_hub)) -> list[dict]:
    """Ingestion log: requests, timings, rows written and the data-quality report of each batch."""
    return hub.runs(limit, provider, dataset)


@router.get("/health")
def health(hours: int = Query(24, ge=1, le=720), _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    return hub.health(hours)


# ---------------------------------------------------------------- ingestion
class IngestIn(BaseModel):
    provider: str = Field(max_length=24)
    dataset: str = Field(max_length=32)
    keys: list[str] | None = Field(None, max_length=200)  # symbols, series ids, tickers; empty = the configured defaults
    timeframe: str = Field("", max_length=8)
    start: str | None = Field(None, max_length=40)
    end: str | None = Field(None, max_length=40)
    days: int | None = Field(None, ge=1, le=4000)  # lookback when nothing is stored yet and no start is given
    depth: int | None = Field(None, ge=1, le=500)
    wait: bool = False  # true: answer when finished (small requests only)


@router.post("/ingest")
def ingest(body: IngestIn, user: User = Depends(trader), hub: DataHub = Depends(get_hub), session: Session = Depends(get_session)) -> dict:
    """Fetch data from a provider into the lake. Runs in the background unless `wait` is set; follow it with GET /api/data/jobs/{id}."""
    options = {k: v for k, v in (("days", body.days), ("depth", body.depth)) if v is not None}
    audit.record(session, user.email, "data.ingest", provider=body.provider, dataset=body.dataset, keys=(body.keys or [])[:20], timeframe=body.timeframe)
    if body.wait:
        return guard(hub.ingest, body.provider, body.dataset, body.keys, body.timeframe, body.start, body.end, actor=user.email, **options)
    return guard(hub.start, body.provider, body.dataset, body.keys, body.timeframe, body.start, body.end, actor=user.email, **options)


@router.get("/jobs/{job_id}")
def job(job_id: str, _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    return guard(hub.job, job_id)


# ---------------------------------------------------------------- stored data
@router.get("/bars")
def bars(source: str, symbol: str, timeframe: str, start: str | None = None, end: str | None = None, limit: int = Query(1000, ge=1, le=5000),
         _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    return guard(views.bars, hub, source, symbol, timeframe, start, end, limit)


@router.get("/compare")
def compare(series: str, timeframe: str = "1m", start: str | None = None, end: str | None = None, limit: int = Query(500, ge=1, le=5000),
            window: int = Query(240, ge=10, le=10000), _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    """Price divergence between venues. `series` is a comma-separated list such as `binance:BTC/USDT,kraken:BTC/USD`."""
    return guard(views.compare, hub, _list(series) or [], timeframe, start, end, limit, window)


@router.get("/macro")
def macro(asof: str | None = None, series: str | None = None, scheduled_release_times: bool = False, _: User = Depends(viewer),
          hub: DataHub = Depends(get_hub)) -> dict:
    """Macro figures as they were known at `asof` (default now). With `scheduled_release_times`, a release counts as known
    from its usual New York release time instead of the conservative 18:00."""
    from ..datahub.sources.fred import SERIES

    times = {s.id: s.release_time for s in SERIES.values() if s.release_time} if scheduled_release_times else None
    return guard(views.macro_state, hub, asof, _list(series), times)


@router.get("/macro/{series_id}")
def macro_series(series_id: str, vintages: bool = False, limit: int = Query(2000, ge=1, le=5000), _: User = Depends(viewer),
                 hub: DataHub = Depends(get_hub)) -> dict:
    return guard(views.macro_series, hub, series_id, vintages, limit)


@router.get("/fundamentals")
def fundamentals(ticker: str, asof: str | None = None, concepts: str | None = None, limit: int = Query(2000, ge=1, le=5000),
                 _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    """Reported figures of a company as known at `asof`: restatements appear only from their own filing time."""
    return guard(views.fundamentals, hub, ticker, asof, _list(concepts), limit)


@router.get("/filings")
def filings(ticker: str, forms: str | None = None, limit: int = Query(200, ge=1, le=2000), _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    return guard(views.filings, hub, ticker, _list(forms), limit)


@router.get("/features")
def feature_sets(_: User = Depends(viewer)) -> list[dict]:
    return features.describe()


@router.get("/features/{name}")
def feature(name: str, source: str, symbol: str, timeframe: str = "1m", start: str | None = None, end: str | None = None,
            limit: int = Query(500, ge=1, le=5000), _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    return guard(views.feature_rows, hub, name, source, symbol, timeframe, start, end, limit)


@router.get("/context")
def context(asof: str | None = None, _: User = Depends(viewer), hub: DataHub = Depends(get_hub)) -> dict:
    """Structured market context for the AI research layer: macro state, cross-venue prices, coverage, stale series, caveats."""
    return guard(views.context, hub, asof)


# ---------------------------------------------------------------- bridge to backtesting
class PromoteIn(BaseModel):
    source: str = Field(max_length=24)
    symbol: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    instrument_id: str = Field(max_length=64)
    start: str | None = Field(None, max_length=40)
    end: str | None = Field(None, max_length=40)
    replace: bool = False


@router.post("/promote")
def promote(body: PromoteIn, user: User = Depends(trader), hub: DataHub = Depends(get_hub), session: Session = Depends(get_session),
            rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Copy a bar series from the lake into the platform's bar store, so backtests and research on an instrument can use it.

    The same rules as a CSV import apply (validated, never merged into an existing series), and it is refused for an
    instrument that a strategy is deployed on or that has a live subscription: those are fed by their own source."""
    def run() -> dict:
        inst = rt.registry.get(body.instrument_id)
        if body.timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {body.timeframe}")
        if rt.kinds.get(inst.id) == "simulated":
            raise ValueError("A simulated replay is driving the paper engine for this instrument; reset the paper account first")
        deployed = session.scalar(select(StrategyDeployment.id).where(StrategyDeployment.instrument_id == inst.id).limit(1))  # active or paused
        if deployed is not None or inst.id in rt.live.subscribed:
            raise PermissionError(f"{inst.id} is in use by a deployment or a live subscription; its stored bars are not replaced from the lake")
        frame = hub.lake.bars(body.source, body.symbol, body.timeframe, views._ts(body.start), views._ts(body.end))
        if frame.empty:
            raise LookupError(f"No stored bars for {body.source} {body.symbol} {body.timeframe}. Ingest them first.")
        out = bt.import_bars_series(session, inst, body.timeframe, frame[OHLCV], f"lake:{body.source}:{body.symbol}"[:64], body.replace)
        audit.record(session, user.email, "data.promoted", instrument_id=inst.id, timeframe=body.timeframe, source=body.source, symbol=body.symbol,
                     bars=out["bars_stored"])
        return {**out, "instrument_id": inst.id, "source": f"lake:{body.source}:{body.symbol}"}

    try:
        return guard(run)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

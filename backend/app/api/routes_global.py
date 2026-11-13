"""Global Markets endpoints: exchange directory, security catalog, synchronisation, coverage report."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database.models import User
from ..execution.runtime import RuntimeError_, TradingRuntime
from ..global_markets import catalog
from ..global_markets.discovery import SECURITY_TYPES
from ..security.roles import Role
from ..security.vault import VaultError
from .deps import get_runtime, get_session, require_role

router = APIRouter(prefix="/api/global", tags=["global markets"])
viewer, trader = require_role(Role.VIEWER), require_role(Role.TRADER)


@router.get("/coverage")
def coverage(_: User = Depends(viewer), session: Session = Depends(get_session)) -> dict:
    return catalog.coverage_report(session)


@router.get("/providers")
def providers(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    return rt.global_markets.providers()


@router.get("/securities")
def securities(q: str = "", exchange: list[str] = Query(default=[]), country: list[str] = Query(default=[]), type: list[str] = Query(default=[]),
               listing_status: str = "active", has_prices: bool | None = None, limit: int = 100, offset: int = 0,
               _: User = Depends(viewer), session: Session = Depends(get_session)) -> dict:
    if any(t not in SECURITY_TYPES for t in type):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"type must be one of: {', '.join(SECURITY_TYPES)}")
    if listing_status not in ("active", "delisted", "all"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "listing_status must be active, delisted or all")
    return catalog.search(session, q, exchange or None, [c.upper() for c in country] or None, type or None,
                          None if listing_status == "all" else listing_status, has_prices, limit, offset)


@router.get("/securities/{security_id}")
def security(security_id: int, _: User = Depends(viewer), session: Session = Depends(get_session)) -> dict:
    out = catalog.profile(session, security_id)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown security")
    return out


class SyncIn(BaseModel):
    provider: str = Field(max_length=24)
    exchanges: list[str] | None = Field(None, max_length=100)
    enrich_identifiers: bool = True
    identifier_limit: int = Field(3000, ge=0, le=200_000)


@router.post("/sync")
def start_sync(body: SyncIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    try:
        return rt.global_markets.start_sync(body.provider, user.email, body.exchanges, body.enrich_identifiers, body.identifier_limit)
    except (ValueError, RuntimeError_, VaultError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/sync")
def sync_status(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return rt.global_markets.status()


@router.post("/sync/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_sync(_: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    rt.global_markets.cancel()


class ActivateIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


@router.post("/securities/activate")
def activate(body: ActivateIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return rt.global_markets.activate(body.ids, user.email)


# ---------------------------------------------------------------- analysis and rankings
def _frame(session: Session, rt: TradingRuntime):
    from ..global_markets.scanner import universe_frame

    return universe_frame(session, rt.registry)


@router.get("/overview")
def overview(group_by: str = "market", market: list[str] = Query(default=[]), _: User = Depends(viewer), session: Session = Depends(get_session),
             rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..global_markets import rankings

    try:
        return rankings.market_overview(_frame(session, rt), group_by, market or None)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/ranking-models")
def ranking_models(_: User = Depends(viewer)) -> list[dict]:
    from ..global_markets import rankings

    return rankings.describe_models()


@router.get("/rankings")
def get_rankings(model: str = "multi_factor", market: list[str] = Query(default=[]), min_price: float = 1.0, min_dollar_volume: float = 1_000_000.0,
                 limit: int = 100, _: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..database.models import ResearchItem
    from ..global_markets import rankings
    from sqlalchemy import select

    try:
        out = rankings.rank(_frame(session, rt), model, min_price=min_price, min_dollar_volume=min_dollar_volume, markets=market or None)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    out["rows"] = out["rows"][: max(1, min(limit, 2000))]
    last = session.scalar(select(ResearchItem).where(ResearchItem.kind == "ranking_test", ResearchItem.title == model).order_by(ResearchItem.created_at.desc()))
    out["validation"] = ({"status": last.status, "tested_at": last.created_at.isoformat(), **last.payload.get("summary", {})} if last else
                         {"status": "not tested", "note": "This ranking has not been backtested on your stored data yet. Run it under Global strategies."})
    return out


class ReportIn(BaseModel):
    group_by: str = Field("market", pattern="^(market|country|sector)$")
    model: str | None = Field(None, max_length=32)
    markets: list[str] = Field(default_factory=list, max_length=50)


@router.post("/report")
def report(body: ReportIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    import uuid

    from ..database.models import ResearchItem
    from ..global_markets import rankings

    frame = _frame(session, rt)
    try:
        ov = rankings.market_overview(frame, body.group_by, body.markets or None)
        ranking = rankings.rank(frame, body.model, markets=body.markets or None) if body.model else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    markdown = rankings.overview_report(ov, ranking)
    title = f"Global market report {ov.get('as_of', '')}".strip()
    row = ResearchItem(id=uuid.uuid4().hex, kind="report", status="done", title=title, payload={"title": title, "markdown": markdown, "reproduced": None})
    session.add(row)
    return {"id": row.id, "title": title, "markdown": markdown}


# ---------------------------------------------------------------- cross-sectional strategies and portfolio
@router.get("/strategies/options")
def strategy_options(_: User = Depends(viewer)) -> dict:
    from ..global_markets import cross_section

    return cross_section.describe_options()


def _spec(body: dict):
    from pydantic import ValidationError

    from ..global_markets.cross_section import StrategySpec

    try:
        return StrategySpec.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from None


@router.post("/strategies/backtest")
def strategy_backtest(body: dict, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    import uuid

    from ..database.models import ResearchItem
    from ..global_markets import cross_section

    spec = _spec(body)
    try:
        result = cross_section.backtest(cross_section.load_panel(session, rt.registry, spec.markets or None, spec.start), spec)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    m, u = result["metrics"], result["universe"]
    summary = {"period": f"{u['test_start']} to {u['end']}", "instruments": u["instruments"], "total_return": m["total_return"], "sharpe_ratio": m["sharpe_ratio"],
               "max_drawdown": m["max_drawdown"], "benchmark_total_return": result["benchmark"]["total_return"]}
    row = ResearchItem(id=uuid.uuid4().hex, kind="ranking_test", status="tested", title=spec.model, payload={"summary": summary, "definition": spec.model_dump()})
    session.add(row)
    return {"id": row.id, **result}


@router.get("/strategies/tests")
def strategy_tests(limit: int = 30, _: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    from sqlalchemy import select

    from ..database.models import ResearchItem

    rows = session.scalars(select(ResearchItem).where(ResearchItem.kind == "ranking_test").order_by(ResearchItem.created_at.desc()).limit(max(1, min(limit, 200))))
    return [{"id": r.id, "model": r.title, "tested_at": r.created_at.isoformat(), **r.payload["summary"], "definition": r.payload["definition"]} for r in rows]


@router.post("/portfolio/target")
def portfolio_target(body: dict, _: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..global_markets import cross_section

    spec = _spec(body)
    try:
        return cross_section.target_portfolio(cross_section.load_panel(session, rt.registry, spec.markets or None), spec)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


class ProposalIn(BaseModel):
    broker_id: str = Field(min_length=1, max_length=64)
    definition: dict


@router.post("/portfolio/proposals", status_code=status.HTTP_201_CREATED)
def create_proposal(body: ProposalIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..global_markets import rebalance

    try:
        return rebalance.propose(rt, session, _spec(body.definition), body.broker_id, user.email)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/portfolio/proposals")
def list_proposals(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    from ..global_markets import rebalance

    return rebalance.proposals(session)


@router.post("/portfolio/proposals/{proposal_id}/execute")
def execute_proposal(proposal_id: str, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..global_markets import rebalance

    try:
        return rebalance.execute(rt, session, proposal_id, user.email)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/portfolio/holdings")
def portfolio_holdings(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..global_markets import rebalance

    return rebalance.holdings(rt)


@router.post("/portfolio/settle")
def portfolio_settle(_: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Advance paper orders and positions of global strategies on the daily bars stored so far."""
    return rt.drive_stored()

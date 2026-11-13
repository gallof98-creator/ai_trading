"""Chart datafeed endpoints (shaped after TradingView's Datafeed API) and paper orders placed from the chart."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..charts import datafeed
from ..database.models import User
from ..execution.runtime import MANUAL_STRATEGY, RuntimeError_, TradingRuntime
from ..security.roles import Role
from .deps import get_runtime, get_session, require_role

router = APIRouter(prefix="/api/charts", tags=["charts"])
viewer, trader = require_role(Role.VIEWER), require_role(Role.TRADER)


def _resolved(session: Session, rt: TradingRuntime, symbol: str) -> dict:
    try:
        return datafeed.resolve(session, rt.registry, symbol)
    except datafeed.DatafeedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


def _instrument(session: Session, rt: TradingRuntime, symbol: str) -> str:
    info = _resolved(session, rt, symbol)
    if not info["instrument_id"]:
        raise HTTPException(status.HTTP_409_CONFLICT, info["data"]["note"])
    return info["instrument_id"]


@router.get("/config")
def config(_: User = Depends(viewer)) -> dict:
    return datafeed.config()


@router.get("/search")
def search(q: str = "", exchange: list[str] = Query(default=[]), type: list[str] = Query(default=[]), sector: str | None = None,
           has_data: bool | None = None, limit: int = 30, _: User = Depends(viewer), session: Session = Depends(get_session),
           rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    return datafeed.search(session, rt.registry, q, exchange or None, type or None, sector, has_data, limit)


@router.get("/symbols")
def symbol(symbol: str, _: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return _resolved(session, rt, symbol)


@router.get("/history")
def history(symbol: str, resolution: str, to: int | None = None, countback: int | None = Query(default=None, ge=1, le=datafeed.MAX_BARS),
            from_: int | None = Query(default=None, alias="from"), _: User = Depends(viewer), session: Session = Depends(get_session),
            rt: TradingRuntime = Depends(get_runtime)) -> dict:
    iid = _instrument(session, rt, symbol)
    try:
        return datafeed.history(session, iid, resolution, from_, to, countback)
    except datafeed.DatafeedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/studies")
def studies(symbol: str, resolution: str, bars: int = Query(default=600, ge=50, le=2000), _: User = Depends(viewer),
            session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    iid = _instrument(session, rt, symbol)
    try:
        return datafeed.studies(session, iid, resolution, bars)
    except datafeed.DatafeedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/position")
def position(symbol: str, _: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Everything the platform holds or has pending in this instrument, across strategies."""
    iid = _instrument(session, rt, symbol)
    positions = []
    for (broker, inst, strategy), s in rt.portfolio.slices.items():
        if inst == iid and s.qty:
            prot = rt.engine.protections.get((broker, inst, strategy))
            positions.append({"broker_id": broker, "strategy": strategy, "qty": s.qty, "avg_price": s.avg_price, "stop_price": prot.stop_price if prot else None,
                              "from_chart": strategy == MANUAL_STRATEGY})
    orders = [{"broker_id": o.broker_id, "side": o.side.value, "qty": o.qty, "type": o.type.value, "stop_price": o.stop_price, "limit_price": o.limit_price,
               "strategy": o.strategy, "reason": o.reason} for o in rt.engine.orders.values() if o.is_open and o.instrument_id == iid]
    return {"instrument_id": iid, "positions": positions, "open_orders": orders}


class ChartOrderIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    broker_id: str = Field(min_length=1, max_length=64)
    action: Literal["buy", "close"]
    qty: float | None = Field(None, gt=0)
    stop_pct: float | None = Field(None, gt=0, lt=1)


@router.post("/orders", status_code=status.HTTP_201_CREATED)
def place(body: ChartOrderIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    iid = _instrument(session, rt, body.symbol)
    try:
        return rt.chart_order(body.broker_id, iid, body.action, body.qty, body.stop_pct, user.email)
    except RuntimeError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

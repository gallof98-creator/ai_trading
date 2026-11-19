"""Trade analytics: performance by asset class x strategy x exit reason, in the base currency and in R; shadow exits."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..database.models import User
from ..execution.runtime import TradingRuntime
from ..security.roles import Role
from .deps import get_runtime, require_role

router = APIRouter(prefix="/api/analytics", tags=["trade analytics"])
viewer = require_role(Role.VIEWER)
trader = require_role(Role.TRADER)
GROUPS = {"asset_class", "strategy", "exit_reason", "instrument_id", "broker_id", "timeframe"}


@router.get("/trades")
def trades(by: str = Query("asset_class,strategy,exit_reason", max_length=200), days: int | None = Query(None, ge=1, le=3650),
           _: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Expectancy, profit factor, average win/loss, drawdown, P&L without the best 3 trades, share of manual exits and cost
    per trade, per group, in the base currency and in R; manual exits against their shadow exits."""
    keys = tuple(k.strip() for k in by.split(",") if k.strip())
    if not keys or any(k not in GROUPS for k in keys):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"group by any of: {', '.join(sorted(GROUPS))}")
    with rt.lock:
        return rt.analytics.report(keys, since=rt.clock() - timedelta(days=days) if days else None)


@router.post("/refresh")
def refresh(_: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Fill in what older trades lack (base P&L, initial risk, shadows) and follow the open shadow exits on the stored prices."""
    with rt.lock:
        out = {"backfill": rt.analytics.backfill(), "shadows": rt.analytics.update_shadows()}
        rt.persist()
    return out

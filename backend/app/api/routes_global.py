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

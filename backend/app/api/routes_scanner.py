"""Market scanner, saved scans and watchlists."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import SavedScan, User, Watchlist
from ..execution.runtime import TradingRuntime
from ..global_markets import scanner
from ..global_markets.metrics import DERIVED, FIELDS, META
from ..monitoring import audit
from ..security.roles import Role
from .deps import get_runtime, get_session, require_role

router = APIRouter(prefix="/api/scanner", tags=["scanner"])
viewer, trader = require_role(Role.VIEWER), require_role(Role.TRADER)


def _lists(session: Session) -> dict[str, list[str]]:
    return {w.name: w.instrument_ids for w in session.scalars(select(Watchlist))}


@router.get("/fields")
def fields(_: User = Depends(viewer)) -> dict:
    def pack(group: dict) -> list[dict]:
        return [{"name": k, "label": v[0], "type": v[1], "description": v[2]} for k, v in group.items()]

    return {"metrics": pack(FIELDS), "derived": pack(DERIVED), "descriptive": pack(META), "operators": sorted(scanner.OPS), "presets": scanner.PRESETS}


class ScanIn(BaseModel):
    definition: dict


@router.post("/run")
def run(body: ScanIn, _: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    try:
        return scanner.run_scan(scanner.universe_frame(session, rt.registry), body.definition, _lists(session))
    except scanner.ScanError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


class MetricsIn(BaseModel):
    force: bool = False


@router.post("/metrics")
def compute(body: MetricsIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    try:
        return rt.global_markets.start_metrics(user.email, body.force)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/metrics")
def metrics_status(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return rt.global_markets.metrics_status()


class SavedScanIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    definition: dict
    run_after_update: bool = False
    notify: bool = True


def _scan_out(s: SavedScan) -> dict:
    return {"id": s.id, "name": s.name, "definition": s.definition, "run_after_update": s.run_after_update, "notify": s.notify,
            "last_run": s.last_run.isoformat() if s.last_run else None, "last_result": s.last_result}


@router.get("/saved")
def saved(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return [_scan_out(s) for s in session.scalars(select(SavedScan).order_by(SavedScan.name))]


@router.post("/saved", status_code=status.HTTP_201_CREATED)
def save(body: SavedScanIn, user: User = Depends(trader), session: Session = Depends(get_session)) -> dict:
    try:
        definition = scanner.validate_definition(body.definition)
    except scanner.ScanError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    row = session.scalar(select(SavedScan).where(SavedScan.name == body.name))
    if row is None:
        row = SavedScan(name=body.name, definition=definition)
        session.add(row)
    row.definition, row.run_after_update, row.notify = definition, body.run_after_update, body.notify
    session.flush()
    audit.record(session, user.email, "scanner.scan_saved", name=body.name, run_after_update=body.run_after_update)
    return _scan_out(row)


@router.delete("/saved/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scan(scan_id: int, user: User = Depends(trader), session: Session = Depends(get_session)) -> None:
    row = session.get(SavedScan, scan_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown scan")
    session.delete(row)
    audit.record(session, user.email, "scanner.scan_deleted", name=row.name)


class WatchlistIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    instrument_ids: list[str] = Field(default_factory=list, max_length=5000)


@router.get("/watchlists")
def watchlists(_: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    return [{"id": w.id, "name": w.name, "instrument_ids": w.instrument_ids} for w in session.scalars(select(Watchlist).order_by(Watchlist.name))]


@router.put("/watchlists")
def put_watchlist(body: WatchlistIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    known = {i.id for i in rt.registry.all()}
    unknown = [i for i in body.instrument_ids if i not in known]
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown instruments: {', '.join(unknown[:5])}")
    row = session.scalar(select(Watchlist).where(Watchlist.name == body.name))
    if row is None:
        row = Watchlist(name=body.name)
        session.add(row)
    row.instrument_ids = list(dict.fromkeys(body.instrument_ids))
    session.flush()
    return {"id": row.id, "name": row.name, "instrument_ids": row.instrument_ids}


@router.delete("/watchlists/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watchlist(watchlist_id: int, _: User = Depends(trader), session: Session = Depends(get_session)) -> None:
    row = session.get(Watchlist, watchlist_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown watchlist")
    session.delete(row)

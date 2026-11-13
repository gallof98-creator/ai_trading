"""Trading, portfolio, execution and risk endpoints. Routes only translate HTTP <-> runtime calls."""
from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..database.models import AuditEntry, FillRow, Notification, OrderRow, RiskEvent, SignalRow, StrategyDeployment, User
from ..execution.runtime import RuntimeError_, TradingRuntime
from ..instruments.registry import default_instruments
from ..market_data.fx import ECB_DAILY_URL, parse_ecb_reference_rates
from ..security.roles import Role
from ..strategies.registry import list_strategies
from .deps import get_runtime, get_session, require_role

router = APIRouter(prefix="/api", tags=["trading"])
viewer, trader, admin = require_role(Role.VIEWER), require_role(Role.TRADER), require_role(Role.ADMIN)
ID = r"^[a-z0-9][a-z0-9_-]{1,62}$"


def guard(fn, *args, **kwargs):
    """Translate domain refusals into HTTP 400 with the user-facing reason."""
    try:
        return fn(*args, **kwargs)
    except (RuntimeError_, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


# ---------------------------------------------------------------- overview / portfolio
@router.get("/overview")
def overview(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return rt.overview()


@router.get("/portfolio/equity")
def equity(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    return rt.equity_history()


@router.get("/trades")
def trades(limit: int = 200, _: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    with rt.lock:
        return [t.to_dict() for t in rt.portfolio.trades[-max(1, min(limit, 2000)):]][::-1]


@router.get("/instruments")
def instruments(q: str = "", limit: int = 300, has_data: bool | None = None, _: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime),
                session: Session = Depends(get_session)) -> list[dict]:
    """Instruments, those with stored data first. `q` filters by text; the list is capped at `limit`
    (the universe can hold thousands of stocks) - use /api/instruments/stats for totals."""
    from ..market_data.store import BarStore

    series: dict[str, list] = {}
    for c in BarStore().coverage(session):
        series.setdefault(c["instrument_id"], []).append({"timeframe": c["timeframe"], "bars": c["bars"], "kind": c["kind"]})
    needle = q.strip().upper()
    with rt.lock:
        builtin = {i.id for i in default_instruments()}
        rows = [i for i in rt.registry.all() if (not needle or needle in i.id.upper()) and (has_data is None or (i.id in series) == has_data)]
        rows.sort(key=lambda i: (i.id not in series and i.id not in builtin, i.id))
        return [{**i.model_dump(mode="json"), "last_price": rt.portfolio.prices.get(i.id), "data_kind": rt.kinds.get(i.id),
                 "feed_status": rt.feed_status.get(i.id), "removable": i.id not in builtin, "series": series.get(i.id, [])}
                for i in rows[: max(1, min(limit, 2000))]]


@router.get("/instruments/stats")
def instrument_stats(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime), session: Session = Depends(get_session)) -> dict:
    from ..instruments.registry import market_of
    from ..market_data.store import BarStore

    with_data = {c["instrument_id"] for c in BarStore().coverage(session)}
    by_market: dict[str, dict] = {}
    with rt.lock:
        everything = rt.registry.all()
    for i in everything:
        m = by_market.setdefault(market_of(i) or i.asset_class.value, {"instruments": 0, "with_data": 0})
        m["instruments"] += 1
        m["with_data"] += i.id in with_data
    return {"total": len(everything), "with_data": sum(1 for i in everything if i.id in with_data), "by_market": by_market}


@router.get("/markets")
def markets(_: User = Depends(viewer)) -> list[dict]:
    from ..instruments.registry import MARKETS

    return [{"code": code, "label": m["label"], "currency": m["currency"], "suffix": m["suffix"]} for code, m in MARKETS.items()]


class InstrumentsIn(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=2000)
    market: str = Field(max_length=4)
    asset_class: Literal["stock", "etf"] = "stock"


@router.post("/instruments")
def add_instruments(body: InstrumentsIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    from ..instruments.registry import MARKETS

    if body.market not in MARKETS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown market; supported: {', '.join(MARKETS)}")
    return rt.add_instruments(body.tickers, body.market, body.asset_class, user.email)


@router.delete("/instruments/{instrument_id:path}", status_code=status.HTTP_204_NO_CONTENT)
def remove_instrument(instrument_id: str, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    guard(rt.remove_instrument, instrument_id, user.email)


# ---------------------------------------------------------------- strategies / deployments
@router.get("/strategies")
def strategies(_: User = Depends(viewer)) -> list[dict]:
    return list_strategies()


class DeploymentIn(BaseModel):
    strategy: str = Field(max_length=64)
    params: dict = Field(default_factory=dict)
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    broker_id: str = Field(max_length=64)


class DeploymentPatch(BaseModel):
    active: bool | None = None
    params: dict | None = None


@router.get("/deployments")
def deployments(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime), session: Session = Depends(get_session)) -> list[dict]:
    out = []
    with rt.lock:
        for row in session.scalars(select(StrategyDeployment).order_by(StrategyDeployment.id)):
            key = (row.broker_account_id, row.instrument_id, row.strategy)
            pnl = [t.pnl for t in rt.portfolio.trades if (t.broker_id, t.instrument_id, t.strategy) == key]
            out.append({"id": row.id, "strategy": row.strategy, "version": row.strategy_version, "instrument_id": row.instrument_id,
                        "timeframe": row.timeframe, "broker_id": row.broker_account_id, "params": row.params, "is_active": row.is_active,
                        "position": rt.portfolio.qty(key), "trades": len(pnl), "net_profit": sum(pnl),
                        "win_rate": sum(p > 0 for p in pnl) / len(pnl) if pnl else None})
    return out


@router.post("/deployments", status_code=status.HTTP_201_CREATED)
def create_deployment(body: DeploymentIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return {"id": guard(rt.add_deployment, body.strategy, body.params, body.instrument_id, body.timeframe, body.broker_id, user.email)}


@router.patch("/deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
def patch_deployment(deployment_id: int, body: DeploymentPatch, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    guard(rt.update_deployment, deployment_id, user.email, active=body.active, params=body.params)


@router.delete("/deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_deployment(deployment_id: int, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    guard(rt.delete_deployment, deployment_id, user.email)


@router.get("/deployments/{deployment_id}/history")
def deployment_history(deployment_id: int, _: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(AuditEntry).where(AuditEntry.action.like("deployment.%")).order_by(AuditEntry.id.desc()).limit(2000))
    return [{"ts": r.ts.isoformat(), "actor": r.actor, "action": r.action, "detail": r.detail} for r in rows if r.detail.get("deployment_id") == deployment_id]


# ---------------------------------------------------------------- execution
@router.get("/orders")
def orders(limit: int = 100, _: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(OrderRow).order_by(OrderRow.updated_at.desc()).limit(max(1, min(limit, 1000))))
    return [{**r.payload, "is_simulated": r.is_simulated} for r in rows]


@router.get("/fills")
def fills(limit: int = 100, _: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(select(FillRow, OrderRow).join(OrderRow, OrderRow.id == FillRow.order_id).order_by(FillRow.id.desc()).limit(max(1, min(limit, 1000))))
    return [{"ts": f.ts.isoformat(), "order_id": f.order_id, "qty": float(f.qty), "price": float(f.price), "commission": float(f.commission),
             "instrument_id": o.instrument_id, "side": o.side, "broker_id": o.broker_account_id, "is_simulated": o.is_simulated,
             "reason": (o.payload.get("reason") or "").split(" | ")[0], "strategy": o.payload.get("strategy")} for f, o in rows]


@router.get("/signals")
def signals(limit: int = 100, _: User = Depends(viewer), session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(SignalRow).order_by(SignalRow.ts.desc()).limit(max(1, min(limit, 1000))))
    return [r.payload for r in rows]


@router.get("/approvals")
def approvals(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    with rt.lock:
        return [{"id": a.id, "created_at": a.created_at.isoformat(), "order": a.order.model_dump(mode="json"), "reference_price": a.intent.price,
                 "stop_distance": a.intent.stop_distance, "is_simulated": rt.brokers.get(a.order.broker_id).capabilities.is_simulated}
                for a in rt.engine.approvals.values()]


class ApprovalDecision(BaseModel):
    approve: bool


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: ApprovalDecision, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return guard(rt.resolve_approval, approval_id, body.approve, user.email).model_dump(mode="json")


@router.get("/brokers/status")
def broker_status(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    return rt.broker_status()


class PaperAccountIn(BaseModel):
    id: str = Field(pattern=ID)
    label: str = Field(min_length=1, max_length=128)


class ResetIn(BaseModel):
    confirmation: str = Field(max_length=64)


class ReplayIn(BaseModel):
    instrument_id: str = Field(max_length=64)
    timeframe: str = Field(max_length=8)
    bars: int = Field(100, ge=1, le=1000)
    seed: int = Field(1, ge=0, le=2**31)
    fx_assumptions: dict[str, float] = Field(default_factory=dict)


@router.post("/paper/accounts", status_code=status.HTTP_201_CREATED)
def create_paper(body: PaperAccountIn, user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return guard(rt.create_paper_account, body.id, body.label, user.email)


@router.post("/paper/accounts/{account_id}/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_paper(account_id: str, body: ResetIn, user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> None:
    try:
        rt.reset_paper_account(account_id, body.confirmation, user.email)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.post("/paper/replay")
def replay(body: ReplayIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    if any(not 0 < v < 1e6 for v in body.fx_assumptions.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "FX assumptions must be positive rates")
    return guard(rt.replay_synthetic, body.instrument_id, body.timeframe, body.bars, body.seed, user.email, body.fx_assumptions)


@router.post("/brokers/{broker_id}/connect")
def connect(broker_id: str, user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return guard(rt.connect_broker, broker_id, user.email)


@router.post("/brokers/{broker_id}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(broker_id: str, user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> None:
    try:
        rt.disconnect_broker(broker_id, user.email)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.post("/feed/tick")
def tick(_: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return rt.tick()


# ---------------------------------------------------------------- risk
@router.get("/risk")
def risk(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime), session: Session = Depends(get_session)) -> dict:
    with rt.lock:
        limits = rt.user_settings.risk
        out = {"status": rt.risk.status(), "limits": limits.model_dump(), "utilisation": None}
        if rt.brokers.all():
            try:
                ov = rt.overview()
                eq = ov.get("equity") or 0
                if eq > 0 and "error" not in ov:
                    day_start = rt.portfolio.day_start_equity or eq
                    biggest = max((g["gross"] for g in ov["exposure_by_instrument"].values()), default=0.0)
                    out["utilisation"] = {
                        "daily_loss": {"value": max(0.0, 1 - eq / day_start), "limit": limits.max_daily_loss},
                        "drawdown": {"value": ov["drawdown"], "limit": limits.max_drawdown},
                        "gross_leverage": {"value": ov["gross_exposure"] / eq, "limit": limits.max_gross_leverage},
                        "margin": {"value": ov["margin_used"] / eq, "limit": limits.max_margin_utilization},
                        "largest_asset_exposure": {"value": biggest / eq, "limit": limits.max_exposure_per_asset},
                        "open_positions": {"value": len({(p["broker_id"], p["instrument_id"]) for p in ov["positions"]}), "limit": limits.max_concurrent_positions},
                    }
            except Exception as exc:  # the risk page must render even if valuation fails
                out["error"] = str(exc)
    events = session.scalars(select(RiskEvent).order_by(RiskEvent.id.desc()).limit(100))
    out["events"] = [{"ts": e.ts.isoformat(), "kind": e.kind, "severity": e.severity, "detail": e.detail} for e in events]
    return out


class KillSwitchIn(BaseModel):
    engage: bool
    reason: str = Field("", max_length=200)


@router.post("/risk/kill-switch", status_code=status.HTTP_204_NO_CONTENT)
def kill_switch(body: KillSwitchIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    # Anyone who trades may STOP trading; only an administrator may resume it.
    if not body.engage and Role(user.role) is not Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an administrator can release the kill switch")
    rt.kill_switch(body.engage, body.reason or "manual", user.email)


@router.post("/risk/breakers/{name}/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_breaker(name: str, user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> None:
    guard(rt.reset_breaker, name, user.email)


# ---------------------------------------------------------------- notifications / FX
@router.get("/notifications")
def notifications(limit: int = 100, _: User = Depends(viewer), session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(select(Notification).order_by(Notification.id.desc()).limit(max(1, min(limit, 500))))
    unread = session.scalar(select(func.count()).select_from(Notification).where(Notification.is_read.is_(False)))
    return {"unread": unread, "items": [{"id": n.id, "ts": n.ts.isoformat(), "category": n.category, "severity": n.severity, "title": n.title,
                                         "body": n.body, "is_read": n.is_read} for n in rows]}


@router.post("/notifications/read-all", status_code=status.HTTP_204_NO_CONTENT)
def read_all(_: User = Depends(viewer), session: Session = Depends(get_session)) -> None:
    session.execute(update(Notification).where(Notification.is_read.is_(False)).values(is_read=True))


class FxIn(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3,5}$")
    rate: float = Field(gt=0, lt=1e6, description="1 unit of `currency` in base currency")
    source: Literal["manual"] = "manual"


@router.get("/fx")
def fx(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    with rt.lock:
        return rt.fx_rates()


@router.post("/fx", status_code=status.HTTP_204_NO_CONTENT)
def add_fx(body: FxIn, user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> None:
    guard(rt.add_fx_rate, body.currency, rt.portfolio.base, body.rate, body.source, user.email)


@router.post("/fx/refresh-ecb")
def refresh_ecb(user: User = Depends(admin), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Fetch the ECB euro reference rates (base currency must be EUR)."""
    if rt.portfolio.base != "EUR":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ECB reference rates are quoted against EUR only")
    try:
        response = httpx.get(ECB_DAILY_URL, timeout=10)
        response.raise_for_status()
        rows = parse_ecb_reference_rates(response.text)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ECB rates unavailable: {exc}") from None
    for base, quote, ts, rate in rows:
        rt.add_fx_rate(base, quote, rate, "ecb", user.email, ts=ts)
    return {"rates": len(rows), "as_of": rows[0][2].isoformat() if rows else None}

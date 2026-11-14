"""Autonomous quant trading: strategy modules, validation, opportunity scan, the three universes, the dashboard summary."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..backtesting.service import DataUnavailable
from ..database.models import User
from ..execution.runtime import RuntimeError_, TradingRuntime
from ..quant_ai import opportunities
from ..security.roles import Role
from .deps import get_runtime, get_session, require_role

router = APIRouter(prefix="/api/quant", tags=["autonomous quant trading"])
viewer, trader = require_role(Role.VIEWER), require_role(Role.TRADER)


@router.get("/modules")
def modules(_: User = Depends(viewer)) -> dict:
    return opportunities.describe_modules()


@router.get("/universes")
def universes(_: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return opportunities.universes(session, rt)


class ValidateIn(BaseModel):
    strategy: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=2, max_length=4)


@router.post("/validations")
def validate(body: ValidateIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    try:
        return opportunities.validate(session, rt, body.strategy, body.instrument_id, body.timeframe, user.email)
    except (ValueError, KeyError, DataUnavailable, StopIteration) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc) or "Unsupported strategy, instrument or timeframe") from None


@router.get("/validations")
def list_validations(_: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> list[dict]:
    return opportunities.validations(session, rt.clock())


class ScanIn(BaseModel):
    broker_id: str = Field(min_length=1, max_length=64)
    instrument_ids: list[str] = Field(default_factory=list, max_length=500)
    min_traded_value: float = Field(0.0, ge=0)


@router.post("/scan")
def scan(body: ScanIn, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    try:
        return opportunities.scan(session, rt, body.broker_id, user.email, body.instrument_ids or None, body.min_traded_value)
    except (ValueError, RuntimeError_) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/dashboard")
def dashboard(_: User = Depends(viewer), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Everything the Autonomous Quant Trading page shows, in one consistent snapshot."""
    ov = rt.overview()
    s = rt.user_settings
    capital = s.initial_paper_capital
    by_strategy: dict[str, dict] = {}
    for t in rt.portfolio.trades:
        row = by_strategy.setdefault(t.strategy or "manual", {"strategy": t.strategy or "manual", "trades": 0, "wins": 0, "pnl_quote": 0.0})
        row["trades"] += 1
        row["wins"] += t.pnl > 0
        row["pnl_quote"] += t.pnl
    for row in by_strategy.values():
        row["win_rate"] = row["wins"] / row["trades"] if row["trades"] else None
    return {
        "configuration": {"virtual_capital": capital, "base_currency": s.base_currency, "trading_mode": s.trading_mode, "risk_per_trade": s.risk.risk_per_trade,
                          "risk_amount": capital * s.risk.risk_per_trade if capital else None, "proposal_ttl_minutes": s.proposal_ttl_minutes,
                          "safety_limits_confirmed": s.safety_limits_confirmed, "max_daily_loss": s.risk.max_daily_loss, "max_drawdown": s.risk.max_drawdown,
                          "live_trading_authorized": rt.settings.live_trading_authorized},
        "overview": ov, "proposals": rt.proposals(), "brokers": rt.broker_status(), "live": rt.live_hub.status(),
        "live_subscriptions": [{"instrument_id": i, "source": src, "quote": rt.live_hub.quote(i)} for i, src in sorted(rt.live.subscribed.items())],
        "universes": opportunities.universes(session, rt), "validations": opportunities.validations(session, rt.clock()),
        "deployments": [{"id": k, "strategy": d.strategy.name, "instrument_id": d.instrument_id, "timeframe": d.timeframe, "broker_id": d.broker_id}
                        for k, d in rt.deployments.items()],
        "performance_by_strategy": sorted(by_strategy.values(), key=lambda r: -r["pnl_quote"]),
        "positions": [{"broker_id": b, "instrument_id": i, "strategy": st, "qty": sl.qty, "avg_price": sl.avg_price, "price": rt.portfolio.prices.get(i),
                       "stop_price": (rt.engine.protections.get((b, i, st)).stop_price if rt.engine.protections.get((b, i, st)) else None)}
                      for (b, i, st), sl in rt.portfolio.slices.items() if sl.qty],
    }


class PauseIn(BaseModel):
    reason: str = Field("paused by the user", max_length=200)


@router.post("/pause", status_code=status.HTTP_204_NO_CONTENT)
def pause(body: PauseIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    """PAUSE TRADING and EMERGENCY STOP both engage the kill switch: no new risk can be taken. What happens to working
    orders and open positions follows the emergency policy chosen in the risk settings (default: nothing is liquidated)."""
    rt.kill_switch(True, body.reason, user.email)


@router.post("/resume", status_code=status.HTTP_204_NO_CONTENT)
def resume(user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    rt.kill_switch(False, "", user.email)


# ---------------------------------------------------------------- autopilot
@router.get("/autopilot")
def autopilot_status(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    return rt.autopilot.status()


@router.put("/autopilot")
def autopilot_configure(body: dict, user: User = Depends(trader), session: Session = Depends(get_session), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Change what the autopilot does. It cannot change the trading mode or a risk limit: those stay under Settings."""
    from pydantic import ValidationError

    from ..database.settings_store import AutopilotSettings, save_settings
    from ..monitoring import audit
    from ..strategies.registry import list_strategies

    try:
        new = AutopilotSettings.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from None
    known = {s["name"] for s in list_strategies()}
    unknown = [s for s in new.strategies if s not in known]
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown strategies: {', '.join(unknown)}")
    save_settings(session, rt.user_settings.model_copy(update={"autopilot": new}))
    audit.record(session, user.email, "autopilot.configured", enabled=new.enabled, cycle_minutes=new.cycle_minutes, max_instruments=new.max_instruments)
    session.flush()
    rt.reload_settings(session)
    return rt.autopilot.status()


@router.post("/autopilot/run", status_code=status.HTTP_202_ACCEPTED)
def autopilot_run(_: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Start one cycle now, in the background (it can take minutes: downloads and validations)."""
    import threading

    if rt.autopilot.state["running"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "A cycle is already running")
    threading.Thread(target=rt.autopilot.run_cycle, name="autopilot-cycle", daemon=True).start()
    return {"started": True}

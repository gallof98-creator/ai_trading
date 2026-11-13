"""Live market data: subscriptions, feed status, latest quotes, and the server-sent event stream the dashboard listens to."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..database.models import User
from ..execution.runtime import RuntimeError_, TradingRuntime
from ..security.roles import Role
from .deps import get_runtime, require_role

router = APIRouter(prefix="/api/live", tags=["live data"])
viewer, trader = require_role(Role.VIEWER), require_role(Role.TRADER)


@router.get("/status")
def live_status(_: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    out = rt.live_hub.status()
    out["subscriptions"] = [{"instrument_id": i, "source": s, "quote": rt.live_hub.quote(i)} for i, s in sorted(rt.live.subscribed.items())]
    out["sources"] = sorted(rt.live.sources)
    out["note"] = ("LIVE means the provider pushes or serves current prices; a quote older than "
                   f"{int(out['stale_after_seconds'])} seconds is treated as stale and blocks sizing and approvals. "
                   "US stock prices from Alpaca's free plan come from the IEX exchange only, not the consolidated tape.")
    return out


class SubscribeIn(BaseModel):
    instrument_ids: list[str] = Field(min_length=1, max_length=200)


@router.post("/subscribe")
def subscribe(body: SubscribeIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    try:
        return rt.live_subscribe(body.instrument_ids, user.email)
    except RuntimeError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(body: SubscribeIn, user: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> None:
    rt.live_unsubscribe(body.instrument_ids, user.email)


@router.post("/reconnect")
def reconnect(_: User = Depends(trader), rt: TradingRuntime = Depends(get_runtime)) -> dict:
    """Rebuild the sources, e.g. after an API key was added."""
    return {"sources": rt.configure_live()}


@router.get("/stream")
async def stream(request: Request, symbols: str = "", _: User = Depends(viewer), rt: TradingRuntime = Depends(get_runtime)) -> StreamingResponse:
    """Server-sent events: one `tick` event per price update of the requested instruments (all subscribed ones when empty)."""
    wanted = {s for s in symbols.split(",") if s} or None
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def push(event: dict) -> None:  # called from provider threads
        def put() -> None:
            if queue.full():  # a slow browser loses the oldest update, never blocks the feed
                queue.get_nowait()
            queue.put_nowait(event)

        loop.call_soon_threadsafe(put)

    token = rt.live_hub.subscribe(push, wanted)

    async def events():
        try:
            yield "retry: 3000\n\n"
            for iid in sorted(wanted or rt.live.subscribed):
                q = rt.live_hub.quote(iid)
                if q:
                    yield f"event: quote\ndata: {json.dumps(q)}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                    yield f"event: tick\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            rt.live_hub.unsubscribe(token)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

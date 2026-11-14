"""Application factory."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from . import __version__
import asyncio
from contextlib import asynccontextmanager

from .api import routes_auth, routes_charts, routes_live, routes_quant, routes_global, routes_research, routes_scanner, routes_settings, routes_trading
from .api.deps import Container, get_container
from .core.config import Settings, get_settings
from .core.logging import configure_logging, get_logger
from .database.base import Database
from .execution.runtime import TradingRuntime
from .notifications.service import EmailChannel, TelegramChannel
from .security import users
from .security.rate_limit import SECURITY_HEADERS, RateLimiter
from .security.tokens import TokenService
from .security.vault import CredentialVault

log = get_logger(__name__)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_startup()
    configure_logging(settings.log_level)
    db = database or Database(settings.database_url)

    channels = []
    if settings.smtp_host and settings.notify_email_to:
        channels.append(EmailChannel(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password, settings.notify_email_to))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        import httpx

        channels.append(TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id, httpx.post))
    runtime = TradingRuntime(db, settings, channels=channels)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.feed_interval_seconds > 0:
            async def loop() -> None:
                while True:
                    await asyncio.sleep(settings.feed_interval_seconds)
                    try:
                        await asyncio.to_thread(runtime.tick)
                    except Exception as exc:  # keep the loop alive; the next pass retries
                        log.error("feed_tick_failed", error=str(exc))

            task = asyncio.create_task(loop())
        async def autopilot_loop() -> None:
            while True:
                await asyncio.sleep(settings.autopilot_tick_seconds)
                try:
                    await asyncio.to_thread(runtime.autopilot.tick)
                except Exception as exc:  # the next minute tries again
                    log.error("autopilot_tick_failed", error=str(exc))

        autopilot_task = asyncio.create_task(autopilot_loop()) if settings.autopilot_tick_seconds > 0 else None
        catalog_task = None
        if settings.catalog_sync_hours > 0:
            async def catalog_loop() -> None:
                while True:
                    try:
                        await asyncio.to_thread(runtime.global_markets.run_scheduled, settings.catalog_sync_hours)
                    except Exception as exc:
                        log.error("catalog_schedule_failed", error=str(exc))
                    await asyncio.sleep(600)

            catalog_task = asyncio.create_task(catalog_loop())
        yield
        for t in (task, catalog_task, autopilot_task):
            if t:
                t.cancel()

    app = FastAPI(title="QUANT AI TRADER", version=__version__, lifespan=lifespan,
                  description="Multi-asset algorithmic trading and research platform. Simulated results are not a guarantee of future returns.")
    app.state.container = Container(
        settings=settings, db=db,
        tokens=TokenService(settings.jwt_secret, settings.jwt_ttl_minutes),
        vault=CredentialVault(settings.credential_key),
        login_limiter=RateLimiter(settings.login_rate_limit_per_minute),
        runtime=runtime,
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type", "X-Requested-With"])
    api_limiter = RateLimiter(settings.api_rate_limit_per_minute)

    @app.middleware("http")
    async def protect(request, call_next):
        """Per-client request budget for the API, plus conservative security headers on every response."""
        if request.url.path.startswith("/api") and request.method != "OPTIONS":
            client = request.client.host if request.client else "unknown"
            if not api_limiter.allow(client):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Too many requests; slow down"}, status_code=429, headers={"Retry-After": "60"})
        response = await call_next(request)
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response

    for module in (routes_auth, routes_settings, routes_trading, routes_research, routes_global, routes_scanner, routes_charts, routes_live, routes_quant):
        app.include_router(module.router)
    try:
        runtime.start()
        if settings.market_data_exchange:
            runtime.connect_data_exchange(settings.market_data_exchange)
    except Exception as exc:  # e.g. migrations not applied yet; /health reports it instead of crash-looping
        log.error("runtime_start_failed", error=str(exc))

    if settings.admin_email and settings.admin_password:
        try:
            with db.session() as session:
                if users.bootstrap_admin(session, settings.admin_email, settings.admin_password):
                    log.info("bootstrap_admin_created", email=settings.admin_email)
        except Exception as exc:  # e.g. migrations not applied yet - report, do not crash-loop silently
            log.error("bootstrap_admin_failed", error=str(exc))

    @app.get("/health", tags=["system"])
    def health(container: Container = Depends(get_container)) -> dict:
        try:
            with container.db.session() as session:
                session.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            database_ok = False
        return {
            "status": "ok" if database_ok and runtime.started else "degraded",
            "version": __version__,
            "database": database_ok,
            "environment": settings.environment,
            "live_trading_authorized": settings.live_trading_authorized,
            "runtime_started": runtime.started,
        }

    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()

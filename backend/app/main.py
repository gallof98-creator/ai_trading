"""Application factory."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from . import __version__
from .api import routes_auth, routes_settings
from .api.deps import Container, get_container
from .core.config import Settings, get_settings
from .core.logging import configure_logging, get_logger
from .database.base import Database
from .security import users
from .security.rate_limit import RateLimiter
from .security.tokens import TokenService
from .security.vault import CredentialVault

log = get_logger(__name__)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_startup()
    configure_logging(settings.log_level)
    db = database or Database(settings.database_url)

    app = FastAPI(title="QUANT AI TRADER", version=__version__,
                  description="Multi-asset algorithmic trading and research platform. Simulated results are not a guarantee of future returns.")
    app.state.container = Container(
        settings=settings, db=db,
        tokens=TokenService(settings.jwt_secret, settings.jwt_ttl_minutes),
        vault=CredentialVault(settings.credential_key),
        login_limiter=RateLimiter(settings.login_rate_limit_per_minute),
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "X-Requested-With"])
    app.include_router(routes_auth.router)
    app.include_router(routes_settings.router)

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
            "status": "ok" if database_ok else "degraded",
            "version": __version__,
            "database": database_ok,
            "environment": settings.environment,
            "live_trading_authorized": settings.live_trading_authorized,
        }

    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()

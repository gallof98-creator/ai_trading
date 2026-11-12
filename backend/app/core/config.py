"""Deployment settings, read from environment variables (prefix QAT_).

Secrets live only in the environment. User-tunable trading settings (capital, risk limits,
trading mode) are stored in the database - see app.database.settings_store.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_CONFIRMATION_PHRASE = "I-UNDERSTAND-LIVE-TRADING-RISKS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QAT_", env_file=".env", extra="ignore")

    environment: str = "development"  # development | production
    log_level: str = "INFO"
    database_url: str = "sqlite:///./qat.db"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Security. In production both secrets are mandatory (startup fails without them).
    jwt_secret: str = ""
    jwt_ttl_minutes: int = Field(60, gt=0)
    credential_key: str = ""  # Fernet key used to encrypt broker credentials at rest
    cookie_secure: bool = False  # set true behind HTTPS
    login_rate_limit_per_minute: int = Field(10, gt=0)

    # Optional first-run admin bootstrap (only used while the users table is empty).
    admin_email: str = ""
    admin_password: str = ""

    # Live trading is OFF unless both values are set explicitly in the live deployment.
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def live_trading_authorized(self) -> bool:
        return self.live_trading_enabled and self.live_trading_confirmation == LIVE_CONFIRMATION_PHRASE

    def validate_for_startup(self) -> None:
        if self.is_production:
            missing = [n for n in ("jwt_secret", "credential_key") if not getattr(self, n)]
            if missing:
                raise RuntimeError(f"Missing required production secrets: {', '.join('QAT_' + m.upper() for m in missing)}")
            if len(self.jwt_secret) < 32:
                raise RuntimeError("QAT_JWT_SECRET must be at least 32 characters")


@lru_cache
def get_settings() -> Settings:
    return Settings()

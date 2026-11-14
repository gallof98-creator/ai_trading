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
    api_rate_limit_per_minute: int = Field(600, gt=0)  # per client address, all /api routes

    # Optional first-run admin bootstrap (only used while the users table is empty).
    admin_email: str = ""
    admin_password: str = ""

    model_dir: str = "./models"  # trained model artefacts

    # Market data and FX
    market_data_exchange: str = ""  # CCXT exchange id used for public crypto data, e.g. "binance"; empty = none
    feed_interval_seconds: int = Field(0, ge=0)  # 0 disables the background market-data loop
    eodhd_api_key: str = ""  # optional: can also be stored (encrypted) from the dashboard
    eodhd_base_url: str = "https://eodhd.com/api"
    tiingo_api_key: str = ""
    tiingo_base_url: str = "https://api.tiingo.com"
    alphavantage_api_key: str = ""
    alphavantage_base_url: str = "https://www.alphavantage.co"
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://data.alpaca.markets"
    finnhub_api_key: str = ""
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    openfigi_api_key: str = ""  # optional: OpenFIGI works without one, more slowly
    catalog_sync_hours: int = Field(0, ge=0)  # re-synchronise the global catalog every N hours; 0 = only on request
    # Data sources that are switched on, comma separated. Alpaca alone by default: one key for listings, history,
    # live prices and trading. The other connectors stay in the code; "all" (or a list of names) switches them back on.
    enabled_providers: str = "alpaca"
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"  # where the asset list is read from; paper keys work here
    live_feed_autostart: bool = True  # reconnect the live subscriptions saved by the user when the API starts

    def provider_enabled(self, name: str) -> bool:
        wanted = {p.strip().lower() for p in self.enabled_providers.split(",") if p.strip()}
        return "all" in wanted or name.lower() in wanted
    # MODELLING ASSUMPTION: currencies treated as trading 1:1 with another one for valuation.
    fx_pegs: dict[str, str] = {"USDT": "USD", "USDC": "USD"}
    fx_max_age_days: int = Field(5, gt=0)

    # Optional external notification channels
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email_to: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

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

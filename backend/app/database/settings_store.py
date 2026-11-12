"""User-configurable platform settings, validated and persisted as one document."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .models import AppSetting

_KEY = "user_settings"


class RiskLimits(BaseModel):
    """All fractions are of account equity in base currency."""

    risk_per_trade: float = Field(0.01, gt=0, le=0.05)
    max_daily_loss: float = Field(0.03, gt=0, le=0.5)
    max_drawdown: float = Field(0.15, gt=0, le=0.9)
    max_exposure_per_asset: float = Field(0.25, gt=0, le=10)
    max_exposure_per_strategy: float = Field(0.5, gt=0, le=10)
    max_exposure_per_broker: float = Field(1.0, gt=0, le=10)
    max_gross_leverage: float = Field(1.0, gt=0, le=20)
    max_concurrent_positions: int = Field(10, gt=0, le=500)
    max_margin_utilization: float = Field(0.8, gt=0, le=1)
    max_correlated_exposure: float = Field(0.5, gt=0, le=10)
    correlation_threshold: float = Field(0.8, gt=0, le=1)
    # Volatility-adjusted sizing: scale positions down when an instrument's realised annual volatility
    # exceeds this target. null disables the adjustment.
    target_volatility: float | None = Field(None, gt=0, le=5)
    # block_new_risk: reject risk-increasing orders only (default, never liquidates)
    # cancel_open_orders: additionally cancel working orders
    # flatten_positions: additionally close positions - must be chosen explicitly by an admin
    emergency_policy: Literal["block_new_risk", "cancel_open_orders", "flatten_positions"] = "block_new_risk"


class NotificationSettings(BaseModel):
    # In-app notifications are always recorded. External channels (email, Telegram) receive
    # notifications at or above this severity, except for muted categories.
    external_min_severity: Literal["info", "warning", "critical"] = "warning"
    muted_categories: list[str] = Field(default_factory=list, max_length=50)


class UserSettings(BaseModel):
    base_currency: str = Field("EUR", pattern=r"^[A-Z]{3,5}$")
    # No default on purpose: paper accounts cannot start until the user sets this.
    initial_paper_capital: float | None = Field(None, gt=0)
    trading_mode: Literal["manual", "semi_auto", "auto"] = "manual"
    fx_conversion_cost_bps: float = Field(5.0, ge=0, le=500)
    risk: RiskLimits = RiskLimits()
    notifications: NotificationSettings = NotificationSettings()


def load_settings(session: Session) -> UserSettings:
    row = session.get(AppSetting, _KEY)
    return UserSettings.model_validate(row.value) if row else UserSettings()


def save_settings(session: Session, settings: UserSettings) -> UserSettings:
    row = session.get(AppSetting, _KEY)
    if row:
        row.value = settings.model_dump()
    else:
        session.add(AppSetting(key=_KEY, value=settings.model_dump()))
    return settings

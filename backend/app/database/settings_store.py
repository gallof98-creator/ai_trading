"""User-configurable platform settings, validated and persisted as one document."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
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
    # Costs assumed when sizing a position, so that the loss at the stop INCLUDING costs stays within risk_per_trade.
    # A live bid/ask spread replaces est_spread_bps when one is known.
    est_spread_bps: float = Field(5.0, ge=0, le=500)
    est_slippage_bps: float = Field(5.0, ge=0, le=500)  # per side
    est_commission_bps: float = Field(10.0, ge=0, le=500)  # per side
    max_spread_bps: float = Field(50.0, gt=0, le=2000)  # new risk is refused on a wider live spread
    max_data_age_seconds: float = Field(60.0, gt=0, le=3600)  # new risk is refused on an older live price
    # block_new_risk: reject risk-increasing orders only (default, never liquidates)
    # cancel_open_orders: additionally cancel working orders
    # flatten_positions: additionally close positions - must be chosen explicitly by an admin
    emergency_policy: Literal["block_new_risk", "cancel_open_orders", "flatten_positions"] = "block_new_risk"


class NotificationSettings(BaseModel):
    # In-app notifications are always recorded. External channels (email, Telegram) receive
    # notifications at or above this severity, except for muted categories.
    external_min_severity: Literal["info", "warning", "critical"] = "warning"
    muted_categories: list[str] = Field(default_factory=list, max_length=50)


class AutopilotSettings(BaseModel):
    """What the platform does by itself. It never sends an order on its own in semi-automatic mode: it prepares
    data, validates strategies and creates proposals; a person still approves each order."""

    enabled: bool = False
    broker_id: str = Field("", max_length=64)  # empty = the first account that can trade
    cycle_minutes: int = Field(15, ge=5, le=1440)
    max_instruments: int = Field(30, ge=1, le=200)
    strategies: list[str] = Field(default_factory=lambda: ["ema_crossover", "macd", "donchian_breakout", "momentum_breakout", "rsi_reversion",
                                                           "volatility_expansion"], max_length=20)
    timeframes: list[Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]] = Field(default_factory=lambda: ["1d"], max_length=4)
    seed_most_active: bool = True     # add the most traded US stocks to the universe when it is too small
    daily_price_update: bool = True
    live_subscribe: bool = True       # request live prices for instruments with a validated strategy
    min_price: float = Field(5.0, ge=0)
    min_traded_value: float = Field(20_000_000.0, ge=0)  # 20-day average traded value, quote currency
    validations_per_cycle: int = Field(25, ge=1, le=500)


class UserSettings(BaseModel):
    base_currency: str = Field("EUR", pattern=r"^[A-Z]{3,5}$")
    # No default on purpose: paper accounts cannot start until the user sets this.
    initial_paper_capital: float | None = Field(None, gt=0)
    trading_mode: Literal["manual", "semi_auto", "auto"] = "manual"
    # A proposed order waits this long for a decision, then lapses.
    proposal_ttl_minutes: int = Field(15, ge=1, le=1440)
    # A proposal is void if, when approved, the price has moved by more than this fraction of its stop distance.
    max_entry_drift: float = Field(0.25, gt=0, le=2)
    # Automatic mode is refused until the user has reviewed the risk limits and ticked this.
    safety_limits_confirmed: bool = False
    fx_conversion_cost_bps: float = Field(5.0, ge=0, le=500)
    # Orders for real-money accounts wait for a human approval even in automatic mode, unless this is switched off.
    live_orders_require_approval: bool = True
    risk: RiskLimits = RiskLimits()
    notifications: NotificationSettings = NotificationSettings()
    autopilot: AutopilotSettings = AutopilotSettings()

    @model_validator(mode="after")
    def _automatic_needs_confirmed_limits(self) -> "UserSettings":
        if self.trading_mode == "auto" and not self.safety_limits_confirmed:
            raise ValueError("Automatic mode needs the safety limits to be reviewed and confirmed first (safety_limits_confirmed)")
        return self


def initial_settings() -> UserSettings:
    """The configuration a new installation starts with. It is ordinary, editable settings (Settings page), not a
    constant in the trading code: 1,000 EUR of virtual capital, semi-automatic mode, 2% risk per trade."""
    return UserSettings(base_currency="EUR", initial_paper_capital=1000.0, trading_mode="semi_auto", risk=RiskLimits(risk_per_trade=0.02))


def load_settings(session: Session) -> UserSettings:
    row = session.get(AppSetting, _KEY)
    if not row:
        return initial_settings()
    value = dict(row.value)
    # Settings saved before the confirmation existed may say "auto" without it. The platform must still start:
    # it falls back to semi-automatic (the safer mode) until the limits are confirmed on the Settings page.
    if value.get("trading_mode") == "auto" and not value.get("safety_limits_confirmed"):
        value["trading_mode"] = "semi_auto"
    return UserSettings.model_validate(value)


def save_settings(session: Session, settings: UserSettings) -> UserSettings:
    row = session.get(AppSetting, _KEY)
    if row:
        row.value = settings.model_dump()
    else:
        session.add(AppSetting(key=_KEY, value=settings.model_dump()))
    return settings

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
    # The most US stocks and ETFs followed. The universe grows towards it step by step (see `universe_step`), so the
    # machine and the data provider are never asked for everything at once.
    max_instruments: int = Field(200, ge=1, le=200)
    universe_step: int = Field(40, ge=1, le=200)  # instruments added per day, and only while validation keeps up
    strategies: list[str] = Field(default_factory=lambda: ["ema_crossover", "macd", "donchian_breakout", "momentum_breakout", "rsi_reversion",
                                                           "volatility_expansion"], max_length=20)
    timeframes: list[Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]] = Field(default_factory=lambda: ["1d"], max_length=4)
    seed_most_active: bool = True     # add the most traded US stocks to the universe when it is too small
    daily_price_update: bool = True
    live_subscribe: bool = True       # request live prices for instruments with a validated strategy
    # Crypto pairs (quoted in USD, as Alpaca trades them) followed alongside the stocks. Their history and prices come
    # from Alpaca's crypto data, which needs no key. An empty list switches crypto off.
    crypto_pairs: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "LINK/USD", "AVAX/USD", "DOGE/USD",
                                                             "UNI/USD", "AAVE/USD", "BCH/USD"], max_length=50)
    # Exchange-traded funds always kept in the universe (the "most traded stocks" screen does not reliably include them).
    etfs: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM", "DIA", "VTI", "EFA", "EEM", "GLD", "TLT", "XLK", "XLF", "XLE"], max_length=100)
    # Live prices for every followed instrument, not only those with a validated strategy. Open positions and working
    # orders always come first.
    live_all_candidates: bool = True
    # How many US stock symbols Alpaca streams over its WebSocket on your plan (30 on the free plan, unlimited on a paid
    # one). The most important instruments stream; the others get their prices by REST requests every few seconds.
    stream_symbol_limit: int = Field(30, ge=1, le=10_000)
    min_price: float = Field(5.0, ge=0)
    min_traded_value: float = Field(20_000_000.0, ge=0)  # 20-day average traded value, quote currency
    validations_per_cycle: int = Field(60, ge=1, le=500)
    validation_bars: int = Field(1500, ge=450, le=10_000)  # validate on the most recent bars (about six years of daily data)
    # Intraday timeframes are validated on this many recent bars (20,000 five-minute bars are about a year of sessions).
    intraday_validation_bars: int = Field(20_000, ge=2_000, le=30_000)
    validation_workers: int = Field(0, ge=0, le=32)  # validations run side by side on this many processor cores; 0 = all but one
    # Official intraday bars from Alpaca. The minute refresh keeps every followed instrument current; the history
    # download fetches this many months for the intraday timeframes listed in `timeframes` (0 = never automatically).
    minute_update: bool = True
    intraday_history_months: int = Field(6, ge=0, le=24)
    # Which stock history to validate on: "iex" is the same single-exchange feed the live prices come from (what is
    # tested is what is traded); "sip" is the consolidated tape, 15 minutes delayed, with complete volume.
    intraday_feed: Literal["iex", "sip"] = "iex"
    allow_short: bool = False          # strategies are validated and traded long-only unless this is on
    # Also propose "candidate" setups: profitable on most unseen periods but on too few trades to be validated.
    # Paper accounts only; each proposal says which level of evidence it rests on.
    paper_candidates: bool = True
    require_regime_fit: bool = False   # only propose when the current market regime is one the strategy family is meant for
    # Exploration: also trade setups that FAILED validation but did not lose on unseen data, to measure them in real time.
    # Paper accounts only (the internal one or a broker's paper environment), never real money; off unless switched on.
    exploration: bool = False


class UserSettings(BaseModel):
    base_currency: str = Field("EUR", pattern=r"^[A-Z]{3,5}$")
    # No default on purpose: paper accounts cannot start until the user sets this.
    initial_paper_capital: float | None = Field(None, gt=0)
    trading_mode: Literal["manual", "semi_auto", "auto"] = "manual"
    # A proposed order waits this long for a decision, then lapses.
    proposal_ttl_minutes: int = Field(15, ge=1, le=1440)
    # A proposal is void if, when approved, the price has moved by more than this fraction of its stop distance.
    max_entry_drift: float = Field(0.25, gt=0, le=2)
    # When a strategy signals the exit of a position it holds, close it without asking. Closing orders only, and only on
    # paper accounts (the internal one and broker paper accounts); entries keep following the trading mode.
    auto_close_on_exit_signal: bool = False
    # Fractional share quantities for US stocks and ETFs (Alpaca supports them). Without this, a small account cannot
    # hold even one share of an expensive stock, so most instruments would never be traded.
    fractional_shares: bool = False
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
    return UserSettings(base_currency="EUR", initial_paper_capital=1000.0, trading_mode="semi_auto", risk=RiskLimits(risk_per_trade=0.02),
                        fractional_shares=True)


def load_settings(session: Session) -> UserSettings:
    row = session.get(AppSetting, _KEY)
    if not row:
        return initial_settings()
    value = dict(row.value)
    # Settings saved before the confirmation existed may say "auto" without it. The platform must still start:
    # it falls back to semi-automatic (the safer mode) until the limits are confirmed on the Settings page.
    if value.get("trading_mode") == "auto" and not value.get("safety_limits_confirmed"):
        value["trading_mode"] = "semi_auto"
    # Installations saved before fractional shares existed get them switched on: it only widens what can be traded.
    value.setdefault("fractional_shares", True)
    return UserSettings.model_validate(value)


def save_settings(session: Session, settings: UserSettings) -> UserSettings:
    row = session.get(AppSetting, _KEY)
    if row:
        row.value = settings.model_dump()
    else:
        session.add(AppSetting(key=_KEY, value=settings.model_dump()))
    return settings

"""Persistence schema. Money is NUMERIC; timestamps are timezone-aware UTC."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Boolean, Float, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


TS = DateTime(timezone=True)
MONEY = Numeric(28, 10)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # admin | trader | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)  # bump to revoke all sessions
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class AppSetting(Base):
    """User-configurable settings document (single row per key)."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. paper-main, binance-live
    adapter: Mapped[str] = mapped_column(String(32))  # paper | ccxt | ibkr
    label: Mapped[str] = mapped_column(String(128))
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(8))
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # non-secret options only
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class EncryptedCredential(Base):
    """Broker API credentials, encrypted with Fernet. Plaintext is never stored or returned."""

    __tablename__ = "encrypted_credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id", ondelete="CASCADE"), unique=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    key_hint: Mapped[str] = mapped_column(String(8))  # last 4 chars of the API key, for display
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class InstrumentRow(Base):
    __tablename__ = "instruments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), index=True)
    spec: Mapped[dict] = mapped_column(JSON)  # serialized domain Instrument
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # delisted instruments stay, inactive


class BrokerSymbol(Base):
    __tablename__ = "broker_symbols"
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id", ondelete="CASCADE"), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True)
    broker_symbol: Mapped[str] = mapped_column(String(64))


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(String(8))
    quote: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(TS)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    source: Mapped[str] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("base", "quote", "ts", "source"), Index("ix_fx_pair_ts", "base", "quote", "ts"))


class BarRow(Base):
    """OHLCV history. Timestamps are epoch milliseconds (bar open, UTC) to avoid any timezone ambiguity."""

    __tablename__ = "bars"
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(12))  # realtime | delayed | historical | simulated
    source: Mapped[str] = mapped_column(String(64))


class StrategyDeployment(Base):
    __tablename__ = "strategy_deployments"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(16))
    instrument_id: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    broker_account_id: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    instrument_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(4))
    payload: Mapped[dict] = mapped_column(JSON)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)  # duplicate-order guard
    broker_account_id: Mapped[str] = mapped_column(String(64), index=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean)  # paper vs real broker order
    instrument_id: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(20), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(TS)
    qty: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    commission: Mapped[Decimal] = mapped_column(MONEY, default=0)
    commission_currency: Mapped[str] = mapped_column(String(8))


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(12))
    detail: Mapped[dict] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    category: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(12))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    config: Mapped[dict] = mapped_column(JSON)  # full inputs incl. data fingerprint => reproducible
    metrics: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))


class AuditEntry(Base):
    """Append-only audit trail. Never updated or deleted by application code."""

    __tablename__ = "audit_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(320))  # user email, "system" or "ai"
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

"""Application runtime: wires market data, strategies, risk, portfolio and brokers into one
long-lived trading process with database persistence. Paper and live trading run through the
same TradingEngine; only the registered broker adapters differ."""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter, BrokerError, BrokerManager
from ..core.config import Settings
from ..core.logging import get_logger
from ..database.base import Database
from ..database.models import (AppSetting, BrokerAccount, FillRow, FxRate, InstrumentRow, OrderRow, RiskEvent, SignalRow,
                               StrategyDeployment)
from ..database.settings_store import UserSettings, load_settings
from ..domain.models import Fill, Order, Signal, TradingMode
from ..domain.models import AssetClass, Instrument
from ..instruments.registry import InstrumentRegistry, default_instruments, make_equity
from ..market_data.calendars import get_calendar
from ..market_data.fx import STATIC_SOURCE, FxRateProvider, FxRateUnavailable
from ..market_data.quality import DataKind, validate_bars
from ..market_data.sources import Dataset, synthetic_bars
from ..market_data.store import BarStore
from ..market_data.timeframes import TIMEFRAMES, resample, tf_delta
from ..monitoring import audit
from ..monitoring.health import BrokerMonitor, StrategyPerformanceMonitor
from ..notifications.service import Channel, Notifier
from ..paper_trading.broker import SimulatedBroker
from ..portfolio.engine import PortfolioEngine
from ..risk.engine import RiskEngine
from ..risk.models import RiskEventRecord
from ..signals.engine import SignalEngine
from ..strategies.registry import create_strategy
from .engine import Deployment, Journal, TradingEngine

log = get_logger(__name__)
CACHE_BARS = 5000
REPLAY_WARMUP = 400
REPLAY_MAX = 3000


class RuntimeError_(Exception):
    """A request the runtime refuses, with a message suitable for the user."""


class DbJournal(Journal):
    """Persists the trading record. If the record cannot be written, `failed` is set and the
    runtime halts new risk: trading without an audit trail is not acceptable."""

    def __init__(self, db: Database, notifier: Notifier) -> None:
        super().__init__()
        self.db, self.notifier = db, notifier
        self.failed: str | None = None

    def _write(self, fn: Callable[[Session], None]) -> None:
        try:
            with self.db.session() as session:
                fn(session)
        except Exception as exc:
            self.failed = str(exc)
            log.error("journal_write_failed", error=str(exc))

    def signal(self, signal: Signal) -> None:
        if signal.action.value == "HOLD":
            return
        self._write(lambda s: s.merge(SignalRow(id=signal.id, ts=signal.ts, instrument_id=signal.instrument_id, strategy=signal.strategy,
                                                action=signal.action.value, payload=signal.model_dump(mode="json"))))
        self.notifier.notify(signal.ts, "signal", "info", f"{signal.action.value} signal: {signal.instrument_id} ({signal.strategy}, {signal.timeframe})",
                             f"Price {signal.price:g}; stop distance {signal.stop_distance or 0:g}.")

    def order(self, order: Order) -> None:
        self._write(lambda s: s.merge(OrderRow(
            id=order.id, idempotency_key=order.client_order_id, broker_account_id=order.broker_id, is_simulated=order.is_simulated,
            instrument_id=order.instrument_id, side=order.side.value, qty=order.qty, status=order.status.value,
            broker_order_id=order.broker_order_id, payload=order.model_dump(mode="json"), created_at=order.created_at, updated_at=order.updated_at)))

    def fill(self, fill: Fill, reason: str | None) -> None:
        self._write(lambda s: s.add(FillRow(order_id=fill.order_id, ts=fill.ts, qty=fill.qty, price=fill.price, commission=fill.commission,
                                            commission_currency="quote")))
        self.notifier.notify(fill.ts, "execution", "info", f"Filled: {fill.side.value} {fill.qty:g} {fill.instrument_id} @ {fill.price:g}",
                             f"{reason or 'order'} ({fill.strategy or 'manual'}) at {fill.broker_id}")

    def risk_event(self, event: RiskEventRecord) -> None:
        self._write(lambda s: s.add(RiskEvent(ts=event.ts, kind=event.kind, severity=event.severity, detail=event.detail)))
        if event.kind != "order_rejected":  # rejections are announced by the engine with full order context
            self.notifier.notify(event.ts, "risk", event.severity, f"Risk event: {event.kind.replace('_', ' ')}",
                                 ", ".join(f"{k}={v}" for k, v in event.detail.items()))

    def alert(self, ts: datetime, category: str, severity: str, title: str, body: str = "") -> None:
        self.notifier.notify(ts, category, severity, title, body)


class TradingRuntime:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 channels: list[Channel] | None = None, data_adapter: BrokerAdapter | None = None) -> None:
        self.db, self.settings, self.clock = db, settings, clock
        self.lock = threading.RLock()
        self.registry = InstrumentRegistry()
        for inst in default_instruments():
            self.registry.register(inst)
        self.user_settings = UserSettings()
        self.fx = FxRateProvider(pegs=settings.fx_pegs, max_age=timedelta(days=settings.fx_max_age_days))
        self.brokers = BrokerManager()
        self.notifier = Notifier(db, lambda: self.user_settings.notifications, channels)
        self.journal = DbJournal(db, self.notifier)
        self.portfolio = PortfolioEngine(self.registry, self.brokers, self.fx, self.user_settings.base_currency)
        self.risk = RiskEngine(lambda: self.user_settings.risk, self.journal.risk_event)
        self.engine = TradingEngine(self.registry, self.brokers, self.portfolio, self.risk, self.fx, lambda: self.user_settings.risk,
                                    lambda: TradingMode(self.user_settings.trading_mode), self.journal, self.user_settings.base_currency,
                                    realized_vol=self._realized_vol, approval_required=self._needs_approval)
        self.signals = SignalEngine()
        self.store = BarStore()
        self.broker_monitor = BrokerMonitor(self.brokers, self.journal.alert)
        self.performance_monitor = StrategyPerformanceMonitor(self.journal.alert)
        self.data_adapter = data_adapter
        self.deployments: dict[int, Deployment] = {}
        self.bars: dict[tuple[str, str], pd.DataFrame] = {}
        self.kinds: dict[str, str] = {}  # instrument -> data kind currently in use
        self.feed_status: dict[str, str] = {}
        self.replays: dict[str, dict] = {}
        from ..global_markets.service import GlobalMarkets

        self.global_markets = GlobalMarkets(self)
        self.provider_factory = None  # test hook: callable(name, api_key) -> PriceProvider
        self.download = None
        self.download_options: dict = {}
        self.started = False

    def _needs_approval(self, order: Order) -> bool:
        """Real-money orders wait for a human unless the user explicitly switched that off."""
        if not self.user_settings.live_orders_require_approval:
            return False
        try:
            return not self.brokers.get(order.broker_id).capabilities.is_simulated
        except BrokerError:
            return True

    def _realized_vol(self, instrument_id: str) -> float | None:
        """Annualised volatility of the instrument's recent bars, for volatility-adjusted sizing."""
        tf = self._exec_tf(instrument_id)
        frame = self.bars.get((instrument_id, tf)) if tf else None
        if frame is None or len(frame) < 60:
            return None
        import numpy as np

        rets = np.log(frame["close"]).diff().iloc[-250:].dropna()
        return float(rets.std(ddof=1) * np.sqrt(pd.Timedelta(days=365) / tf_delta(tf)))

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self.lock, self.db.session() as session:
            self.reload_settings(session)
            for row in session.scalars(select(FxRate)):
                ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
                self.fx.add_rate(row.base, row.quote, ts, float(row.rate), row.source)
            for row in session.scalars(select(InstrumentRow).where(InstrumentRow.is_active.is_(True))):
                self.registry.register(Instrument.model_validate(row.spec))
            for account in session.scalars(select(BrokerAccount).where(BrokerAccount.adapter == "paper")):
                state = session.get(AppSetting, f"paper:{account.id}")
                if state is not None:
                    self._attach_paper(account.id, account.currency, state.value)
            for key, target in (("runtime:portfolio", self.portfolio), ("runtime:risk", self.risk), ("runtime:engine", self.engine)):
                saved = session.get(AppSetting, key)
                if saved is not None:
                    target.load_state(saved.value)
            meta = session.get(AppSetting, "runtime:meta")
            if meta is not None:
                self.kinds, self.replays = dict(meta.value.get("kinds", {})), dict(meta.value.get("replays", {}))
            for row in session.scalars(select(StrategyDeployment).where(StrategyDeployment.is_active.is_(True))):
                self._activate(row)
            for iid, tf in {(d.instrument_id, self._exec_tf(d.instrument_id)) for d in self.deployments.values()}:
                ds = self.store.load(session, iid, tf)
                if ds is not None:
                    self.bars[(iid, tf)] = ds.bars.iloc[-CACHE_BARS:]
            self.started = True

    def reload_settings(self, session: Session) -> None:
        self.user_settings = load_settings(session)
        self.fx.conversion_cost_bps = self.user_settings.fx_conversion_cost_bps
        self.portfolio.base = self.engine.base = self.user_settings.base_currency

    def persist(self) -> None:
        def write(session: Session) -> None:
            values = {"runtime:portfolio": self.portfolio.to_state(), "runtime:risk": self.risk.to_state(), "runtime:engine": self.engine.to_state(),
                      "runtime:meta": {"kinds": self.kinds, "replays": self.replays}}
            for adapter in self.brokers.all():
                if isinstance(adapter, SimulatedBroker):
                    values[f"paper:{adapter.broker_id}"] = adapter.to_state()
            for key, value in values.items():
                row = session.get(AppSetting, key)
                if row is None:
                    session.add(AppSetting(key=key, value=value))
                else:
                    row.value = value

        self.journal._write(write)
        self._check_journal()

    def _check_journal(self) -> None:
        if self.journal.failed and not self.risk.kill_switch_on:
            reason = "trading record could not be saved to the database"
            self.journal.failed = None
            self.engine.engage_kill_switch(reason, "system", self.clock())
            self.notifier.notify(self.clock(), "system", "critical", "Trading halted: database unavailable", reason)

    # ------------------------------------------------------------------ FX
    def add_fx_rate(self, base: str, quote: str, rate: float, source: str, actor: str, ts: datetime | None = None) -> None:
        ts = ts or self.clock()
        with self.lock, self.db.session() as session:
            existing = session.scalar(select(FxRate).where(FxRate.base == base, FxRate.quote == quote, FxRate.source == source, FxRate.ts == ts))
            if existing is not None:
                if abs(float(existing.rate) - rate) > 1e-12:
                    raise RuntimeError_(f"A different {base}/{quote} rate from '{source}' is already recorded for {ts.isoformat()}")
                return  # identical observation already stored
            session.add(FxRate(base=base, quote=quote, ts=ts, rate=rate, source=source))
            audit.record(session, actor, "fx.rate_added", base=base, quote=quote, rate=rate, source=source, ts=ts.isoformat())
            session.flush()  # the database accepts it before the in-memory provider learns it
            self.fx.add_rate(base, quote, ts, rate, source)

    def fx_rates(self) -> list[dict]:
        now, out = self.clock(), []
        currencies = {i.quote_currency for i in self.registry.all()} | {a.get_account_balance().currency for a in self.brokers.all()}
        for ccy in sorted(currencies - {self.portfolio.base}):
            try:
                q = self.fx.quote(ccy, self.portfolio.base, now)
                out.append({"currency": ccy, "rate": q.rate, "ts": q.ts.isoformat(), "source": q.source, "available": True})
            except FxRateUnavailable as exc:
                out.append({"currency": ccy, "available": False, "error": str(exc)})
        return out

    # ------------------------------------------------------------------ paper accounts
    def _attach_paper(self, account_id: str, currency: str, state: dict | None, capital: float | None = None) -> SimulatedBroker:
        broker = SimulatedBroker(account_id, self.registry, self.fx, capital or state["initial_cash"], currency)
        if state is not None:
            broker.load_state(state)
        broker.connect()
        broker.map_all_instruments()
        self.brokers.register(broker)
        return broker

    def create_paper_account(self, account_id: str, label: str, actor: str) -> dict:
        with self.lock, self.db.session() as session:
            capital = self.user_settings.initial_paper_capital
            if capital is None:
                raise RuntimeError_("Set the initial paper capital in Settings before creating a paper account")
            if session.get(BrokerAccount, account_id) is not None:
                raise RuntimeError_(f"Broker account {account_id} already exists")
            currency = self.user_settings.base_currency
            session.add(BrokerAccount(id=account_id, adapter="paper", label=label, is_live=False, currency=currency, config={}))
            broker = self._attach_paper(account_id, currency, None, capital)
            session.add(AppSetting(key=f"paper:{account_id}", value=broker.to_state()))
            audit.record(session, actor, "paper.account_created", account_id=account_id, capital=capital, currency=currency)
            return {"id": account_id, "currency": currency, "initial_capital": capital}

    def reset_paper_account(self, account_id: str, confirmation: str, actor: str) -> None:
        """Wipes a paper account back to the configured starting capital. Requires typing the account id."""
        with self.lock:
            broker = self.brokers.get(account_id)
            if not isinstance(broker, SimulatedBroker):
                raise RuntimeError_("Only paper accounts can be reset")
            if confirmation != account_id:
                raise RuntimeError_("Confirmation does not match the account id; nothing was reset")
            capital = self.user_settings.initial_paper_capital
            if capital is None:
                raise RuntimeError_("Set the initial paper capital in Settings first")
            now = self.clock()
            broker.reset(capital)
            for key in [k for k in self.portfolio.slices if k[0] == account_id]:
                del self.portfolio.slices[key]
                self.engine.protections.pop(key, None)
            self.portfolio.trades = [t for t in self.portfolio.trades if t.broker_id != account_id]
            self.portfolio.equity_curve, self.portfolio.exposure_curve = [], []
            self.portfolio.peak_equity = self.portfolio.day_start_equity = None
            self.portfolio._day = None
            for approval_id in [a.id for a in self.engine.approvals.values() if a.order.broker_id == account_id]:
                self.engine.resolve_approval(approval_id, False, f"{actor}:reset", now)
            for o in [o for o in self.engine.orders.values() if o.broker_id == account_id and o.is_open]:
                self.engine._record(o.model_copy(update={"status": "cancelled", "reason": f"{o.reason or ''} | account reset".strip(" |"), "updated_at": now}))
            self.portfolio.reconcile()
            with self.db.session() as session:
                audit.record(session, actor, "paper.account_reset", account_id=account_id, capital=capital)
            self.persist()

    def connect_broker(self, account_id: str, actor: str, exchange_factory: Callable | None = None, ib=None) -> dict:
        """Connect a CCXT or IBKR account. Without live authorization: testnet / paper gateway only."""
        from ..brokers.ccxt_adapter import CcxtAdapter
        from ..brokers.ibkr_adapter import IbkrAdapter
        from ..database.models import BrokerSymbol
        from ..live_trading.gate import authorize_live_trading
        from ..security.credentials import load_credentials
        from ..security.vault import CredentialVault, VaultError

        with self.lock, self.db.session() as session:
            row = session.get(BrokerAccount, account_id)
            if row is None or row.adapter == "paper":
                raise RuntimeError_("Unknown external broker account")
            if any(a.broker_id == account_id for a in self.brokers.all()):
                raise RuntimeError_(f"{account_id} is already connected")
            try:
                authorization = authorize_live_trading(self.settings) if row.is_live else None
            except PermissionError as exc:
                raise RuntimeError_(str(exc)) from None
            try:
                creds = load_credentials(session, CredentialVault(self.settings.credential_key), account_id)
            except LookupError:
                creds = {}
            except VaultError as exc:
                raise RuntimeError_(str(exc)) from None
            for sym in session.scalars(select(BrokerSymbol).where(BrokerSymbol.broker_account_id == account_id)):
                self.registry.map_symbol(account_id, sym.instrument_id, sym.broker_symbol)
            if row.adapter == "ccxt":
                import ccxt

                exchange_id = row.config.get("exchange", "")
                if exchange_factory is None and not hasattr(ccxt, exchange_id):
                    raise RuntimeError_(f"Unknown CCXT exchange {exchange_id!r}; set config.exchange (e.g. binance, bybit)")
                options = {"apiKey": creds.get("api_key"), "secret": creds.get("api_secret"), "password": creds.get("passphrase"),
                           "options": {"defaultType": row.config.get("market_type", "spot")}}
                exchange = (exchange_factory or getattr(ccxt, exchange_id))({k: v for k, v in options.items() if v})
                adapter: BrokerAdapter = CcxtAdapter(account_id, self.registry, exchange, row.currency, authorization)
            else:
                adapter = IbkrAdapter(account_id, self.registry, ib=ib, host=row.config.get("host", "127.0.0.1"), port=int(row.config.get("port", 7497)),
                                      client_id=int(row.config.get("client_id", 1)), account_currency=row.currency, authorization=authorization)
            try:
                adapter.connect()
            except Exception as exc:  # network, credentials, gateway not running
                raise RuntimeError_(f"Could not connect {account_id}: {exc}") from None
            self.brokers.register(adapter)
            audit.record(session, actor, "broker.connected", broker_account_id=account_id, is_live=row.is_live,
                         trading_enabled=adapter.capabilities.trading_enabled)
            return {"id": account_id, "connected": True, "capabilities": adapter.capabilities.to_dict()}

    def connect_data_exchange(self, exchange_id: str) -> None:
        """Public (keyless) CCXT connection used only for market data; it can never place orders."""
        import ccxt

        from ..brokers.ccxt_adapter import CcxtAdapter

        if not hasattr(ccxt, exchange_id):
            log.error("unknown_market_data_exchange", exchange=exchange_id)
            return
        adapter = CcxtAdapter(f"data-{exchange_id}", self.registry, getattr(ccxt, exchange_id)())
        try:
            adapter.exchange.enableRateLimit = True
            adapter.exchange.load_markets()
            adapter.connect_data_only()
            self.data_adapter = adapter
        except Exception as exc:
            log.error("market_data_exchange_unavailable", exchange=exchange_id, error=str(exc))

    def disconnect_broker(self, account_id: str, actor: str) -> None:
        with self.lock:
            adapter = self.brokers.get(account_id)
            if isinstance(adapter, SimulatedBroker):
                raise RuntimeError_("Paper accounts are always available")
            if any(k[0] == account_id for k in self.portfolio.slices):
                raise RuntimeError_("This account holds open positions tracked by the platform; close them before disconnecting")
            adapter.disconnect()
            self.brokers.remove(account_id)
            with self.db.session() as session:
                audit.record(session, actor, "broker.disconnected", broker_account_id=account_id)

    def broker_status(self) -> list[dict]:
        with self.lock:
            connected = self.broker_monitor.check(self.clock())
            out = []
            for adapter in self.brokers.all():
                caps = adapter.capabilities
                out.append({"id": adapter.broker_id, "connected": connected[adapter.broker_id], "is_simulated": caps.is_simulated,
                            "capabilities": caps.to_dict()})
            if self.data_adapter is not None:
                out.append({"id": self.data_adapter.broker_id, "connected": self.data_adapter.is_connected(), "is_simulated": False,
                            "capabilities": self.data_adapter.capabilities.to_dict() if self.data_adapter.is_connected() else None, "data_only": True})
            return out

    # ------------------------------------------------------------------ instruments
    def add_instruments(self, tickers: list[str], market: str, asset_class: str, actor: str) -> dict:
        """Add stocks or ETFs to the universe. Returns which ids were created, already known or invalid."""
        out: dict[str, list] = {"created": [], "existing": [], "invalid": []}
        with self.lock, self.db.session() as session:
            for ticker in dict.fromkeys(t for t in tickers if t.strip()):
                try:
                    inst = make_equity(ticker, market, AssetClass(asset_class))
                except ValueError as exc:
                    out["invalid"].append({"ticker": ticker, "reason": str(exc)})
                    continue
                try:
                    self.registry.get(inst.id)
                    out["existing"].append(inst.id)
                    continue
                except KeyError:
                    pass
                self.registry.register(inst)
                session.add(InstrumentRow(id=inst.id, asset_class=inst.asset_class.value, spec=inst.model_dump(mode="json"), is_active=True))
                out["created"].append(inst.id)
            for adapter in self.brokers.all():
                if isinstance(adapter, SimulatedBroker):
                    adapter.map_all_instruments()
            if out["created"]:
                audit.record(session, actor, "instruments.added", market=market, count=len(out["created"]), ids=out["created"][:200])
        return out

    def remove_instrument(self, instrument_id: str, actor: str) -> None:
        """Remove a user-added instrument together with its stored bars. Built-in instruments stay."""
        with self.lock, self.db.session() as session:
            row = session.get(InstrumentRow, instrument_id)
            if row is None:
                raise RuntimeError_("Only instruments you added can be removed")
            if session.scalar(select(StrategyDeployment).where(StrategyDeployment.instrument_id == instrument_id)) is not None:
                raise RuntimeError_("A strategy is deployed on this instrument; delete the deployment first")
            if any(k[1] == instrument_id for k in self.portfolio.slices):
                raise RuntimeError_("There is an open position in this instrument")
            self.store.delete_series(session, instrument_id)
            session.delete(row)
            self.registry.unregister(instrument_id)
            self.kinds.pop(instrument_id, None)
            for key in [k for k in self.bars if k[0] == instrument_id]:
                del self.bars[key]
            audit.record(session, actor, "instruments.removed", instrument_id=instrument_id)

    # ------------------------------------------------------------------ data providers
    PROVIDERS = ("tiingo", "alphavantage", "eodhd")
    EXTRA_KEYS = ("finnhub", "openfigi")  # keys used by the global catalog only

    def _provider_key(self, name: str) -> tuple[str, str] | None:
        """(key, where it comes from) for a provider, or None. A stored key takes precedence over the environment."""
        from ..security.vault import CredentialVault

        with self.db.session() as session:
            row = session.get(AppSetting, f"provider:{name}")
            if row is not None:
                return CredentialVault(self.settings.credential_key).decrypt(row.value["ciphertext"])["api_key"], "stored"
        env_key = getattr(self.settings, f"{name}_api_key", "")
        return (env_key, "environment") if env_key else None

    def provider_status(self) -> list[dict]:
        from ..market_data.providers import PROVIDER_CLASSES
        from ..security.vault import VaultError

        out = []
        for name in self.PROVIDERS:
            cls = PROVIDER_CLASSES[name]
            info = {"name": name, "label": cls.label, "markets": sorted(cls.markets), "listing_markets": sorted(cls.listing_markets),
                    "pricing": cls.pricing, "signup_url": cls.signup_url, "seconds_per_request": cls.min_interval,
                    "configured": False, "key_hint": None, "source": None}
            try:
                found = self._provider_key(name)
                if found:
                    info.update(configured=True, key_hint=f"…{found[0][-4:]}", source=found[1])
            except VaultError as exc:
                info["error"] = str(exc)
            out.append(info)
        return out

    def set_provider_key(self, name: str, api_key: str | None, actor: str) -> None:
        """Store (encrypted) or remove a data-provider API key."""
        from ..security.vault import CredentialVault, VaultError

        if name not in self.PROVIDERS + self.EXTRA_KEYS:
            raise RuntimeError_(f"Unknown data provider {name}")
        with self.lock, self.db.session() as session:
            row = session.get(AppSetting, f"provider:{name}")
            if api_key is None:
                if row is not None:
                    session.delete(row)
                audit.record(session, actor, "provider.key_removed", provider=name)
                return
            try:
                value = {"ciphertext": CredentialVault(self.settings.credential_key).encrypt({"api_key": api_key})}
            except VaultError as exc:
                raise RuntimeError_(str(exc)) from None
            if row is None:
                session.add(AppSetting(key=f"provider:{name}", value=value))
            else:
                row.value = value
            audit.record(session, actor, "provider.key_stored", provider=name, key_hint=api_key[-4:])

    def _provider(self, name: str):
        from ..market_data.providers import PROVIDER_CLASSES
        from ..security.vault import VaultError

        if name not in self.PROVIDERS:
            raise RuntimeError_(f"Unknown data provider {name}")
        try:
            found = self._provider_key(name)
        except VaultError as exc:
            raise RuntimeError_(str(exc)) from None
        if found is None:
            raise RuntimeError_(f"No API key is configured for {PROVIDER_CLASSES[name].label}. Add it under Markets → Data providers.")
        if self.provider_factory is not None:
            return self.provider_factory(name, found[0])
        return PROVIDER_CLASSES[name](found[0], base_url=getattr(self.settings, f"{name}_base_url"))

    def test_provider(self, name: str) -> dict:
        """One real request for a well-known symbol, so the user can confirm key, plan and data format."""
        from datetime import date, timedelta

        from ..market_data.providers import ProviderError

        provider = self._provider(name)
        try:
            ds = provider.daily_bars(self.registry.get("STOCK:AAPL"), date.today() - timedelta(days=30), date.today())
        except ProviderError as exc:
            return {"ok": False, "message": str(exc)}
        clean, report = validate_bars(ds.bars, "1d", get_calendar("XNYS"))
        last = clean.iloc[-1]
        return {"ok": True, "message": f"Received {len(clean)} daily bars for AAPL; latest {clean.index[-1].date()} close {last.close:.2f}.",
                "bars": len(clean), "last_date": clean.index[-1].date().isoformat(), "last_close": float(last.close)}

    def import_symbols(self, name: str, market: str, kinds: list[str], actor: str, limit: int | None = None) -> dict:
        """Add every listed stock/ETF of a market from the provider's symbol list."""
        from ..market_data.providers import ProviderError

        provider = self._provider(name)
        if market not in provider.listing_markets:
            raise RuntimeError_(f"{provider.label} cannot list the symbols of this market")
        try:
            listed = provider.list_symbols(market)
        except ProviderError as exc:
            raise RuntimeError_(str(exc)) from None
        out = {"listed": len(listed), "created": 0, "existing": 0, "invalid": 0}
        for kind in ("stock", "etf"):
            if kind not in kinds:
                continue
            tickers = [r["ticker"] for r in listed if r["type"] == kind]
            if limit:
                tickers = tickers[:limit]
            result = self.add_instruments(tickers, market, kind, actor)
            for key in ("created", "existing", "invalid"):
                out[key] += len(result[key])
        return out

    def _store_download(self, inst: Instrument, dataset: Dataset) -> int:
        clean, _ = validate_bars(dataset.bars, "1d", get_calendar(inst.calendar))
        if clean.empty:
            raise ValueError("no valid bars")
        if self.kinds.get(inst.id) == DataKind.SIMULATED.value:
            raise ValueError("a simulated replay is using this instrument")
        with self.db.session() as session:
            existing = {c["timeframe"]: c for c in self.store.coverage_for(session, inst.id)}.get("1d")
            if existing is not None and existing["kind"] == DataKind.SIMULATED.value:
                raise ValueError("holds simulated data; remove it first")
            # Adjusted histories are replaced as a whole: mixing an old and a new adjustment basis would create jumps.
            self.store.delete_series(session, inst.id, "1d")
            return self.store.save(session, inst.id, "1d", Dataset(clean, DataKind.HISTORICAL, dataset.source))

    def start_download(self, name: str, actor: str, instrument_ids: list[str] | None = None, market: str | None = None,
                       only_missing: bool = True, years: int = 10) -> dict:
        from ..instruments.registry import market_of
        from ..market_data.providers import DownloadJob

        with self.lock:
            if self.download is not None and self.download.snapshot()["status"] in ("pending", "running"):
                raise RuntimeError_("A download is already running")
            provider = self._provider(name)
            if instrument_ids:
                try:
                    selected = [self.registry.get(i) for i in instrument_ids]
                except KeyError as exc:
                    raise RuntimeError_(f"Unknown instrument {exc}") from None
            else:
                selected = [i for i in self.registry.all() if market_of(i) is not None and (market is None or market_of(i) == market)]
            selected = [i for i in selected if market_of(i) in provider.markets]
            if not selected:
                raise RuntimeError_(f"{provider.label} does not cover the selected instruments (it covers: {', '.join(sorted(provider.markets))})")
            if only_missing:
                with self.db.session() as session:
                    have = {c["instrument_id"] for c in self.store.coverage(session) if c["timeframe"] == "1d" and c["kind"] != "simulated"}
                selected = [i for i in selected if i.id not in have]
            if not selected:
                raise RuntimeError_("Nothing to download: every selected instrument already has daily data")
            self.download = DownloadJob(provider, sorted(selected, key=lambda i: i.id), self._store_download, years=years, **self.download_options)
            with self.db.session() as session:
                audit.record(session, actor, "market.download_started", provider=name, instruments=len(selected), years=years, market=market)
            threading.Thread(target=self.download.run, name="price-download", daemon=True).start()
            return self.download.snapshot()

    # ------------------------------------------------------------------ deployments
    def _exec_tf(self, instrument_id: str) -> str | None:
        tfs = [d.timeframe for d in self.deployments.values() if d.instrument_id == instrument_id]
        return min(tfs, key=tf_delta) if tfs else None

    def _activate(self, row: StrategyDeployment) -> None:
        strategy = create_strategy(row.strategy, row.params, row.strategy_version)
        self.deployments[row.id] = Deployment(f"dep{row.id}", strategy, row.instrument_id, row.timeframe, row.broker_account_id)

    def add_deployment(self, strategy: str, params: dict, instrument_id: str, timeframe: str, broker_id: str, actor: str) -> int:
        with self.lock, self.db.session() as session:
            instance = create_strategy(strategy, params)  # raises ValueError on invalid parameters
            try:
                self.registry.get(instrument_id)
            except KeyError:
                raise RuntimeError_(f"Unknown instrument {instrument_id}") from None
            if timeframe not in TIMEFRAMES:
                raise RuntimeError_(f"Unsupported timeframe {timeframe}")
            try:
                adapter = self.brokers.get(broker_id)
            except BrokerError as exc:
                raise RuntimeError_(str(exc)) from None
            if not self.registry.is_mapped(broker_id, instrument_id) or self.registry.get(instrument_id).asset_class not in adapter.capabilities.asset_classes:
                raise RuntimeError_(f"{instrument_id} cannot be traded at {broker_id}")
            clash = session.scalar(select(StrategyDeployment).where(
                StrategyDeployment.strategy == strategy, StrategyDeployment.instrument_id == instrument_id,
                StrategyDeployment.broker_account_id == broker_id))
            if clash is not None:
                raise RuntimeError_("This strategy is already deployed on this instrument and account")
            row = StrategyDeployment(strategy=strategy, strategy_version=instance.version, instrument_id=instrument_id, timeframe=timeframe,
                                     broker_account_id=broker_id, params=instance.params.model_dump(), is_active=False)
            session.add(row)
            session.flush()
            audit.record(session, actor, "deployment.created", deployment_id=row.id, strategy=strategy, version=instance.version,
                         instrument_id=instrument_id, timeframe=timeframe, broker_id=broker_id, params=row.params)
            return row.id

    def update_deployment(self, deployment_id: int, actor: str, active: bool | None = None, params: dict | None = None) -> None:
        with self.lock, self.db.session() as session:
            row = session.get(StrategyDeployment, deployment_id)
            if row is None:
                raise RuntimeError_(f"Unknown deployment {deployment_id}")
            if params is not None:
                instance = create_strategy(row.strategy, params, row.strategy_version)
                before, row.params = row.params, instance.params.model_dump()
                audit.record(session, actor, "deployment.params_changed", deployment_id=row.id, before=before, after=row.params)
            if active is not None and active != row.is_active:
                row.is_active = active
                audit.record(session, actor, "deployment.activated" if active else "deployment.deactivated", deployment_id=row.id)
            self.deployments.pop(row.id, None)
            if row.is_active:
                self._activate(row)

    def delete_deployment(self, deployment_id: int, actor: str) -> None:
        with self.lock, self.db.session() as session:
            row = session.get(StrategyDeployment, deployment_id)
            if row is None:
                raise RuntimeError_(f"Unknown deployment {deployment_id}")
            dep = self.deployments.get(deployment_id)
            key = (row.broker_account_id, row.instrument_id, row.strategy)
            if self.portfolio.qty(key) != 0:
                raise RuntimeError_("This deployment still has an open position; close it or deactivate the deployment instead")
            self.deployments.pop(deployment_id, None)
            session.execute(delete(StrategyDeployment).where(StrategyDeployment.id == deployment_id))
            audit.record(session, actor, "deployment.deleted", deployment_id=deployment_id, strategy=row.strategy, was_active=dep is not None)

    # ------------------------------------------------------------------ market data -> engine
    def _require_fx(self, instrument_id: str, ts: datetime) -> None:
        inst = self.registry.get(instrument_id)
        currencies = {a.get_account_balance().currency for a in self.brokers.all()} | {self.portfolio.base}
        for ccy in currencies:
            try:
                self.fx.quote(inst.quote_currency, ccy, ts)
            except FxRateUnavailable as exc:
                raise RuntimeError_(f"No usable FX rate to value {instrument_id}: {exc}. Add a rate under Settings.") from None

    def ingest(self, instrument_id: str, timeframe: str, dataset: Dataset, trade: bool = True) -> int:
        """Feed completed bars into the engine in time order. Returns the number of new bars processed."""
        with self.lock:
            inst = self.registry.get(instrument_id)
            clean, report = validate_bars(dataset.bars, timeframe, get_calendar(inst.calendar))
            simulated = dataset.kind is DataKind.SIMULATED
            known = self.kinds.get(instrument_id)
            if known is not None and (known == DataKind.SIMULATED.value) != simulated:
                raise RuntimeError_(f"{instrument_id} already uses {known} data; simulated and market data are never mixed")
            key = (instrument_id, timeframe)
            cache = self.bars.get(key)
            new = clean if cache is None else clean[clean.index > cache.index[-1]]
            if new.empty:
                return 0
            exec_tf = self._exec_tf(instrument_id)
            delta = tf_delta(timeframe)
            drive = trade and timeframe == exec_tf
            if drive:
                self._require_fx(instrument_id, new.index[0].to_pydatetime())
            self.kinds[instrument_id] = dataset.kind.value
            deps = [d for d in self.deployments.values() if d.instrument_id == instrument_id] if drive else []
            for i in range(len(new)):
                row = new.iloc[i]
                t_open = new.index[i].to_pydatetime()
                t_close = t_open + delta
                if drive:
                    self.engine.on_bar_open(instrument_id, t_open, float(row.open), float(row.volume), delta)
                cache = new.iloc[: i + 1] if cache is None else pd.concat([cache, new.iloc[i: i + 1]])
                if not drive:
                    self.portfolio.mark(instrument_id, float(row.close), t_close)
                    continue
                self.engine.on_bar_close(instrument_id, t_open, float(row.high), float(row.low), float(row.close), delta)
                for dep in deps:
                    frame = cache
                    if dep.timeframe != timeframe:
                        frame = resample(cache, timeframe, dep.timeframe)
                        if frame.empty or frame.index[-1] + tf_delta(dep.timeframe) != new.index[i] + delta:
                            continue  # no higher-timeframe bar completed at this moment
                    signal = self.signals.latest(frame, dep.strategy, instrument_id, dep.timeframe)
                    if signal is not None:
                        self.engine.on_signal(signal, dep)
                self.engine.end_of_timestamp(t_close, reconcile=True)
            self.bars[key] = cache.iloc[-CACHE_BARS:]
            self.journal._write(lambda session: self.store.save(session, instrument_id, timeframe, Dataset(new, dataset.kind, dataset.source)))
            if not report.ok:
                self.notifier.notify(self.clock(), "system", "warning", f"Data quality issue: {instrument_id} {timeframe}",
                                     f"{report.duplicates_removed} duplicates, {report.invalid_rows_removed} invalid, {report.missing_bars} missing bars")
            if drive:
                self.performance_monitor.check(self.portfolio.trades, self.clock())
            self.persist()
            return len(new)

    def _feed_for(self, instrument_id: str) -> BrokerAdapter | None:
        candidates = ([self.data_adapter] if self.data_adapter is not None else []) + self.brokers.all()
        for adapter in candidates:
            try:
                if adapter.is_connected() and adapter.capabilities.supports_historical_data and self.registry.is_mapped(adapter.broker_id, instrument_id):
                    return adapter
            except BrokerError:
                continue
        return None

    def tick(self) -> dict[str, str]:
        """One pass of the live loop: connectivity, broker order updates, then new completed bars."""
        with self.lock:
            now = self.clock()
            self.broker_monitor.check(now)
            self.engine.poll_orders(now)
            for iid in sorted({d.instrument_id for d in self.deployments.values()}):
                if self.kinds.get(iid) == DataKind.SIMULATED.value:
                    self.feed_status[iid] = "simulated replay (no live feed)"
                    continue
                feed = self._feed_for(iid)
                if feed is None:
                    self.feed_status[iid] = "no market data feed connected"
                    continue
                tf = self._exec_tf(iid)
                try:
                    ds = feed.get_historical_data(iid, tf, limit=500)
                    first = (iid, tf) not in self.bars
                    n = self.ingest(iid, tf, ds, trade=not first)  # the first batch only warms up indicators
                    self.feed_status[iid] = f"ok ({feed.broker_id}, {n} new bars)"
                except (BrokerError, RuntimeError_, ValueError) as exc:
                    if not self.feed_status.get(iid, "").startswith("error"):
                        self.notifier.notify(now, "system", "warning", f"Market data problem: {iid}", str(exc))
                    self.feed_status[iid] = f"error: {exc}"
            self.persist()
            return dict(self.feed_status)

    def replay_synthetic(self, instrument_id: str, timeframe: str, bars: int, seed: int, actor: str,
                         fx_assumptions: dict[str, float] | None = None) -> dict:
        """Drive the paper engine with SIMULATED bars (seeded random walk) so the whole pipeline can be
        exercised without a market-data connection. Never available for instruments holding market data."""
        with self.lock:
            if self._exec_tf(instrument_id) != timeframe:
                raise RuntimeError_("Activate a strategy on this instrument and timeframe first")
            if any(not a.capabilities.is_simulated for a in self.brokers.all()
                   if any(d.broker_id == a.broker_id and d.instrument_id == instrument_id for d in self.deployments.values())):
                raise RuntimeError_("Simulated data can only drive paper accounts")
            state = self.replays.get(instrument_id)
            if state is None or state["timeframe"] != timeframe or state["seed"] != seed:
                if self.kinds.get(instrument_id) not in (None, DataKind.SIMULATED.value):
                    raise RuntimeError_(f"{instrument_id} holds market data; simulated and market data are never mixed")
                # One series per instrument and timeframe: a replay never blends into previously stored bars.
                with self.db.session() as session:
                    stored = self.store.load(session, instrument_id, timeframe)
                    if stored is not None and stored.kind is not DataKind.SIMULATED:
                        raise RuntimeError_(f"{instrument_id} {timeframe} holds stored market data; simulated and market data are never mixed")
                    if stored is not None:
                        self.store.delete_series(session, instrument_id, timeframe)
                        audit.record(session, actor, "market.simulated_replaced", instrument_id=instrument_id, timeframe=timeframe,
                                     removed_bars=len(stored.bars), reason="new simulated replay")
                delta = tf_delta(timeframe)
                end = pd.Timestamp(self.clock()).floor(f"{int(delta.total_seconds())}s")
                start = end - (REPLAY_WARMUP + REPLAY_MAX) * delta
                state = {"timeframe": timeframe, "seed": seed, "start": start.isoformat(), "position": 0}
                self.bars.pop((instrument_id, timeframe), None)
            start = pd.Timestamp(state["start"])
            for ccy, rate in (fx_assumptions or {}).items():
                self.add_fx_rate(ccy, self.portfolio.base, rate, STATIC_SOURCE, actor, ts=(start - pd.Timedelta(days=1)).to_pydatetime())
            series = synthetic_bars(timeframe, REPLAY_WARMUP + REPLAY_MAX, seed, start=state["start"],
                                    start_price=self.portfolio.prices.get(instrument_id, 100.0) if state["position"] == 0 else state["start_price"])
            if state["position"] == 0:
                state["start_price"] = float(series.bars["open"].iloc[0])
                self.replays[instrument_id] = state
                self.ingest(instrument_id, timeframe, Dataset(series.bars.iloc[:REPLAY_WARMUP], DataKind.SIMULATED, series.source), trade=False)
            lo = REPLAY_WARMUP + state["position"]
            hi = min(lo + bars, REPLAY_WARMUP + REPLAY_MAX)
            if hi <= lo:
                raise RuntimeError_("The simulated replay has reached the present; reset the paper account to start again")
            n = self.ingest(instrument_id, timeframe, Dataset(series.bars.iloc[lo:hi], DataKind.SIMULATED, series.source), trade=True)
            state["position"] += n
            self.replays[instrument_id] = state
            with self.db.session() as session:
                audit.record(session, actor, "paper.simulated_replay", instrument_id=instrument_id, timeframe=timeframe, bars=n, seed=seed)
            self.persist()
            return {"bars_processed": n, "data_kind": "simulated", "remaining": REPLAY_MAX - state["position"]}

    # ------------------------------------------------------------------ operator actions
    def kill_switch(self, engage: bool, reason: str, actor: str) -> None:
        with self.lock:
            now = self.clock()
            if engage:
                self.engine.engage_kill_switch(reason, actor, now)
            else:
                self.risk.release_kill_switch(now, actor)
            with self.db.session() as session:
                audit.record(session, actor, "risk.kill_switch_on" if engage else "risk.kill_switch_off", reason=reason)
            self.persist()

    def reset_breaker(self, name: str, actor: str) -> None:
        with self.lock:
            if name not in self.risk.breakers:
                raise RuntimeError_(f"Circuit breaker {name} is not tripped")
            if name == "reconciliation" and self.portfolio.reconcile():
                raise RuntimeError_("Positions still do not reconcile: " + "; ".join(self.portfolio.discrepancies))
            self.risk.reset_breaker(name, self.clock(), actor)
            with self.db.session() as session:
                audit.record(session, actor, "risk.breaker_reset", breaker=name)
            self.persist()

    def resolve_approval(self, approval_id: str, approve: bool, actor: str) -> Order:
        with self.lock:
            try:
                order = self.engine.resolve_approval(approval_id, approve, actor, self.clock())
            except KeyError:
                raise RuntimeError_(f"No pending approval {approval_id}") from None
            with self.db.session() as session:
                audit.record(session, actor, "order.approved" if approve else "order.declined", approval_id=approval_id, order_id=order.id,
                             result=order.status.value)
            self.persist()
            return order

    # ------------------------------------------------------------------ views
    def overview(self) -> dict:
        with self.lock:
            now = self.clock()
            if not self.brokers.all():
                return {"configured": False, "base_currency": self.portfolio.base, "trading_mode": self.user_settings.trading_mode,
                        "risk": self.risk.status(), "active_strategies": len(self.deployments), "brokers": []}
            # A purely simulated replay lives on its own (historical) clock: value it at its latest bar.
            simulated_only = bool(self.kinds) and all(k == DataKind.SIMULATED.value for k in self.kinds.values())
            ts = self.portfolio.equity_curve[-1][0] if simulated_only and self.portfolio.equity_curve else now
            try:
                snap = self.portfolio.snapshot(ts)
            except FxRateUnavailable as exc:
                return {"configured": True, "error": f"Portfolio cannot be valued: {exc}", "base_currency": self.portfolio.base,
                        "trading_mode": self.user_settings.trading_mode, "risk": self.risk.status(), "active_strategies": len(self.deployments),
                        "brokers": []}
            trades = self.portfolio.trades
            wins = sum(1 for t in trades if t.pnl > 0)
            curve = pd.Series([e for _, e in self.portfolio.equity_curve], dtype=float)
            initial = sum(a.initial_cash * self.fx.quote(a.currency, self.portfolio.base, ts).rate for a in self.brokers.all() if isinstance(a, SimulatedBroker))
            snap.update({
                "configured": True, "trading_mode": self.user_settings.trading_mode, "risk": self.risk.status(),
                "active_strategies": len(self.deployments), "active_brokers": sum(1 for a in self.brokers.all() if a.is_connected()),
                "number_of_trades": len(trades), "win_rate": wins / len(trades) if trades else None,
                "total_pnl": snap["equity"] - initial if initial and all(b["is_simulated"] for b in snap["brokers"]) else snap["realized_pnl"] + snap["unrealized_pnl"],
                "max_drawdown": float((1 - curve / curve.cummax()).max()) if len(curve) else 0.0,
                "data_kinds": dict(self.kinds), "feed_status": dict(self.feed_status),
                "all_simulated": all(b["is_simulated"] for b in snap["brokers"]),
            })
            return snap

    def equity_history(self, limit: int = 1500) -> list[dict]:
        with self.lock:
            curve = self.portfolio.equity_curve
            step = max(1, len(curve) // limit)
            return [{"ts": t.isoformat(), "equity": e} for t, e in curve[::step]]

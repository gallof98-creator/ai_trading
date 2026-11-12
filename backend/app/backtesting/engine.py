"""Event-driven backtester.

It drives the SAME TradingEngine, RiskEngine, PortfolioEngine and SimulatedBroker used for paper
trading through a timeline built from historical bars:

    at time T:  1. bars closing at T   -> intrabar stop/limit fills, mark to market
                2. revalue             -> circuit breakers
                3. signals known at T  -> sizing, risk check, orders queued
                4. bars opening at T   -> queued orders fill at the open

A signal computed from the bar that closes at T can therefore never trade before T.
The run uses no wall-clock time and no unseeded randomness: equal inputs give equal outputs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from ..brokers.base import BrokerManager
from ..database.settings_store import RiskLimits
from ..domain.models import Instrument, TradingMode
from ..execution.engine import Deployment, Journal, TradingEngine
from ..instruments.registry import InstrumentRegistry, default_instruments
from ..market_data.fx import FxRateProvider
from ..market_data.quality import DataKind, validate_bars
from ..market_data.calendars import get_calendar
from ..market_data.sources import Dataset
from ..market_data.timeframes import resample, tf_delta
from ..paper_trading.broker import ExecutionModel, SimulatedBroker
from ..portfolio.engine import ClosedTrade, PortfolioEngine
from ..risk.engine import RiskEngine
from ..signals.engine import signal_from_row
from ..strategies.registry import create_strategy
from .metrics import DISCLAIMER, compute_metrics, drawdown_curve, monthly_returns, trade_distribution

CLOSE, REVALUE, ROLL, SIGNAL, OPEN = range(5)


@dataclass
class DeploymentSpec:
    strategy: str
    instrument_id: str
    timeframe: str
    params: dict = field(default_factory=dict)
    version: str | None = None


@dataclass
class BacktestSpec:
    initial_capital: float
    deployments: list[DeploymentSpec]
    data: dict[tuple[str, str], Dataset]  # (instrument_id, timeframe) -> bars
    base_currency: str = "EUR"
    execution: ExecutionModel = field(default_factory=ExecutionModel)
    limits: RiskLimits = field(default_factory=RiskLimits)
    fx: FxRateProvider | None = None
    # Used only when `fx` is None: {"USD": 0.92} means 1 USD = 0.92 base for the WHOLE test (flagged in the result).
    static_fx: dict[str, float] = field(default_factory=dict)
    instruments: list[Instrument] | None = None
    trade_start: pd.Timestamp | None = None  # earlier bars only warm up indicators
    rolls: dict[str, list[pd.Timestamp]] = field(default_factory=dict)  # futures roll timestamps per instrument

    def config_hash(self) -> str:
        payload = {
            "capital": self.initial_capital, "base": self.base_currency, "execution": self.execution.to_dict(),
            "limits": self.limits.model_dump(), "static_fx": self.static_fx,
            "trade_start": self.trade_start.isoformat() if self.trade_start is not None else None,
            "deployments": [[d.strategy, d.version, d.instrument_id, d.timeframe, d.params] for d in self.deployments],
            "data": {f"{k[0]}|{k[1]}": v.fingerprint() for k, v in sorted(self.data.items())},
            "rolls": {k: [t.isoformat() for t in v] for k, v in sorted(self.rolls.items())},
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class BacktestResult:
    config_hash: str
    metrics: dict
    equity: pd.Series
    trades: list[ClosedTrade]
    per_strategy: dict[str, dict]
    orders_total: int
    orders_rejected: int
    risk_events: list[dict]
    warnings: list[str]
    costs: dict
    open_positions: int
    data_kinds: dict[str, str]
    disclaimer: str = DISCLAIMER

    def to_dict(self, max_points: int = 1500) -> dict:
        eq = self.equity
        step = max(1, len(eq) // max_points)
        sampled = eq.iloc[::step] if len(eq) else eq
        dd = drawdown_curve(eq).iloc[::step] if len(eq) else eq
        initial = self.metrics["initial_capital"]
        return {
            "config_hash": self.config_hash, "metrics": self.metrics, "per_strategy": self.per_strategy,
            "equity_curve": [{"ts": t.isoformat(), "equity": float(v)} for t, v in sampled.items()],
            "drawdown_curve": [{"ts": t.isoformat(), "drawdown": float(v)} for t, v in dd.items()],
            "monthly_returns": monthly_returns(eq, initial), "trade_distribution": trade_distribution(self.trades),
            "trades": [t.to_dict() for t in self.trades[-500:]], "orders_total": self.orders_total, "orders_rejected": self.orders_rejected,
            "risk_events": self.risk_events[-200:], "warnings": self.warnings, "costs": self.costs, "open_positions": self.open_positions,
            "data_kinds": self.data_kinds, "disclaimer": self.disclaimer,
        }


def _static_fx_provider(spec: BacktestSpec, start: pd.Timestamp) -> FxRateProvider:
    fx = FxRateProvider(conversion_cost_bps=0.0, max_age=timedelta(days=365 * 200))
    for ccy, rate in spec.static_fx.items():
        fx.add_rate(ccy, spec.base_currency, (start - pd.Timedelta(days=1)).to_pydatetime(), rate, "static-assumption")
    return fx


def run_backtest(spec: BacktestSpec) -> BacktestResult:
    if not spec.initial_capital > 0:
        raise ValueError("Initial capital must be configured and positive")
    if not spec.deployments:
        raise ValueError("At least one strategy deployment is required")
    names = [(d.strategy, d.instrument_id) for d in spec.deployments]
    if len(set(names)) != len(names):
        raise ValueError("A strategy can be deployed only once per instrument")

    warnings: list[str] = []
    registry = InstrumentRegistry()
    for inst in spec.instruments or default_instruments():
        registry.register(inst)

    # ---- data: validate, pick each instrument's execution timeframe, derive higher timeframes causally
    by_instrument: dict[str, list[DeploymentSpec]] = {}
    for d in spec.deployments:
        registry.get(d.instrument_id)
        by_instrument.setdefault(d.instrument_id, []).append(d)
    bars: dict[tuple[str, str], pd.DataFrame] = {}
    data_kinds: dict[str, str] = {}
    exec_tf: dict[str, str] = {}
    for iid, deps in by_instrument.items():
        tfs = sorted({d.timeframe for d in deps}, key=tf_delta)
        exec_tf[iid] = tfs[0]
        for tf in tfs:
            ds = spec.data.get((iid, tf))
            if ds is None:
                if (iid, tfs[0]) not in bars:
                    raise ValueError(f"No data supplied for {iid} {tf}")
                bars[(iid, tf)] = resample(bars[(iid, tfs[0])], tfs[0], tf)
                continue
            clean, report = validate_bars(ds.bars, tf, get_calendar(registry.get(iid).calendar))
            if not report.ok:
                warnings.append(f"{iid} {tf}: {report.duplicates_removed} duplicate, {report.invalid_rows_removed} invalid and "
                                f"{report.missing_bars} missing bars in the input data")
            if clean.empty:
                raise ValueError(f"No valid bars for {iid} {tf}")
            bars[(iid, tf)] = clean
            data_kinds[f"{iid}|{tf}"] = ds.kind.value
            if ds.kind is DataKind.SIMULATED:
                warnings.append(f"{iid} {tf}: SIMULATED price data - results describe a random walk, not a market")
    start = min(b.index[0] for b in bars.values())

    fx = spec.fx
    if fx is None:
        fx = _static_fx_provider(spec, start)
        if spec.static_fx:
            warnings.append("Static FX rates assumed for the whole period; currency moves and conversion costs are not modelled")

    # ---- wire the same components paper trading uses
    brokers = BrokerManager()
    broker = SimulatedBroker("backtest", registry, fx, spec.initial_capital, spec.base_currency, spec.execution)
    broker.connect()
    broker.map_all_instruments()
    brokers.register(broker)
    journal = Journal()
    portfolio = PortfolioEngine(registry, brokers, fx, spec.base_currency)
    risk = RiskEngine(lambda: spec.limits, journal.risk_event)
    engine = TradingEngine(registry, brokers, portfolio, risk, fx, lambda: spec.limits, lambda: TradingMode.AUTO, journal,
                           spec.base_currency, sleep=lambda s: None)

    # ---- timeline
    events: list[tuple] = []
    n = 0
    close_times: set = set()
    for iid, tf in exec_tf.items():
        frame, delta = bars[(iid, tf)], tf_delta(tf)
        opens = frame.index.to_pydatetime()
        for i, (o, h, l, c, v) in enumerate(frame[["open", "high", "low", "close", "volume"]].itertuples(index=False, name=None)):
            n += 1
            events.append((opens[i], OPEN, n, (iid, o, v, delta)))
            close_t = opens[i] + delta
            events.append((close_t, CLOSE, n, (iid, opens[i], h, l, c, delta)))
            close_times.add(close_t)
        for roll_ts in spec.rolls.get(iid, []):
            n += 1
            events.append((roll_ts.to_pydatetime(), ROLL, n, (iid,)))
    for t in close_times:
        n += 1
        events.append((t, REVALUE, n, ()))
    for i, d in enumerate(spec.deployments):
        strategy = create_strategy(d.strategy, d.params, d.version)
        dep = Deployment(f"bt{i}", strategy, d.instrument_id, d.timeframe, "backtest")
        frame, delta = bars[(d.instrument_id, d.timeframe)], tf_delta(d.timeframe)
        signals = strategy.generate(frame)
        for ts, row in signals[signals["action"] != "HOLD"].iterrows():
            close_t = ts + delta
            if spec.trade_start is not None and close_t < spec.trade_start:
                continue
            n += 1
            events.append((close_t.to_pydatetime(), SIGNAL, n, (dep, signal_from_row(ts, row, strategy, d.instrument_id, d.timeframe, close_t))))
    events.sort(key=lambda e: e[:3])

    for ts, kind, _, payload in events:
        if kind == OPEN:
            engine.on_bar_open(payload[0], ts, payload[1], payload[2], payload[3])
        elif kind == CLOSE:
            engine.on_bar_close(*payload)
        elif kind == REVALUE:
            engine.end_of_timestamp(ts)
        elif kind == ROLL:
            broker.charge_roll(payload[0], ts)
        else:
            engine.on_signal(payload[1], payload[0])

    problems = portfolio.reconcile()
    if problems:  # internal books and the simulated venue must agree exactly; anything else is an engine bug
        raise RuntimeError("Backtest books do not reconcile: " + "; ".join(problems))

    equity = pd.Series({pd.Timestamp(t): e for t, e in portfolio.equity_curve}, dtype=float).sort_index()
    metrics = compute_metrics(equity, portfolio.trades, spec.initial_capital, portfolio.exposure_curve)
    per_strategy = {}
    for name in sorted({t.strategy for t in portfolio.trades if t.strategy}):
        ts_ = [t for t in portfolio.trades if t.strategy == name]
        pnl = [t.pnl for t in ts_]
        wins = [p for p in pnl if p > 0]
        loss = -sum(p for p in pnl if p < 0)
        per_strategy[name] = {"number_of_trades": len(pnl), "net_profit": sum(pnl), "win_rate": len(wins) / len(pnl),
                              "profit_factor": sum(wins) / loss if loss > 0 else None, "expectancy": sum(pnl) / len(pnl)}
    if metrics["number_of_trades"] < 30:
        warnings.append(f"Only {metrics['number_of_trades']} closed trades: statistics are not reliable")
    rejected = sum(1 for o in journal.orders.values() if o.status.value == "rejected")
    return BacktestResult(
        config_hash=spec.config_hash(), metrics=metrics, equity=equity, trades=list(portfolio.trades), per_strategy=per_strategy,
        orders_total=len(journal.orders), orders_rejected=rejected,
        risk_events=[{"ts": e.ts.isoformat(), "kind": e.kind, "severity": e.severity, "detail": e.detail} for e in journal.risk_events],
        warnings=warnings, costs=dict(broker.costs), open_positions=len(portfolio.slices), data_kinds=data_kinds)

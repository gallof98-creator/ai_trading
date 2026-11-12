"""Multi-asset, multi-broker portfolio engine.

Positions are tracked per (broker, instrument, strategy) slice from fills. Account equity always
comes from each broker's own balance - balances of different brokers are converted to the base
currency with timestamped FX rates and only summed alongside a reconciliation status.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from ..brokers.base import BrokerError, BrokerManager
from ..domain.models import Fill
from ..instruments.registry import InstrumentRegistry
from ..market_data.fx import FxRateProvider
from ..risk.models import Exposure, PortfolioState

Key = tuple[str, str, str | None]  # broker, instrument, strategy


@dataclass
class Slice:
    qty: float  # signed
    avg_price: float
    entry_ts: datetime
    entry_commission: float  # quote currency, not yet attributed to a closed trade


@dataclass
class ClosedTrade:
    broker_id: str
    instrument_id: str
    strategy: str | None
    direction: int
    qty: float
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    commission: float  # quote currency, entry + exit share
    pnl_quote: float  # net of commission, quote currency
    pnl: float  # net, base currency (converted at exit, incl. conversion cost)
    exit_reason: str | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_ts"], d["exit_ts"] = self.entry_ts.isoformat(), self.exit_ts.isoformat()
        return d


class PortfolioEngine:
    def __init__(self, registry: InstrumentRegistry, brokers: BrokerManager, fx: FxRateProvider, base_currency: str = "EUR",
                 correlation_window: int = 250, min_correlation_points: int = 30) -> None:
        self.registry, self.brokers, self.fx, self.base = registry, brokers, fx, base_currency
        self.slices: dict[Key, Slice] = {}
        self.trades: list[ClosedTrade] = []
        self.prices: dict[str, float] = {}
        self.equity_curve: list[tuple[datetime, float]] = []
        self.exposure_curve: list[bool] = []
        self.day_start_equity: float | None = None
        self.peak_equity: float | None = None
        self._day = None
        self._history: dict[str, deque] = {}
        self._window, self._min_points = correlation_window, min_correlation_points
        self._corr_cache: tuple[int, dict] = (-1, {})
        self._marks = 0
        self.reconciled = True
        self.discrepancies: list[str] = []

    # ------------------------------------------------------------------ fills and marks
    def qty(self, key: Key) -> float:
        s = self.slices.get(key)
        return s.qty if s else 0.0

    def on_fill(self, fill: Fill, reason: str | None = None) -> ClosedTrade | None:
        inst = self.registry.get(fill.instrument_id)
        key: Key = (fill.broker_id, fill.instrument_id, fill.strategy)
        signed = fill.side.sign * fill.qty
        s = self.slices.get(key)
        self.prices.setdefault(fill.instrument_id, fill.price)
        if s is None or s.qty * signed > 0:  # open or add
            if s is None:
                self.slices[key] = Slice(signed, fill.price, fill.ts, fill.commission)
            else:
                total = s.qty + signed
                s.avg_price = (abs(s.qty) * s.avg_price + fill.qty * fill.price) / abs(total)
                s.qty, s.entry_commission = total, s.entry_commission + fill.commission
            return None
        closing = min(fill.qty, abs(s.qty))
        direction = 1 if s.qty > 0 else -1
        entry_share = s.entry_commission * closing / abs(s.qty)
        exit_share = fill.commission * closing / fill.qty
        gross = closing * (fill.price - s.avg_price) * direction * inst.contract_multiplier
        net_quote = gross - entry_share - exit_share
        trade = ClosedTrade(fill.broker_id, fill.instrument_id, fill.strategy, direction, closing, s.entry_ts, fill.ts, s.avg_price,
                            fill.price, entry_share + exit_share, net_quote,
                            self.fx.convert(net_quote, inst.quote_currency, self.base, fill.ts, with_cost=True), reason)
        self.trades.append(trade)
        s.entry_commission -= entry_share
        s.qty += direction * -closing
        rest = fill.qty - closing
        if abs(s.qty) < 1e-12:
            del self.slices[key]
            if rest > 1e-12:  # flipped through zero
                self.slices[key] = Slice(fill.side.sign * rest, fill.price, fill.ts, fill.commission - exit_share)
        return trade

    def mark(self, instrument_id: str, price: float, ts: datetime) -> None:
        self.prices[instrument_id] = price
        self._history.setdefault(instrument_id, deque(maxlen=self._window)).append((ts, price))
        self._marks += 1

    # ------------------------------------------------------------------ valuation
    def _rate(self, currency: str, ts: datetime) -> float:
        return self.fx.quote(currency, self.base, ts).rate

    def broker_balances(self, ts: datetime) -> list[dict]:
        out = []
        for adapter in self.brokers.all():
            bal = adapter.get_account_balance()
            q = self.fx.quote(bal.currency, self.base, ts)
            out.append({"broker_id": adapter.broker_id, "currency": bal.currency, "cash": bal.cash, "equity": bal.equity,
                        "margin_used": bal.margin_used, "fx_rate": q.rate, "fx_ts": q.ts.isoformat(), "fx_source": q.source,
                        "equity_base": bal.equity * q.rate, "margin_base": bal.margin_used * q.rate,
                        "is_simulated": adapter.capabilities.is_simulated})
        return out

    def equity(self, ts: datetime) -> float:
        return sum(b["equity_base"] for b in self.broker_balances(ts))

    def exposures(self, ts: datetime) -> list[Exposure]:
        out = []
        for (broker_id, iid, strategy), s in self.slices.items():
            inst = self.registry.get(iid)
            notional = s.qty * self.prices[iid] * inst.contract_multiplier * self._rate(inst.quote_currency, ts)
            out.append(Exposure(broker_id, iid, strategy, inst.asset_class.value, inst.quote_currency, notional, abs(notional) * inst.margin_rate))
        return out

    def correlations(self) -> dict[tuple[str, str], float]:
        """Pairwise correlation of bar-to-bar returns over the recent window, on common timestamps."""
        if self._corr_cache[0] == self._marks:
            return self._corr_cache[1]
        series = {iid: pd.Series({t: p for t, p in h}) for iid, h in self._history.items() if len(h) >= self._min_points}
        out: dict[tuple[str, str], float] = {}
        ids = sorted(series)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                joined = pd.concat([series[a], series[b]], axis=1, join="inner").pct_change().dropna()
                if len(joined) >= self._min_points and joined.std().min() > 0:
                    out[(a, b)] = float(joined.corr().iloc[0, 1])
        self._corr_cache = (self._marks, out)
        return out

    def state(self, ts: datetime) -> PortfolioState:
        equity = self.equity(ts)
        return PortfolioState(ts=ts, equity=equity, day_start_equity=self.day_start_equity or equity, peak_equity=self.peak_equity or equity,
                              exposures=self.exposures(ts), correlations=self.correlations(), reconciled=self.reconciled)

    def revalue(self, ts: datetime) -> PortfolioState:
        """Record an equity observation and roll the daily / peak reference levels."""
        equity = self.equity(ts)
        if self._day != ts.date():
            self._day = ts.date()
            self.day_start_equity = self.equity_curve[-1][1] if self.equity_curve else equity
        self.peak_equity = max(self.peak_equity or equity, equity)
        self.equity_curve.append((ts, equity))
        self.exposure_curve.append(bool(self.slices))
        return self.state(ts)

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self) -> list[str]:
        """Compare internal books with each broker's reported positions."""
        problems: list[str] = []
        for adapter in self.brokers.all():
            try:
                reported = {p.instrument_id: p.qty for p in adapter.get_positions()}
            except BrokerError as exc:
                problems.append(f"{adapter.broker_id}: positions unavailable ({exc})")
                continue
            internal: dict[str, float] = {}
            for (broker_id, iid, _), s in self.slices.items():
                if broker_id == adapter.broker_id:
                    internal[iid] = internal.get(iid, 0.0) + s.qty
            for iid in sorted(set(reported) | set(internal)):
                a, b = internal.get(iid, 0.0), reported.get(iid, 0.0)
                lot = self.registry.get(iid).lot_size if not iid.startswith("UNMAPPED:") else 0.0
                if abs(a - b) > max(lot / 2, 1e-9):
                    problems.append(f"{adapter.broker_id} {iid}: internal {a:g} vs broker {b:g}")
        self.discrepancies, self.reconciled = problems, not problems
        return problems

    # ------------------------------------------------------------------ persistence
    def to_state(self, curve_tail: int = 5000) -> dict:
        return {
            "slices": [[*k, s.qty, s.avg_price, s.entry_ts.isoformat(), s.entry_commission] for k, s in self.slices.items()],
            "trades": [t.to_dict() for t in self.trades[-5000:]], "prices": self.prices,
            "equity_curve": [[t.isoformat(), e] for t, e in self.equity_curve[-curve_tail:]],
            "exposure_curve": self.exposure_curve[-curve_tail:], "day_start_equity": self.day_start_equity, "peak_equity": self.peak_equity,
            "day": self._day.isoformat() if self._day else None, "reconciled": self.reconciled, "discrepancies": self.discrepancies,
        }

    def load_state(self, state: dict) -> None:
        from datetime import date

        self.slices = {(b, i, st): Slice(q, a, datetime.fromisoformat(ts), c) for b, i, st, q, a, ts, c in state["slices"]}
        self.trades = [ClosedTrade(**{**t, "entry_ts": datetime.fromisoformat(t["entry_ts"]), "exit_ts": datetime.fromisoformat(t["exit_ts"])})
                       for t in state["trades"]]
        self.prices = dict(state["prices"])
        self.equity_curve = [(datetime.fromisoformat(t), e) for t, e in state["equity_curve"]]
        self.exposure_curve = list(state["exposure_curve"])
        self.day_start_equity, self.peak_equity = state["day_start_equity"], state["peak_equity"]
        self._day = date.fromisoformat(state["day"]) if state["day"] else None
        self.reconciled, self.discrepancies = state["reconciled"], list(state["discrepancies"])

    # ------------------------------------------------------------------ analytics
    @staticmethod
    def _group(exposures: list[Exposure], attr: str, equity: float) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in exposures:
            g = out.setdefault(str(getattr(e, attr)), {"net": 0.0, "gross": 0.0})
            g["net"] += e.signed_notional
            g["gross"] += abs(e.signed_notional)
        for g in out.values():
            g["pct_of_equity"] = g["gross"] / equity if equity > 0 else None
        return out

    def drawdown(self) -> float:
        if not self.equity_curve or not self.peak_equity:
            return 0.0
        return 1 - self.equity_curve[-1][1] / self.peak_equity

    def volatility(self, periods_per_year: float = 365.0) -> float | None:
        """Annualised volatility of daily portfolio returns (needs >= 20 daily observations)."""
        if len(self.equity_curve) < 2:
            return None
        daily = pd.Series({t: e for t, e in self.equity_curve}).resample("1D").last().dropna().pct_change().dropna()
        return float(daily.std(ddof=1) * np.sqrt(periods_per_year)) if len(daily) >= 20 else None

    def snapshot(self, ts: datetime) -> dict:
        balances = self.broker_balances(ts)
        equity = sum(b["equity_base"] for b in balances)
        margin = sum(b["margin_base"] for b in balances)
        exposures = self.exposures(ts)
        positions = []
        unrealized = 0.0
        for (broker_id, iid, strategy), s in self.slices.items():
            inst = self.registry.get(iid)
            pnl_quote = s.qty * (self.prices[iid] - s.avg_price) * inst.contract_multiplier
            pnl = pnl_quote * self._rate(inst.quote_currency, ts)
            unrealized += pnl
            positions.append({"broker_id": broker_id, "instrument_id": iid, "strategy": strategy, "qty": s.qty, "avg_price": s.avg_price,
                              "last_price": self.prices[iid], "currency": inst.quote_currency, "unrealized_pnl": pnl,
                              "entry_ts": s.entry_ts.isoformat()})
        corr = self.correlations()
        return {
            "ts": ts.isoformat(), "base_currency": self.base, "equity": equity, "available_capital": equity - margin, "margin_used": margin,
            "unrealized_pnl": unrealized, "realized_pnl": sum(t.pnl for t in self.trades),
            "daily_pnl": equity - self.day_start_equity if self.day_start_equity else 0.0,
            "drawdown": self.drawdown(), "peak_equity": self.peak_equity, "volatility": self.volatility(),
            "gross_exposure": sum(abs(e.signed_notional) for e in exposures), "net_exposure": sum(e.signed_notional for e in exposures),
            "brokers": balances, "positions": positions, "reconciled": self.reconciled, "discrepancies": self.discrepancies,
            "exposure_by_market": self._group(exposures, "asset_class", equity), "exposure_by_broker": self._group(exposures, "broker_id", equity),
            "exposure_by_currency": self._group(exposures, "currency", equity), "exposure_by_strategy": self._group(exposures, "strategy", equity),
            "exposure_by_instrument": self._group(exposures, "instrument_id", equity),
            "correlations": [{"a": a, "b": b, "value": v} for (a, b), v in sorted(corr.items())],
        }

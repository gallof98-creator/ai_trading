"""Multi-asset, multi-broker portfolio engine.

Positions are tracked per (broker, instrument, strategy) slice from fills. Account equity always
comes from each broker's own balance - balances of different brokers are converted to the base
currency with timestamped FX rates and only summed alongside a reconciliation status.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from ..brokers.base import BrokerError, BrokerManager
from ..core.logging import get_logger
from ..domain.models import AssetClass, Fill, Instrument
from ..instruments.registry import InstrumentRegistry
from ..market_data.fx import FxRateProvider, FxRateUnavailable
from ..risk.models import Exposure, PortfolioState

Key = tuple[str, str, str | None]  # broker, instrument, strategy


log = get_logger(__name__)


@dataclass
class Slice:
    qty: float  # signed
    avg_price: float
    entry_ts: datetime
    entry_commission: float  # quote currency, not yet attributed to a closed trade
    initial_stop_distance: float | None = None  # price distance of the stop when the position was opened (for R)
    funding: float = 0.0  # quote currency: financing / perpetual funding paid (+) or received (-), not yet attributed


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
    pnl: float  # net, base currency (converted at exit, incl. conversion cost); 0 when no rate was available (see fx_source)
    exit_reason: str | None
    # --- measurement (all optional: trades recorded before they existed load unchanged) ---
    asset_class: str | None = None
    timeframe: str | None = None
    fx_rate: float | None = None      # quote -> base at the close
    fx_source: str | None = None      # where the rate came from; "unavailable" when there was none (then pnl_base is None)
    fx_ts: str | None = None
    funding_quote: float = 0.0        # financing / perpetual funding over the trade's life, quote currency (paid > 0)
    est_cost_quote: float = 0.0       # spread + slippage estimated from the cost model; ALREADY inside the fill prices
    pnl_net_quote: float | None = None  # pnl_quote - funding: the trade's whole result, quote currency
    pnl_base: float | None = None     # pnl_net_quote in the base currency at the close (conversion cost included)
    initial_stop_distance: float | None = None
    initial_risk_quote: float | None = None  # stop distance x quantity x multiplier at entry
    initial_risk_base: float | None = None
    r_multiple: float | None = None   # pnl_net_quote / initial_risk_quote
    shadow: dict | None = None        # for manual closes: the exit the system would have made (see analytics.shadow)
    risk_source: str | None = None    # how the initial risk was known: None = recorded at entry; else how it was reconstructed

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
        self.valuation_problems: dict[str, str] = {}  # broker id -> why its figures could not be read just now
        self._last_balance: dict = {}
        # broker id -> [equity in base currency, capital basis] at the last recorded observation. An account that joins,
        # leaves or changes its capital basis (virtual capital set, deposit, withdrawal) is a flow of capital, not a gain
        # or a loss: the daily and peak reference levels move with it. None until the first observation.
        self._contrib: dict[str, list] | None = None
        # One row per UTC day, kept for good: equity at the start and end of the day and the capital that came in (+) or
        # went out (-) that day (accounts linked or removed, virtual capital changed). Returns are computed from it,
        # so that a deposit is never mistaken for a gain.
        self.daily: dict[str, dict] = {}
        # Set by the owner (runtime or backtest): estimated spread/2 + slippage per side in bps, the timeframe of the strategy
        # behind a position, and currencies to price through another one (assumptions, from the analytics settings).
        self.cost_bps: Callable[[Instrument], float] = lambda inst: 0.0
        self.timeframe_of: Callable[[Key], str | None] = lambda key: None
        self.fx_aliases: dict[str, str] = {}
        self.discrepancies: list[str] = []

    # ------------------------------------------------------------------ measurement helpers
    def _close_rate(self, quote_ccy: str, ts: datetime) -> tuple[float | None, str, str | None, float | None]:
        """(rate, source, rate time, factor) converting quote -> base at a close. `factor` includes the conversion cost
        (what converting the amount really yields). Currencies the FX source does not cover: a configured alias
        (e.g. USDT -> USD at par, an assumption), or a crypto quote priced by the platform itself (e.g. BTC via BTC/USD).
        None when nothing is available: the trade is then flagged, never summed in the wrong unit."""
        def through(ccy: str, label: str, mult: float = 1.0):
            q = self.fx.quote(ccy, self.base, ts)
            factor = self.fx.convert(1.0, ccy, self.base, ts, with_cost=True) * mult
            return q.rate * mult, f"{label}{q.source}", q.ts.isoformat(), factor

        try:
            return through(quote_ccy, "")
        except FxRateUnavailable:
            pass
        alias = self.fx_aliases.get(quote_ccy)
        if alias:
            try:
                return through(alias, f"{quote_ccy}={alias} at par (assumption) + ")
            except FxRateUnavailable:
                pass
        for usd in ("USD", "USDT", "USDC"):
            px = self.prices.get(f"CRYPTO:{quote_ccy}/{usd}")
            if px:
                try:
                    rate, src, at, factor = through(self.fx_aliases.get(usd, usd), f"{quote_ccy}/{usd} platform price + ", px)
                    return rate, src, at, factor
                except FxRateUnavailable:
                    continue
        return None, "unavailable", None, None

    def add_funding(self, key: Key, amount_quote: float) -> None:
        """Financing or perpetual funding charged (+) or credited (-) to a position, quote currency."""
        s = self.slices.get(key)
        if s is not None:
            s.funding += amount_quote

    def set_initial_stop(self, key: Key, stop_distance: float | None) -> None:
        s = self.slices.get(key)
        if s is not None and s.initial_stop_distance is None and stop_distance:
            s.initial_stop_distance = float(stop_distance)

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
        funding = s.funding * closing / abs(s.qty)
        net = net_quote - funding
        rate, source, rate_ts, factor = self._close_rate(inst.quote_currency, fill.ts)
        risk = s.initial_stop_distance * closing * inst.contract_multiplier if s.initial_stop_distance else None
        per_side = self.cost_bps(inst) / 10_000
        trade = ClosedTrade(fill.broker_id, fill.instrument_id, fill.strategy, direction, closing, s.entry_ts, fill.ts, s.avg_price,
                            fill.price, entry_share + exit_share, net_quote,
                            net_quote * factor if factor is not None else 0.0, reason,
                            asset_class=inst.asset_class.value, timeframe=self.timeframe_of((fill.broker_id, fill.instrument_id, fill.strategy)),
                            fx_rate=rate, fx_source=source, fx_ts=rate_ts, funding_quote=funding,
                            est_cost_quote=closing * (s.avg_price + fill.price) * inst.contract_multiplier * per_side,
                            pnl_net_quote=net, pnl_base=net * factor if factor is not None else None,
                            initial_stop_distance=s.initial_stop_distance, initial_risk_quote=risk,
                            initial_risk_base=risk * factor if risk is not None and factor is not None else None,
                            r_multiple=net / risk if risk else None)
        self.trades.append(trade)
        s.funding -= funding
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
        """Every account's figures in base currency. An account that cannot be read right now is reported with its last
        known figures and the problem (`valuation_problems`), instead of failing every page and every decision."""
        out = []
        current = {a.broker_id for a in self.brokers.all()}
        self.valuation_problems = {k: v for k, v in self.valuation_problems.items() if k in current}
        for adapter in self.brokers.all():
            try:
                bal = adapter.get_account_balance()
                stale = bool(getattr(adapter, "stale", False))
                problem = getattr(adapter, "last_error", None) if stale else None
            except Exception as exc:  # unreachable broker, rejected key, rate limit
                bal, stale, problem = self._last_balance.get(adapter.broker_id), True, f"{type(exc).__name__}: {exc}"
                if bal is None:
                    self.valuation_problems[adapter.broker_id] = problem
                    continue
            if problem:
                self.valuation_problems[adapter.broker_id] = problem
            else:
                self.valuation_problems.pop(adapter.broker_id, None)
                self._last_balance[adapter.broker_id] = bal
            q = self.fx.quote(bal.currency, self.base, ts)
            out.append({"broker_id": adapter.broker_id, "basis": getattr(adapter, "capital_basis", "account"), "currency": bal.currency, "cash": bal.cash, "equity": bal.equity,
                        "margin_used": bal.margin_used, "fx_rate": q.rate, "fx_ts": q.ts.isoformat(), "fx_source": q.source,
                        "equity_base": bal.equity * q.rate, "margin_base": bal.margin_used * q.rate,
                        "is_simulated": adapter.capabilities.is_simulated if adapter.is_connected() else None,
                        "stale": stale, "problem": problem})
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
        out: dict[tuple[str, str], float] = {}
        if sum(len(h) >= self._min_points for h in self._history.values()) < 2:
            # Fewer than two instruments with enough history: there is no pair to correlate. Same result as below,
            # without building a series on every bar (this was most of the time of a single-instrument backtest).
            self._corr_cache = (self._marks, out)
            return out
        series = {iid: pd.Series({t: p for t, p in h}) for iid, h in self._history.items() if len(h) >= self._min_points}
        ids = sorted(series)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                joined = pd.concat([series[a], series[b]], axis=1, join="inner").pct_change().dropna()
                if len(joined) >= self._min_points and joined.std().min() > 0:
                    out[(a, b)] = float(joined.corr().iloc[0, 1])
        self._corr_cache = (self._marks, out)
        return out

    def state(self, ts: datetime, balances: list[dict] | None = None) -> PortfolioState:
        balances = self.broker_balances(ts) if balances is None else balances
        equity = sum(b["equity_base"] for b in balances)
        return PortfolioState(ts=ts, equity=equity, day_start_equity=self.day_start_equity or equity, peak_equity=self.peak_equity or equity,
                              exposures=self.exposures(ts), correlations=self.correlations(), reconciled=self.reconciled,
                              valuation_problems=dict(self.valuation_problems),
                              account_equity={b["broker_id"]: b["equity_base"] for b in balances})

    def revalue(self, ts: datetime) -> PortfolioState:
        """Record an equity observation and roll the daily / peak reference levels."""
        balances = self.broker_balances(ts)
        equity = sum(b["equity_base"] for b in balances)
        if self.valuation_problems:
            # An account could not be read: its figures are old or missing, so this is not a true observation. It is not
            # recorded, and it does not move the daily or peak reference levels.
            return self.state(ts, balances)
        current = {b["broker_id"]: [b["equity_base"], b["basis"]] for b in balances}
        if self._day != ts.date():
            self._day = ts.date()
            self.day_start_equity = self.equity_curve[-1][1] if self.equity_curve else equity
        flow = 0.0
        if self._contrib is None:
            # First observation with this bookkeeping (a new installation, or one updated from a version without it):
            # earlier reference levels may include accounts valued on another basis, so they start again from here.
            self.day_start_equity = self.peak_equity = equity
            self._daily_restart(ts, equity)
        else:
            flow = 0.0
            for broker_id, (value, basis) in current.items():
                before = self._contrib.get(broker_id)
                if before is None or before[1] != basis:
                    flow += value - (before[0] if before else 0.0)
            flow -= sum(v for broker_id, (v, _) in self._contrib.items() if broker_id not in current)
            if flow:
                self.day_start_equity = (self.day_start_equity or 0.0) + flow
                self.peak_equity = (self.peak_equity or 0.0) + flow
                if self.day_start_equity <= 0 or self.peak_equity <= 0:
                    self.day_start_equity = self.peak_equity = equity
        self._contrib = current
        self.peak_equity = max(self.peak_equity or equity, equity)
        self._daily_record(ts, equity, flow)
        self.equity_curve.append((ts, equity))
        self.exposure_curve.append(bool(self.slices))
        return self.state(ts, balances)  # the figures just read, not a second reading of every account

    # ------------------------------------------------------------------ returns
    def _daily_record(self, ts: datetime, equity: float, flow: float) -> None:
        day = ts.date().isoformat()
        row = self.daily.get(day)
        if row is None:
            last = self.daily[max(self.daily)] if self.daily else None
            row = self.daily[day] = {"start": last["end"] if last else equity, "end": equity, "flows": 0.0}
        row["end"] = equity
        row["flows"] += flow

    def _daily_restart(self, ts: datetime, equity: float) -> None:
        """The day's measurement starts again at `equity` (the reference basis of the accounts changed)."""
        self.daily[ts.date().isoformat()] = {"start": equity, "end": equity, "flows": 0.0}

    @staticmethod
    def _day_return(row: dict) -> float | None:
        base = row["start"] + row["flows"]  # capital moved during the day counts as present from its start
        return (row["end"] - row["start"] - row["flows"]) / base if base > 0 else None

    def returns(self, days: int = 31, weeks: int = 13, months: int = 12) -> dict:
        """Time-weighted portfolio returns in the base currency, net of costs: per day, compounded per ISO week and per
        calendar month (UTC), with capital moved in or out excluded. Only days the platform observed are counted."""
        import math

        rows = sorted((d, r, self._day_return(r)) for d, r in self.daily.items())
        rows = [(d, r, x) for d, r, x in rows if x is not None]

        def compound(values: list[float]) -> float | None:
            return math.prod(1 + v for v in values) - 1 if values else None

        def grouped(key) -> list[dict]:
            out: dict[str, list] = {}
            for d, _, x in rows:
                out.setdefault(key(d), []).append(x)
            return [{"period": k, "return": compound(v), "days": len(v)} for k, v in sorted(out.items())]

        from datetime import date as _date

        def iso_week(d: str) -> str:
            y, w, _ = _date.fromisoformat(d).isocalendar()
            return f"{y}-W{w:02d}"

        weekly, monthly = grouped(iso_week), grouped(lambda d: d[:7])
        today = rows[-1] if rows else None
        return {
            "base_currency": self.base, "first_day": rows[0][0] if rows else None, "last_day": today[0] if today else None,
            "today": today[2] if today else None,
            "week_to_date": weekly[-1]["return"] if weekly else None, "month_to_date": monthly[-1]["return"] if monthly else None,
            "since_start": compound([x for _, _, x in rows]),
            "daily": [{"period": d, "return": x, "start": r["start"], "end": r["end"], "flows": r["flows"]} for d, r, x in rows[-days:]],
            "weekly": weekly[-weeks:], "monthly": monthly[-months:],
            "meaning": "Time-weighted returns of all accounts together in the base currency, after costs. Capital added or removed "
                       "(an account linked or removed, virtual capital changed) is not counted as a gain or a loss. Days are UTC.",
        }

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, pending: set[tuple[str, str]] | frozenset = frozenset()) -> list[str]:
        """Compare internal books with each broker's reported positions. `pending`: (broker, instrument) differences already
        recognised as the user's own action at the broker and about to be taken into the books (see sync_with_broker)."""
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
                if (adapter.broker_id, iid) in pending:
                    continue
                a, b = internal.get(iid, 0.0), reported.get(iid, 0.0)
                lot = self.registry.get(iid).lot_size if not iid.startswith("UNMAPPED:") else 0.0
                if abs(a - b) > max(lot / 2, 1e-9) and self._fee_in_kind(adapter, iid, a, b):
                    continue
                if abs(a - b) > max(lot / 2, 1e-9):
                    problems.append(f"{adapter.broker_id} {iid}: internal {a:g} vs broker {b:g}")
        self.discrepancies, self.reconciled = problems, not problems
        return problems

    FEE_IN_KIND_MAX = 0.01  # crypto venues charge at most a fraction of a percent; anything larger is a real discrepancy

    def _fee_in_kind(self, adapter, instrument_id: str, internal: float, reported: float) -> bool:
        """Crypto brokers (Alpaca among them) take their trading fee out of the coins bought: buying 0.01 BTC delivers
        about 0.00997. A long crypto holding slightly SMALLER than the books, by at most FEE_IN_KIND_MAX, is that fee: the
        books take the broker's quantity and the missing coins are booked as commission at the average price. Anything
        else (more coins than the books, a short, a larger gap) stays a discrepancy and stops new risk."""
        if adapter.capabilities.is_simulated or instrument_id.startswith("UNMAPPED:"):
            return False
        inst = self.registry.get(instrument_id)
        if inst.asset_class is not AssetClass.CRYPTO or internal <= 0 or not (0 < internal - reported <= self.FEE_IN_KIND_MAX * internal):
            return False
        keys = [k for k, s in self.slices.items() if k[0] == adapter.broker_id and k[1] == instrument_id and s.qty > 0]
        if not keys or any(self.slices[k].qty <= 0 for k in keys):
            return False
        scale = reported / internal
        for k in keys:
            s = self.slices[k]
            fee = s.qty * (1 - scale) * s.avg_price * inst.contract_multiplier
            s.qty *= scale
            s.entry_commission += fee
        log.info("fee_in_kind_booked", broker_id=adapter.broker_id, instrument_id=instrument_id, books=internal, broker=reported)
        return True

    # ------------------------------------------------------------------ persistence
    def to_state(self, curve_tail: int = 5000) -> dict:
        return {
            "slices": [[*k, s.qty, s.avg_price, s.entry_ts.isoformat(), s.entry_commission, s.initial_stop_distance, s.funding]
                       for k, s in self.slices.items()],
            "trades": [t.to_dict() for t in self.trades[-5000:]], "prices": self.prices,
            "equity_curve": [[t.isoformat(), e] for t, e in self.equity_curve[-curve_tail:]],
            "exposure_curve": self.exposure_curve[-curve_tail:], "day_start_equity": self.day_start_equity, "peak_equity": self.peak_equity,
            "day": self._day.isoformat() if self._day else None, "reconciled": self.reconciled, "discrepancies": self.discrepancies,
            "contributions": self._contrib, "daily": self.daily,
        }

    def load_state(self, state: dict) -> None:
        from datetime import date

        self.slices = {(b, i, st): Slice(q, a, datetime.fromisoformat(ts), c, *rest) for b, i, st, q, a, ts, c, *rest in state["slices"]}
        self.trades = [ClosedTrade(**{**t, "entry_ts": datetime.fromisoformat(t["entry_ts"]), "exit_ts": datetime.fromisoformat(t["exit_ts"])})
                       for t in state["trades"]]
        self.prices = dict(state["prices"])
        self.equity_curve = [(datetime.fromisoformat(t), e) for t, e in state["equity_curve"]]
        self.exposure_curve = list(state["exposure_curve"])
        self.day_start_equity, self.peak_equity = state["day_start_equity"], state["peak_equity"]
        self._day = date.fromisoformat(state["day"]) if state["day"] else None
        self.reconciled, self.discrepancies = state["reconciled"], list(state["discrepancies"])
        self._contrib = state.get("contributions")
        self.daily = dict(state.get("daily") or {})

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

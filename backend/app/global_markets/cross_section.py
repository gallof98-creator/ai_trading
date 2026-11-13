"""Cross-sectional (whole-universe) strategies: selection, portfolio weights and a rebalancing backtest.

The same `Selector` picks the portfolio in the backtest and for a paper rebalance, so what is tested
is what is proposed. Decisions at the close of day D use data up to D only; orders fill at the next open.

What the backtest does NOT know, stated in every result:
  * instruments that were delisted before the catalog was built are missing (survivorship bias);
  * prices are in each instrument's own currency and are not converted (as if currency were hedged at no cost);
  * costs are one flat rate per traded value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtesting.metrics import DISCLAIMER, compute_metrics
from ..database.models import BarRow, BarSeries, Security
from ..instruments.registry import market_of
from ..strategies import indicators as ind
from .exchanges import EXCHANGES
from .rankings import EQUITY_LIKE, MODELS, zscores
from .scanner import _MARKET_EXCHANGE

MAX_INSTRUMENTS = 2000
WARMUP = 260  # bars needed before the first rebalance (12-month factors)
STRATEGY_PREFIX = "global:"

WEIGHTINGS = {
    "equal": "Every selected instrument gets the same weight.",
    "inverse_volatility": "Weights proportional to 1 / 60-day volatility: calmer instruments get more.",
    "min_variance": "Long-only minimum-variance weights from the last 126 daily returns (covariance shrunk halfway to its diagonal).",
}


class StrategySpec(BaseModel):
    model: str = Field("momentum", max_length=32)
    top_n: int = Field(10, ge=1, le=200)
    rebalance: Literal["weekly", "monthly"] = "monthly"
    weighting: Literal["equal", "inverse_volatility", "min_variance"] = "equal"
    max_weight: float = Field(0.2, gt=0, le=1)
    invest: float = Field(1.0, gt=0, le=1)  # share of equity the strategy may deploy
    max_country_weight: float | None = Field(None, gt=0, le=1)
    max_sector_weight: float | None = Field(None, gt=0, le=1)
    max_currency_weight: float | None = Field(None, gt=0, le=1)
    target_volatility: float | None = Field(None, gt=0, le=2)
    rotation: Literal["country", "sector"] | None = None
    top_groups: int = Field(3, ge=1, le=50)
    markets: list[str] = Field(default_factory=list, max_length=60)
    min_price: float = Field(1.0, ge=0)
    min_dollar_volume: float = Field(1_000_000.0, ge=0)
    stop_pct: float = Field(0.2, ge=0.02, le=0.9)  # protective stop below the entry price
    cost_bps: float = Field(10.0, ge=0, le=500)
    max_participation: float = Field(0.05, gt=0, le=1)  # position size limit as a share of 20-day average traded value
    start: str | None = None
    initial_capital: float = Field(100_000.0, gt=0)

    @property
    def strategy_name(self) -> str:
        return STRATEGY_PREFIX + self.model


def describe_options() -> dict:
    return {"models": [{"name": k, "label": v["label"], "description": v["description"]} for k, v in MODELS.items()],
            "weightings": WEIGHTINGS, "defaults": StrategySpec().model_dump(),
            "rules": ["Decisions use data up to the close of the rebalance day; orders fill at the next open.",
                      "Positions are entered at their target weight and held until they leave the selection or hit their stop; they are not resized in between.",
                      "Weight removed by a cap stays in cash; it is not pushed into other instruments.",
                      "Rebalance days are the first trading day of each week or month."]}


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    ids: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    meta: dict[str, dict]


def instrument_meta(session: Session, registry) -> dict[str, dict]:
    linked = {s.instrument_id: s for s in session.scalars(select(Security).where(Security.instrument_id.is_not(None)))}
    out = {}
    for inst in registry.all():
        market = market_of(inst) or inst.asset_class.value
        exchange = EXCHANGES.get(_MARKET_EXCHANGE.get(market, market))
        sec = linked.get(inst.id)
        out[inst.id] = {"symbol": inst.symbol, "name": sec.name if sec else "", "market": market,
                        "country": (sec.country if sec else (exchange.country if exchange else "")) or None, "currency": inst.quote_currency,
                        "asset_type": sec.security_type if sec else inst.asset_class.value, "sector": sec.sector if sec else None}
    return out


def load_panel(session: Session, registry, markets: list[str] | None = None, start: str | None = None,
               max_instruments: int = MAX_INSTRUMENTS) -> Panel:
    """Daily bars of every equity-like instrument with stored market (never simulated) data, aligned on one calendar."""
    meta = instrument_meta(session, registry)
    series = {r.instrument_id for r in session.scalars(select(BarSeries).where(BarSeries.timeframe == "1d", BarSeries.kind != "simulated"))}
    ids = sorted(i for i in series if i in meta and meta[i]["asset_type"] in EQUITY_LIKE and (not markets or meta[i]["market"] in markets))
    if len(ids) > max_instruments:
        raise ValueError(f"{len(ids)} instruments match; at most {max_instruments} can be tested at once. Choose fewer markets.")
    if len(ids) < 2:
        raise ValueError("Fewer than two instruments with stored daily prices match. Download prices first.")
    start_ms = int(pd.Timestamp(start, tz="UTC").value // 1_000_000) if start else None
    frames = []
    for k in range(0, len(ids), 200):
        q = select(BarRow.instrument_id, BarRow.ts_ms, BarRow.open, BarRow.high, BarRow.low, BarRow.close, BarRow.volume).where(
            BarRow.timeframe == "1d", BarRow.kind != "simulated", BarRow.instrument_id.in_(ids[k:k + 200]))
        if start_ms is not None:
            q = q.where(BarRow.ts_ms >= start_ms)
        frames.append(pd.DataFrame(session.execute(q).all(), columns=["id", "ts", "open", "high", "low", "close", "volume"]))
    raw = pd.concat(frames, ignore_index=True)
    if raw.empty:
        raise ValueError("No stored daily prices in the requested period.")
    raw["ts"] = pd.to_datetime(raw["ts"], unit="ms", utc=True).dt.normalize()
    raw = raw.drop_duplicates(["id", "ts"], keep="last")
    dates = pd.DatetimeIndex(sorted(raw["ts"].unique()))
    cols = sorted(raw["id"].unique())
    fields = {f: raw.pivot(index="ts", columns="id", values=f).reindex(index=dates, columns=cols).to_numpy(dtype=float)
              for f in ("open", "high", "low", "close", "volume")}
    return Panel(dates, cols, meta={i: meta[i] for i in cols}, **fields)


def cap_weights(weights: np.ndarray, max_weight: float, groups: list[tuple[np.ndarray, float]]) -> np.ndarray:
    """Scale weights down until no instrument and no group exceeds its cap. Never scales anything up: the remainder is cash."""
    w = np.minimum(weights, max_weight)
    for labels, cap in groups:
        for g in pd.unique(labels):
            members = labels == g
            total = w[members].sum()
            if total > cap > 0:
                w[members] *= cap / total
    return w


def min_variance(cov: np.ndarray) -> np.ndarray:
    """Long-only minimum variance by active set: solve, drop negative weights, repeat."""
    n = len(cov)
    active = np.ones(n, dtype=bool)
    w = np.full(n, 1.0 / n)
    for _ in range(n):
        sub = cov[np.ix_(active, active)]
        try:
            raw = np.linalg.solve(sub + 1e-10 * np.eye(len(sub)), np.ones(len(sub)))
        except np.linalg.LinAlgError:
            break
        if raw.sum() <= 0:
            break
        sol = raw / raw.sum()
        if (sol >= -1e-12).all():
            w = np.zeros(n)
            w[active] = np.clip(sol, 0, None)
            return w / w.sum()
        idx = np.flatnonzero(active)
        active[idx[sol < 0]] = False
        if not active.any():
            break
    return np.full(n, 1.0 / n)


class Selector:
    """Point-in-time factor values and the portfolio a specification selects on a given day."""

    def __init__(self, panel: Panel, spec: StrategySpec) -> None:
        if spec.model not in MODELS:
            raise ValueError(f"Unknown model {spec.model!r}; available: {', '.join(MODELS)}")
        self.panel, self.spec = panel, spec
        self.factors = MODELS[spec.model]["factors"]
        close = pd.DataFrame(panel.close).ffill(limit=5)
        high = pd.DataFrame(panel.high).ffill(limit=5)
        self.price = close.to_numpy()
        log_ret = np.log(close).diff()
        self.returns = close.pct_change(fill_method=None).to_numpy()
        self.adv = (close * pd.DataFrame(panel.volume).fillna(0.0)).rolling(20).mean().to_numpy()
        self.vol60 = (log_ret.rolling(60).std(ddof=1) * math.sqrt(252)).to_numpy()
        self.ret_3m = (close / close.shift(63) - 1).to_numpy()
        builders = {
            "mom_12_1": lambda: close.shift(21) / close.shift(252) - 1, "ret_5d": lambda: close / close.shift(5) - 1,
            "ret_1m": lambda: close / close.shift(21) - 1, "ret_3m": lambda: close / close.shift(63) - 1, "ret_6m": lambda: close / close.shift(126) - 1,
            "volatility_20": lambda: log_ret.rolling(20).std(ddof=1) * math.sqrt(252),
            "dist_sma_200": lambda: close / close.rolling(200).mean() - 1, "dist_high_52w": lambda: close / high.rolling(252).max() - 1,
            "adx_14": self._adx,
        }
        missing = [f for f, _, _ in self.factors if f not in builders]
        if missing:
            raise ValueError(f"Model {spec.model} uses {', '.join(missing)}, which cannot be rebuilt historically")
        self.values = {f: np.asarray(builders[f]()) for f, _, _ in self.factors}
        frame = pd.DataFrame([panel.meta[i] for i in panel.ids])
        self.labels = {k: frame[k].where(frame[k].notna(), "(unknown)").to_numpy(dtype=object) for k in ("country", "sector", "currency")}

    def _adx(self) -> np.ndarray:
        p = self.panel
        out = np.full(p.close.shape, np.nan)
        for j in range(len(p.ids)):
            ok = np.isfinite(p.close[:, j])
            if ok.sum() > 30:
                bars = pd.DataFrame({"high": p.high[ok, j], "low": p.low[ok, j], "close": p.close[ok, j]})
                out[ok, j] = ind.adx(bars, 14)[0].to_numpy()
        return pd.DataFrame(out).ffill(limit=5).to_numpy()

    def eligible(self, i: int) -> np.ndarray:
        s = self.spec
        mask = np.isfinite(self.panel.close[i]) & (self.price[i] >= s.min_price) & (self.adv[i] >= s.min_dollar_volume)
        for f, _, _ in self.factors:
            mask &= np.isfinite(self.values[f][i])
        return mask

    def select(self, i: int) -> dict:
        """Portfolio chosen at the close of day `i`: {"weights": {column: weight}, "scores": {...}, "eligible": n, "note": str}."""
        s = self.spec
        mask = self.eligible(i)
        note = ""
        if s.rotation:
            labels = self.labels[s.rotation]
            known = mask & (labels != "(unknown)") & np.isfinite(self.ret_3m[i])
            groups = pd.Series(self.ret_3m[i][known]).groupby(labels[known]).mean().sort_values(ascending=False)
            if groups.empty:
                return {"weights": {}, "scores": {}, "eligible": int(mask.sum()), "note": f"No {s.rotation} information is available for the eligible instruments."}
            chosen = list(groups.index[: s.top_groups])
            mask = known & np.isin(labels, chosen)
            note = f"Leading {s.rotation} groups by 3-month return: {', '.join(map(str, chosen))}."
        cols = np.flatnonzero(mask)
        if len(cols) < 2:
            return {"weights": {}, "scores": {}, "eligible": int(len(cols)), "note": note or "Fewer than two eligible instruments."}
        score = np.zeros(len(cols))
        for f, direction, weight in self.factors:
            score += zscores(pd.Series(self.values[f][i][cols])).to_numpy() * direction * weight
        order = np.argsort(-score, kind="stable")[: s.top_n]
        picked, k = cols[order], len(order)
        window = np.nan_to_num(self.returns[max(0, i - 125): i + 1][:, picked])
        cov = np.cov(window, rowvar=False).reshape(k, k) if len(window) > 20 else None
        if s.weighting == "inverse_volatility":
            vol = self.vol60[i][picked]
            inv = np.where(np.isfinite(vol) & (vol > 0), 1.0 / np.where(vol > 0, vol, 1.0), 0.0)
            w = inv / inv.sum() if inv.sum() > 0 else np.full(k, 1.0 / k)
        elif s.weighting == "min_variance" and cov is not None and k > 1:
            w = min_variance(0.5 * cov + 0.5 * np.diag(np.diag(cov)))
        else:
            w = np.full(k, 1.0 / k)
        caps = [(self.labels[name][picked], cap) for name, cap in (("country", s.max_country_weight), ("sector", s.max_sector_weight),
                                                                  ("currency", s.max_currency_weight)) if cap]
        w = cap_weights(w * s.invest, s.max_weight, caps)
        if s.target_volatility and cov is not None and w.sum() > 0:
            vol = math.sqrt(max(float(w @ cov @ w), 0.0) * 252)
            if vol > s.target_volatility:
                w = w * (s.target_volatility / vol)
        return {"weights": {int(c): float(x) for c, x in zip(picked, w) if x > 0}, "scores": {int(c): float(score[o]) for c, o in zip(picked, order)},
                "eligible": int(len(cols)), "note": note}


def rebalance_days(dates: pd.DatetimeIndex, frequency: str) -> np.ndarray:
    """True on the first trading day of each week or month (known on the day itself: no calendar look-ahead)."""
    period = dates.tz_localize(None).to_period("W" if frequency == "weekly" else "M")
    flags = np.ones(len(dates), dtype=bool)
    flags[1:] = period[1:] != period[:-1]
    flags[0] = False
    return flags


def target_portfolio(panel: Panel, spec: StrategySpec) -> dict:
    """What the specification would hold if it were rebalanced on the latest stored day."""
    sel = Selector(panel, spec)
    i = len(panel.dates) - 1
    picked = sel.select(i)
    rows = [{"instrument_id": panel.ids[c], **{k: panel.meta[panel.ids[c]][k] for k in ("symbol", "name", "market", "country", "currency", "sector")},
             "weight": w, "score": picked["scores"][c], "price": float(sel.price[i][c]),
             "avg_traded_value_20d": float(sel.adv[i][c])} for c, w in picked["weights"].items()]
    rows.sort(key=lambda r: -r["score"])
    invested = sum(r["weight"] for r in rows)
    return {"as_of": panel.dates[i].date().isoformat(), "strategy": spec.strategy_name, "definition": spec.model_dump(), "eligible": picked["eligible"],
            "universe": len(panel.ids), "rows": rows, "invested": invested, "cash": 1 - invested, "note": picked["note"],
            "history_bars": int(len(panel.dates)), "enough_history": bool(len(panel.dates) > WARMUP), "disclaimer": DISCLAIMER}


def _summary(equity: pd.Series, initial: float) -> dict:
    m = compute_metrics(equity, [], initial)
    return {"start": equity.index[0].date().isoformat(), "end": equity.index[-1].date().isoformat(), "total_return": m["total_return"],
            "annualized_return": m["annualized_return"], "sharpe_ratio": m["sharpe_ratio"], "max_drawdown": m["max_drawdown"]}


def backtest(panel: Panel, spec: StrategySpec) -> dict:
    sel = Selector(panel, spec)
    T, N = panel.close.shape
    if T <= WARMUP + 20:
        raise ValueError(f"{T} daily bars of history; at least {WARMUP + 21} are needed (twelve months of warm-up plus a test period).")
    rebalance = rebalance_days(panel.dates, spec.rebalance)
    cost = spec.cost_bps / 10_000.0
    cash = spec.initial_capital
    qty, entry, stop, last_px = np.zeros(N), np.zeros(N), np.zeros(N), np.zeros(N)
    pending_sell: set[int] = set()
    pending_buy: dict[int, float] = {}  # column -> target value decided at the previous close
    equity = np.empty(T - WARMUP)
    bench = np.empty(T - WARMUP)
    bench_value, bench_members = spec.initial_capital, np.zeros(N, dtype=bool)
    trades, costs_paid, traded_value, stops_hit, rebalances, held_counts, skipped_liquidity = [], 0.0, 0.0, 0, 0, [], 0
    last_selection: dict = {}

    def sell(c: int, price: float, reason: str) -> None:
        nonlocal cash, costs_paid, traded_value
        value = qty[c] * price
        fee = value * cost
        cash += value - fee
        costs_paid += fee
        traded_value += value
        trades.append(SimpleNamespace(pnl=(price - entry[c]) * qty[c] - fee - entry[c] * qty[c] * cost, commission=fee, reason=reason))
        qty[c] = 0.0

    for i in range(WARMUP, T):
        o, lo, c_raw = panel.open[i], panel.low[i], panel.close[i]
        # 1. Orders decided at the previous close fill at today's open (instruments without a bar today wait).
        for c in sorted(pending_sell):
            if np.isfinite(o[c]) and qty[c] > 0:
                sell(c, o[c], "rebalance")
                pending_sell.discard(c)
        for c in sorted(pending_buy):
            if not np.isfinite(o[c]) or o[c] <= 0:
                continue
            value = min(pending_buy.pop(c), cash / (1 + cost))
            shares = math.floor(value / o[c])
            if shares <= 0:
                continue
            fee = shares * o[c] * cost
            cash -= shares * o[c] + fee
            costs_paid += fee
            traded_value += shares * o[c]
            qty[c], entry[c], stop[c] = shares, o[c], o[c] * (1 - spec.stop_pct)
        # 2. Protective stops: filled at the stop, or at the open when the price gapped below it.
        for c in np.flatnonzero(qty > 0):
            if np.isfinite(lo[c]) and lo[c] <= stop[c]:
                sell(c, min(stop[c], o[c]) if np.isfinite(o[c]) else stop[c], "stop_loss")
                stops_hit += 1
                pending_sell.discard(c)
        # 3. Mark to the close (a holding without a bar today keeps its last price).
        seen = np.isfinite(c_raw)
        last_px[seen] = c_raw[seen]
        total = cash + float(qty @ last_px)
        equity[i - WARMUP] = total
        day_ret = np.nan_to_num(sel.returns[i][bench_members])
        bench_value *= 1 + (float(day_ret.mean()) if len(day_ret) else 0.0)
        bench[i - WARMUP] = bench_value
        held_counts.append(int((qty > 0).sum()))
        # 4. Rebalance decision at today's close, executed tomorrow.
        if rebalance[i]:
            picked = sel.select(i)
            bench_members = sel.eligible(i)
            last_selection = {"date": panel.dates[i], **picked}
            rebalances += 1
            target = picked["weights"]
            pending_sell = {int(c) for c in np.flatnonzero(qty > 0) if int(c) not in target}
            pending_buy = {}
            for c, w in target.items():
                if qty[c] > 0:
                    continue
                value = w * total
                limit = spec.max_participation * sel.adv[i][c]
                if value > limit:
                    value, skipped_liquidity = limit, skipped_liquidity + 1
                pending_buy[c] = value

    index = panel.dates[WARMUP:]
    curve = pd.Series(equity, index=index)
    bench_curve = pd.Series(bench, index=index)
    metrics = compute_metrics(curve, trades, spec.initial_capital, [h > 0 for h in held_counts])
    years = max((index[-1] - index[0]) / pd.Timedelta(days=365.25), 1e-9)
    split = int(len(curve) * 0.7)
    step = max(1, len(curve) // 400)
    currencies = sorted({m["currency"] for m in panel.meta.values()})
    caveats = [
        "Survivorship bias: companies delisted before your catalog was built are not in the data, so past results are flattered by an unknown amount.",
        "Prices are split- and dividend-adjusted as supplied by the data provider; the adjustment basis is the provider's.",
        f"Costs are a flat {spec.cost_bps:g} basis points of traded value (commission, spread and slippage together); real costs vary by market and size.",
        "Whole shares only; orders fill at the next open, with no partial fills and no market impact beyond the participation limit.",
        "The parameters were not optimised on this history. The two sub-periods show how stable the result was; they are not a forecast.",
    ]
    if len(currencies) > 1:
        caveats.insert(1, f"Prices in {len(currencies)} currencies ({', '.join(currencies[:8])}{'…' if len(currencies) > 8 else ''}) are not converted: results are "
                          "as if currency exposure were hedged at no cost, and the price and traded-value filters apply in each local currency.")
    final_sel = last_selection.get("weights", {})
    return {
        "definition": spec.model_dump(), "strategy": spec.strategy_name,
        "universe": {"instruments": N, "markets": sorted({m["market"] for m in panel.meta.values()}), "currencies": currencies,
                     "data_start": panel.dates[0].date().isoformat(), "test_start": index[0].date().isoformat(), "end": index[-1].date().isoformat()},
        "metrics": metrics,
        "benchmark": {"label": "Equal-weight of all eligible instruments, reset on rebalance days, no costs", **_summary(bench_curve, spec.initial_capital)},
        "sub_periods": [{"label": "First 70% of the period", **_summary(curve.iloc[: split + 1], float(curve.iloc[0]))},
                        {"label": "Last 30% of the period", **_summary(curve.iloc[split:], float(curve.iloc[split]))}],
        "activity": {"rebalances": rebalances, "round_trips": len(trades), "stops_hit": stops_hit, "average_positions": float(np.mean(held_counts)),
                     "turnover_per_year": traded_value / float(curve.mean()) / years, "costs_paid": costs_paid,
                     "buys_reduced_by_liquidity_limit": skipped_liquidity},
        "equity_curve": [{"ts": ts.date().isoformat(), "equity": float(curve.loc[ts]), "benchmark": float(bench_curve.loc[ts])} for ts in index[::step]],
        "last_selection": {"date": last_selection["date"].date().isoformat() if last_selection else None, "note": last_selection.get("note", ""),
                           "rows": [{"instrument_id": panel.ids[c], "symbol": panel.meta[panel.ids[c]]["symbol"], "market": panel.meta[panel.ids[c]]["market"],
                                     "weight": w} for c, w in final_sel.items()]},
        "caveats": caveats, "disclaimer": DISCLAIMER,
    }

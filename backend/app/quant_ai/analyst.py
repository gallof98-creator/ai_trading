"""AI analyst: deterministic, auditable analysis of strategies, trades and market conditions.

Design limits (enforced in code and covered by tests):
  * The analyst only READS results and PROPOSES strategy parameters.
  * A proposal is accepted only if it passes schema validation and an out-of-sample gate, and it
    is applied only by an administrator.
  * Nothing here can change risk limits, the trading mode or live-trading authorization: the
    module has no reference to those objects, and proposals may contain strategy parameters only.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import optuna
import pandas as pd

from ..backtesting.engine import BacktestResult, BacktestSpec, DeploymentSpec, run_backtest
from ..backtesting.metrics import DISCLAIMER
from ..database.settings_store import RiskLimits
from ..market_data.sources import Dataset
from ..paper_trading.broker import ExecutionModel
from ..portfolio.engine import ClosedTrade
from ..strategies.registry import create_strategy, get_strategy_class
from .regime import detect_regimes

optuna.logging.set_verbosity(optuna.logging.WARNING)
MIN_TRADES_FOR_STATISTICS = 30
FORBIDDEN_KEYS = set(RiskLimits.model_fields) | {"trading_mode", "live_trading_enabled", "live_trading_confirmation", "initial_paper_capital",
                                                  "emergency_policy", "broker_id", "is_live"}


# ---------------------------------------------------------------------------- trade analysis
def _bucket(trades: list[ClosedTrade], key) -> list[dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[str(key(t))].append(t.pnl)
    return [{"group": g, "trades": len(p), "net_profit": float(sum(p)), "win_rate": float(np.mean([x > 0 for x in p])),
             "expectancy": float(np.mean(p))} for g, p in sorted(groups.items())]


def analyze_trades(trades: list[ClosedTrade], bars: dict[str, pd.DataFrame] | None = None) -> dict:
    """Break results down by exit reason, direction, strategy, instrument, weekday, hour and market regime at entry."""
    if not trades:
        return {"trades": 0}
    pnl = np.array([t.pnl for t in trades])
    holding = [(pd.Timestamp(t.exit_ts) - pd.Timestamp(t.entry_ts)) / pd.Timedelta(hours=1) for t in trades]
    streak = worst = 0
    for x in pnl:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    out = {
        "trades": len(trades), "by_exit_reason": _bucket(trades, lambda t: t.exit_reason or "unknown"),
        "by_direction": _bucket(trades, lambda t: "long" if t.direction > 0 else "short"),
        "by_strategy": _bucket(trades, lambda t: t.strategy or "manual"), "by_instrument": _bucket(trades, lambda t: t.instrument_id),
        "by_weekday": _bucket(trades, lambda t: pd.Timestamp(t.entry_ts).day_name()),
        "by_entry_hour_utc": _bucket(trades, lambda t: f"{pd.Timestamp(t.entry_ts).hour:02d}"),
        "average_holding_hours": float(np.mean(holding)), "longest_losing_streak": int(worst),
        "largest_win": float(pnl.max()), "largest_loss": float(pnl.min()),
        "top5_share_of_gross_profit": float(np.sort(pnl[pnl > 0])[-5:].sum() / pnl[pnl > 0].sum()) if (pnl > 0).any() else None,
    }
    if bars:
        regimes = {iid: detect_regimes(frame)["primary"] for iid, frame in bars.items()}

        def regime_at_entry(t: ClosedTrade) -> str:
            series = regimes.get(t.instrument_id)
            if series is None:
                return "unknown"
            known = series[series.index < pd.Timestamp(t.entry_ts)]  # last bar that had CLOSED before the entry
            return known.iloc[-1] if len(known) else "unknown"

        out["by_regime_at_entry"] = _bucket(trades, regime_at_entry)
    return out


def statistical_weaknesses(metrics: dict, trades: list[ClosedTrade], costs: dict | None = None, seed: int = 0) -> list[dict]:
    """Checks that commonly separate a real edge from luck or from a fragile backtest."""
    findings: list[dict] = []

    def add(severity: str, code: str, message: str) -> None:
        findings.append({"severity": severity, "code": code, "message": message})

    n = len(trades)
    pnl = np.array([t.pnl for t in trades], dtype=float)
    if n < MIN_TRADES_FOR_STATISTICS:
        add("high", "small_sample", f"Only {n} closed trades. Below {MIN_TRADES_FOR_STATISTICS}, win rate, profit factor and Sharpe are mostly noise.")
    if n >= 2 and pnl.std(ddof=1) > 0:
        t_stat = pnl.mean() / (pnl.std(ddof=1) / math.sqrt(n))
        rng = np.random.default_rng(seed)
        boot = rng.choice(pnl, size=(2000, n), replace=True).mean(axis=1)
        lo, hi = np.quantile(boot, [0.025, 0.975])
        if lo <= 0:
            add("high" if pnl.mean() <= 0 else "medium", "edge_not_significant",
                f"Average trade {pnl.mean():.2f} (t = {t_stat:.2f}); the 95% bootstrap interval [{lo:.2f}, {hi:.2f}] includes zero, "
                "so a true edge of zero cannot be ruled out.")
    if n and (pnl > 0).any():
        share = np.sort(pnl[pnl > 0])[-max(1, n // 20):].sum() / pnl[pnl > 0].sum()
        if share > 0.5 and n >= 20:
            add("medium", "profit_concentration", f"The best {max(1, n // 20)} trades produce {share:.0%} of gross profit; results depend on a few outliers.")
    if metrics.get("profit_factor") is not None and metrics["profit_factor"] < 1:
        add("high", "negative_expectancy", f"Profit factor {metrics['profit_factor']:.2f}: losses exceed gains after costs.")
    if metrics.get("max_drawdown", 0) > 0.25:
        add("high", "deep_drawdown", f"Maximum drawdown {metrics['max_drawdown']:.1%} would be hard to sit through in practice.")
    if metrics.get("sharpe_ratio") is not None and metrics.get("daily_observations", 0) >= 20:
        se = math.sqrt((1 + 0.5 * metrics["sharpe_ratio"] ** 2) / metrics["daily_observations"]) * math.sqrt(metrics.get("periods_per_year", 252))
        if abs(metrics["sharpe_ratio"]) < 2 * se:
            add("medium", "sharpe_uncertain", f"Sharpe {metrics['sharpe_ratio']:.2f} with a standard error of about {se:.2f} from "
                f"{metrics['daily_observations']} daily observations: statistically indistinguishable from zero.")
    if costs and metrics.get("gross_profit"):
        cost = costs.get("commission", 0) + costs.get("spread_slippage", 0) + costs.get("financing", 0)
        if cost > 0.5 * metrics["gross_profit"]:
            add("medium", "cost_sensitive", f"Trading costs ({cost:.2f}) are {cost / metrics['gross_profit']:.0%} of gross profit; "
                "small changes in spread or slippage decide the outcome.")
    if metrics.get("exposure_time") is not None and metrics["exposure_time"] < 0.05 and n:
        add("low", "rarely_in_market", f"In the market only {metrics['exposure_time']:.1%} of the time; few independent observations.")
    return findings


def explain_performance(result: BacktestResult, bars: dict[str, pd.DataFrame] | None = None) -> dict:
    m = result.metrics
    breakdown = analyze_trades(result.trades, bars)
    drivers = []
    if result.trades:
        for dim in ("by_exit_reason", "by_direction", "by_regime_at_entry", "by_strategy"):
            rows = breakdown.get(dim) or []
            if len(rows) > 1:
                best, worst = max(rows, key=lambda r: r["net_profit"]), min(rows, key=lambda r: r["net_profit"])
                drivers.append(f"{dim.replace('by_', '').replace('_', ' ')}: best '{best['group']}' ({best['net_profit']:+.2f} over {best['trades']} trades), "
                               f"worst '{worst['group']}' ({worst['net_profit']:+.2f} over {worst['trades']} trades)")
    headline = (f"Net result {m['net_profit']:+.2f} ({m['total_return']:+.2%}) over {m['number_of_trades']} trades; "
                f"maximum drawdown {m['max_drawdown']:.2%}; win rate {m['win_rate']:.1%}." if m["number_of_trades"] else
                "The strategy closed no trades in this period.")
    return {"headline": headline, "drivers": drivers, "breakdown": breakdown,
            "weaknesses": statistical_weaknesses(m, result.trades, result.costs), "warnings": result.warnings}


def compare(rows: list[dict]) -> dict:
    """Rank comparison rows (from backtesting.compare_strategies) with explicit caveats."""
    usable = [r for r in rows if r.get("sharpe_ratio") is not None and r["number_of_trades"] >= MIN_TRADES_FOR_STATISTICS]
    ranked = sorted(usable, key=lambda r: r["sharpe_ratio"], reverse=True)
    notes = [f"{r['label']}: excluded from ranking ({r['number_of_trades']} trades is too few)" for r in rows if r not in usable]
    if len(ranked) >= 2 and ranked[0]["sharpe_ratio"] - ranked[1]["sharpe_ratio"] < 0.5:
        notes.append("The top configurations are within 0.5 Sharpe of each other: the difference is not meaningful on this sample.")
    if len(rows) > 5:
        notes.append(f"{len(rows)} configurations were compared on the same data; the best of many is biased upwards (multiple testing).")
    return {"ranking": [r["label"] for r in ranked], "notes": notes, "rows": rows}


# ---------------------------------------------------------------------------- market anomalies
def detect_anomalies(bars: pd.DataFrame, window: int = 200, threshold: float = 5.0, recent: int = 50) -> list[dict]:
    """Unusual bars among the most recent `recent`: return, range, volume or opening gap far outside
    their trailing distribution (robust z-score from the median and MAD of the PRIOR `window` bars)."""
    ret = np.log(bars["close"]).diff()
    measures = {"return": ret.abs(), "range": (bars["high"] - bars["low"]) / bars["close"], "volume": bars["volume"],
                "gap": (bars["open"] / bars["close"].shift(1) - 1).abs()}
    out = []
    for name, series in measures.items():
        med = series.rolling(window, min_periods=window // 2).median().shift(1)
        deviation = (series - med).abs()
        mad = deviation.rolling(window, min_periods=window // 2).median().shift(1)
        # When a measure is normally constant (e.g. no opening gaps in a 24h market) the MAD is zero;
        # fall back to the mean deviation, with a floor so the score stays finite.
        mean_dev = deviation.rolling(window, min_periods=window // 2).mean().shift(1)
        floor = 0.01 * med.abs() if name == "volume" else 1e-4
        scale = (1.4826 * mad).where(mad > 0, mean_dev).clip(lower=floor)
        z = (series - med) / scale
        for ts, value in z.iloc[-recent:].items():
            if pd.notna(value) and value > threshold:
                out.append({"ts": ts.isoformat(), "measure": name, "robust_z": float(value), "value": float(series.loc[ts]),
                            "typical": float(med.loc[ts])})
    return sorted(out, key=lambda a: a["ts"], reverse=True)


# ---------------------------------------------------------------------------- configuration proposals
def _search_space(strategy: str) -> dict[str, tuple]:
    """Numeric strategy parameters with their declared bounds, narrowed to a band around the default."""
    schema = get_strategy_class(strategy).Params.model_json_schema()["properties"]
    space = {}
    for name, spec in schema.items():
        kind = spec.get("type")
        if kind not in ("integer", "number") or name in ("atr_period",):
            continue
        default = spec.get("default")
        if default is None:
            continue
        lo = spec.get("minimum", spec.get("exclusiveMinimum", default / 2))
        hi = spec.get("maximum", spec.get("exclusiveMaximum", default * 2))
        lo, hi = max(lo, default / 2.5), min(hi, default * 2.5)
        if kind == "integer":
            lo, hi = int(math.ceil(lo)), int(math.floor(hi))
        if hi > lo:
            space[name] = (kind, lo, hi)
    return space


def validate_proposal(strategy: str, params: dict) -> list[str]:
    """Schema gate. Returns reasons for rejection (empty list = acceptable strategy configuration)."""
    reasons = []
    illegal = sorted(set(params) & FORBIDDEN_KEYS)
    if illegal:
        reasons.append(f"Proposal tries to set non-strategy settings: {', '.join(illegal)}")
    try:
        create_strategy(strategy, params)
    except (ValueError, KeyError) as exc:
        reasons.append(str(exc))
    return reasons


def suggest_configurations(strategy: str, instrument_id: str, timeframe: str, data: Dataset, initial_capital: float,
                           static_fx: dict[str, float] | None = None, limits: RiskLimits | None = None, execution: ExecutionModel | None = None,
                           n_trials: int = 25, seed: int = 0, top: int = 3) -> list[dict]:
    """Search strategy parameters on the first 60% of the data (Optuna, seeded), then judge the best
    candidates ONLY on the remaining 40%. Every candidate is returned with both results and a verdict;
    none is applied."""
    bars = data.bars
    cut = int(len(bars) * 0.6)
    if cut < 500 or len(bars) - cut < 300:
        raise ValueError("At least 800 bars are needed to propose and validate configurations")
    space = _search_space(strategy)
    if not space:
        raise ValueError(f"{strategy} has no numeric parameters to tune")
    limits, execution = limits or RiskLimits(), execution or ExecutionModel()

    def run(frame: pd.DataFrame, params: dict, trade_start=None) -> BacktestResult:
        return run_backtest(BacktestSpec(initial_capital=initial_capital, static_fx=static_fx or {}, limits=limits, execution=execution,
                                         trade_start=trade_start, deployments=[DeploymentSpec(strategy, instrument_id, timeframe, params)],
                                         data={(instrument_id, timeframe): Dataset(frame, data.kind, data.source)}))

    seen: dict[str, tuple[dict, dict]] = {}

    def objective(trial: optuna.Trial) -> float:
        params = {n: (trial.suggest_int(n, lo, hi) if kind == "integer" else round(trial.suggest_float(n, lo, hi), 4)) for n, (kind, lo, hi) in space.items()}
        if validate_proposal(strategy, params):
            return -10.0
        m = run(bars.iloc[:cut], params).metrics
        score = m["sharpe_ratio"] if m["sharpe_ratio"] is not None and m["number_of_trades"] >= 15 else -10.0
        seen[str(sorted(params.items()))] = (params, m)
        return score

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(objective, n_trials=min(n_trials, 60))
    ranked = sorted((v for v in seen.values() if v[1]["sharpe_ratio"] is not None and v[1]["number_of_trades"] >= 15),
                    key=lambda v: v[1]["sharpe_ratio"], reverse=True)[:top]
    proposals = []
    for params, in_sample in ranked:
        oos = run(bars, params, trade_start=bars.index[cut])
        oos_eq = oos.equity[oos.equity.index >= bars.index[cut]]
        oos_return = float(oos_eq.iloc[-1] / oos_eq.iloc[0] - 1) if len(oos_eq) > 1 else 0.0
        oos_trades = [t for t in oos.trades if pd.Timestamp(t.entry_ts) >= bars.index[cut]]
        oos_pf = None
        gains, losses = sum(t.pnl for t in oos_trades if t.pnl > 0), -sum(t.pnl for t in oos_trades if t.pnl < 0)
        if losses > 0:
            oos_pf = gains / losses
        reasons = validate_proposal(strategy, params)
        if len(oos_trades) < MIN_TRADES_FOR_STATISTICS:
            reasons.append(f"only {len(oos_trades)} out-of-sample trades (needs {MIN_TRADES_FOR_STATISTICS})")
        if oos_return <= 0:
            reasons.append(f"out-of-sample return {oos_return:+.2%} is not positive")
        if oos_pf is not None and oos_pf < 1.1:
            reasons.append(f"out-of-sample profit factor {oos_pf:.2f} is below 1.1")
        proposals.append({
            "strategy": strategy, "instrument_id": instrument_id, "timeframe": timeframe, "params": params,
            "in_sample": {k: in_sample[k] for k in ("sharpe_ratio", "total_return", "max_drawdown", "number_of_trades")},
            "out_of_sample": {"total_return": oos_return, "profit_factor": oos_pf, "number_of_trades": len(oos_trades)},
            "trials": len(study.trials), "data_kind": data.kind.value, "data_fingerprint": data.fingerprint(),
            "validation": "passed" if not reasons else "failed", "validation_reasons": reasons,
            "note": f"Best of {len(study.trials)} trials: in-sample figures are optimistically biased. " + DISCLAIMER,
        })
    return proposals


# ---------------------------------------------------------------------------- reports
def research_report(title: str, result: BacktestResult, bars: dict[str, pd.DataFrame] | None = None) -> dict:
    """Structured research report plus a Markdown rendering."""
    exp = explain_performance(result, bars)
    m = result.metrics

    def fmt(v, pct=False):
        return "n/a" if v is None else (f"{v:.2%}" if pct else f"{v:,.2f}")

    lines = [f"# {title}", "", f"> {DISCLAIMER}", "", "## Summary", exp["headline"], "", "## Key figures", "| Metric | Value |", "|---|---|"]
    for label, key, pct in (("Net profit", "net_profit", False), ("Total return", "total_return", True), ("Annualised return", "annualized_return", True),
                            ("Maximum drawdown", "max_drawdown", True), ("Sharpe ratio", "sharpe_ratio", False), ("Sortino ratio", "sortino_ratio", False),
                            ("Profit factor", "profit_factor", False), ("Win rate", "win_rate", True), ("Expectancy per trade", "expectancy", False),
                            ("Trades", "number_of_trades", False), ("Time in market", "exposure_time", True)):
        lines.append(f"| {label} | {fmt(m.get(key), pct)} |")
    lines += ["", "## What drove the result"] + ([f"- {d}" for d in exp["drivers"]] or ["- Not enough trades to attribute results."])
    lines += ["", "## Statistical weaknesses"] + ([f"- **{w['severity']}**: {w['message']}" for w in exp["weaknesses"]] or ["- None of the automated checks fired."])
    if exp["warnings"]:
        lines += ["", "## Data and modelling warnings"] + [f"- {w}" for w in exp["warnings"]]
    lines += ["", f"Configuration hash: `{result.config_hash}`"]
    return {"title": title, "markdown": "\n".join(lines), "explanation": exp, "metrics": m, "config_hash": result.config_hash}

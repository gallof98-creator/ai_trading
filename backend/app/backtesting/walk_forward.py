"""Walk-forward analysis and strategy comparison.

Parameters are chosen on an in-sample window and then judged ONLY on the following, unseen
window. The gap between in-sample and out-of-sample results is the honest measure of how much
of a strategy's apparent edge is overfitting.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

import pandas as pd

from ..database.settings_store import RiskLimits
from ..market_data.sources import Dataset
from ..paper_trading.broker import ExecutionModel
from ..strategies.registry import create_strategy
from .engine import BacktestResult, BacktestSpec, DeploymentSpec, run_backtest
from .metrics import DISCLAIMER, compute_metrics, daily_returns
from .stats import return_stats

MAX_COMBINATIONS = 200  # every extra combination tried makes a lucky in-sample result more likely


@dataclass
class WalkForwardSpec:
    strategy: str
    instrument_id: str
    timeframe: str
    data: Dataset
    param_grid: dict[str, list]
    initial_capital: float
    n_folds: int = 4
    train_bars: int | None = None  # default: 50% of the data
    metric: str = "sharpe_ratio"
    min_trades: int = 10
    fixed_params: dict = field(default_factory=dict)
    base_currency: str = "EUR"
    execution: ExecutionModel = field(default_factory=ExecutionModel)
    limits: RiskLimits = field(default_factory=RiskLimits)
    static_fx: dict[str, float] = field(default_factory=dict)
    instruments: list | None = None  # defaults to the built-in starter universe
    # Extra out-of-sample runs that test how fragile the result is (used by strategy validation): every parameter
    # combination on the unseen window, and the chosen one again with trading costs multiplied by `stress_costs`.
    robustness: bool = False
    stress_costs: float = 2.0
    # Entry filters applied as in live trading (see quant_ai/filters.py), and which ones they are.
    blocked_any: pd.Series | None = None
    blocked_long: pd.Series | None = None
    filters: dict = field(default_factory=dict)
    profile: object | None = None
    family: str | None = None
    regime: pd.Series | None = None


def _grid(spec: WalkForwardSpec) -> list[dict]:
    keys = sorted(spec.param_grid)
    combos = []
    for values in itertools.product(*(spec.param_grid[k] for k in keys)):
        params = {**spec.fixed_params, **dict(zip(keys, values))}
        try:
            create_strategy(spec.strategy, params)
        except ValueError:
            continue  # invalid combination (e.g. fast >= slow)
        combos.append(params)
    if not combos:
        raise ValueError("The parameter grid contains no valid combination")
    if len(combos) > MAX_COMBINATIONS:
        raise ValueError(f"{len(combos)} parameter combinations exceed the limit of {MAX_COMBINATIONS}; narrow the grid")
    return combos


def stressed(execution: ExecutionModel, factor: float) -> ExecutionModel:
    """The same execution model with spread, slippage and every commission multiplied by `factor`."""
    return replace(execution, spread_bps=execution.spread_bps * factor, slippage_bps=execution.slippage_bps * factor,
                   commissions={k: replace(c, rate=c.rate * factor, per_unit=c.per_unit * factor, minimum=c.minimum * factor)
                                for k, c in execution.commissions.items()})


def _run(spec: WalkForwardSpec, bars: pd.DataFrame, params: dict, trade_start: pd.Timestamp | None = None,
         execution: ExecutionModel | None = None) -> BacktestResult:
    return run_backtest(BacktestSpec(
        initial_capital=spec.initial_capital, base_currency=spec.base_currency, execution=execution or spec.execution, limits=spec.limits,
        static_fx=spec.static_fx, trade_start=trade_start, instruments=spec.instruments,
        deployments=[DeploymentSpec(spec.strategy, spec.instrument_id, spec.timeframe, params, blocked_any=spec.blocked_any, blocked_long=spec.blocked_long,
                                    profile=spec.profile, family=spec.family, regime=spec.regime)],
        data={(spec.instrument_id, spec.timeframe): Dataset(bars, spec.data.kind, spec.data.source)}))


def _score(result: BacktestResult, spec: WalkForwardSpec) -> float | None:
    if result.metrics["number_of_trades"] < spec.min_trades:
        return None
    return result.metrics.get(spec.metric)


def walk_forward(spec: WalkForwardSpec) -> dict:
    bars = spec.data.bars
    n = len(bars)
    train = spec.train_bars or n // 2
    test = (n - train) // spec.n_folds
    if spec.n_folds < 1 or test < 50 or train < 200:
        raise ValueError("Not enough data: each fold needs at least 200 training bars and 50 test bars")
    combos = _grid(spec)
    folds, oos_returns, oos_trades = [], [], []
    stress_returns, grid_runs, grid_profitable = [], 0, 0
    costly = stressed(spec.execution, spec.stress_costs) if spec.robustness else None
    for k in range(spec.n_folds):
        tr0, tr1 = k * test, k * test + train
        te1 = tr1 + test if k < spec.n_folds - 1 else n
        best_params, best_score, best_is = None, None, None
        for params in combos:  # deterministic order; ties keep the earlier combination
            is_run = _run(spec, bars.iloc[tr0:tr1], params)
            score = _score(is_run, spec)
            if score is not None and (best_score is None or score > best_score):
                best_params, best_score, best_is = params, score, is_run
        fold = {"fold": k + 1, "train_start": bars.index[tr0].isoformat(), "train_end": bars.index[tr1 - 1].isoformat(),
                "test_start": bars.index[tr1].isoformat(), "test_end": bars.index[te1 - 1].isoformat(),
                "best_params": best_params, "in_sample_score": best_score, "out_of_sample": None,
                # The chosen setting on the bars it was chosen on: reported apart, never mixed with the unseen results.
                "in_sample": {m: best_is.metrics[m] for m in ("total_return", "max_drawdown", "number_of_trades", "win_rate", "profit_factor")}
                if best_is is not None else None}
        if best_params is not None:
            # The test run sees the training bars only to warm up indicators; it may not trade before test_start.
            oos = _run(spec, bars.iloc[tr0:te1], best_params, trade_start=bars.index[tr1])
            eq = oos.equity[oos.equity.index >= bars.index[tr1]]
            fold["out_of_sample"] = {**{m: oos.metrics[m] for m in ("total_return", "max_drawdown", "number_of_trades", "win_rate", "profit_factor")},
                                     "score": oos.metrics.get(spec.metric)}
            oos_returns.append(eq.pct_change().dropna())
            oos_trades += oos.trades
            fold["oos_trades"] = [{**t.to_dict(), "params": best_params} for t in oos.trades]
            if spec.robustness:
                window, start = bars.iloc[tr0:te1], bars.index[tr1]
                again = _run(spec, window, best_params, trade_start=start, execution=costly)
                stress_returns.append(again.equity[again.equity.index >= start].pct_change().dropna())
                # Would a neighbouring parameter choice also have made money on the unseen window? A result that
                # only one setting produces is a fit to noise, however good it looks.
                for params in combos:
                    profit = oos.metrics["total_return"] if params == best_params else _run(spec, window, params, trade_start=start).metrics["total_return"]
                    grid_runs += 1
                    grid_profitable += profit > 0
        folds.append(fold)

    summary: dict = {"folds_with_parameters": sum(f["best_params"] is not None for f in folds)}
    stitched = None
    if oos_returns:
        rets = pd.concat(oos_returns).sort_index()
        stitched = spec.initial_capital * (1 + rets).cumprod()
        summary["out_of_sample_metrics"] = compute_metrics(stitched, [], spec.initial_capital)
        summary["out_of_sample_metrics"]["number_of_trades"] = len(oos_trades)
        scored = [(f["in_sample_score"], f["out_of_sample"]["score"]) for f in folds if f["out_of_sample"] and f["out_of_sample"]["score"] is not None]
        if scored:
            mean_is, mean_oos = sum(a for a, _ in scored) / len(scored), sum(b for _, b in scored) / len(scored)
            summary.update({"mean_in_sample_score": mean_is, "mean_out_of_sample_score": mean_oos,
                            # < 0.5 (or negative) means most of the in-sample result did not survive on unseen data
                            "walk_forward_efficiency": mean_oos / mean_is if mean_is > 0 else None})
        summary["profitable_folds"] = sum(1 for f in folds if f["out_of_sample"] and f["out_of_sample"]["total_return"] > 0)
        # Significance is judged on daily returns of the stitched unseen periods: one observation per day, whatever the bar size.
        summary["out_of_sample_statistics"] = return_stats(daily_returns(stitched))
        if spec.robustness:
            pnls = sorted((t.pnl for t in oos_trades), reverse=True)
            costly_equity = spec.initial_capital * (1 + pd.concat(stress_returns).sort_index()).cumprod()
            summary["robustness"] = {
                "cost_multiplier": spec.stress_costs, "return_with_stressed_costs": float(costly_equity.iloc[-1] / spec.initial_capital - 1),
                "parameter_runs": grid_runs, "parameter_runs_profitable": int(grid_profitable),
                "profitable_parameter_share": grid_profitable / grid_runs if grid_runs else None,
                "net_profit": float(sum(pnls)), "best_trade": float(pnls[0]) if pnls else 0.0,
                "net_profit_without_best_trade": float(sum(pnls[1:])) if pnls else 0.0}
    return {
        "strategy": spec.strategy, "instrument_id": spec.instrument_id, "timeframe": spec.timeframe, "metric": spec.metric,
        "combinations_tested": len(combos), "folds": folds, "summary": summary,
        "out_of_sample_equity": [] if stitched is None else [{"ts": t.isoformat(), "equity": float(v)} for t, v in stitched.iloc[::max(1, len(stitched) // 1000)].items()],
        "data_kind": spec.data.kind.value, "disclaimer": DISCLAIMER,
    }


COMPARE_FIELDS = ("net_profit", "total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
                  "profit_factor", "win_rate", "expectancy", "number_of_trades", "exposure_time")


def compare_strategies(specs: dict[str, BacktestSpec]) -> list[dict]:
    """Run several configurations on their data and tabulate the key statistics side by side."""
    rows = []
    for label, spec in specs.items():
        result = run_backtest(spec)
        rows.append({"label": label, "config_hash": result.config_hash, **{f: result.metrics[f] for f in COMPARE_FIELDS},
                     "warnings": result.warnings})
    return rows

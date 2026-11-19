"""Trade statistics by asset class x strategy x exit reason, in the base currency and in R.

Pure functions over closed-trade dictionaries (ClosedTrade.to_dict()). A trade without a base-currency P&L (no exchange
rate at its close) is counted apart and never summed in another unit; a trade without an initial risk has no R.
"""
from __future__ import annotations

import math
from collections.abc import Iterable

MANUAL_REASONS = ("closed_at_broker", "closed_by_user")


def _max_drawdown(values: list[float]) -> float:
    """Largest fall of the cumulative sum from its running peak (same unit as the values)."""
    peak = equity = worst = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def metrics(trades: list[dict], min_sample: int) -> dict:
    trades = sorted(trades, key=lambda t: t["exit_ts"])
    base = [t["pnl_base"] for t in trades if t.get("pnl_base") is not None]
    r = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    wins, losses = [v for v in base if v > 0], [v for v in base if v < 0]
    n = len(trades)
    top3 = sorted(base, reverse=True)[:3]
    costs = [(t.get("commission") or 0.0) + (t.get("funding_quote") or 0.0) for t in trades]
    out = {
        "trades": n, "with_base_pnl": len(base), "without_fx": n - len(base), "with_r": len(r),
        "pnl_base": sum(base), "expectancy_base": sum(base) / len(base) if base else None,
        "expectancy_r": sum(r) / len(r) if r else None, "total_r": sum(r) if r else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else (math.inf if wins else None),
        "avg_win_base": sum(wins) / len(wins) if wins else None, "avg_loss_base": sum(losses) / len(losses) if losses else None,
        "avg_win_r": _avg([v for v in r if v > 0]), "avg_loss_r": _avg([v for v in r if v < 0]),
        "max_drawdown_base": _max_drawdown(base), "max_drawdown_r": _max_drawdown(r) if r else None,
        "pnl_base_without_top3": sum(base) - sum(top3) if len(base) > 3 else None,
        "manual_close_share": sum(t.get("exit_reason") in MANUAL_REASONS for t in trades) / n if n else None,
        "avg_cost_quote": sum(costs) / n if n else None,
        "avg_est_spread_slippage_quote": _avg([t.get("est_cost_quote") or 0.0 for t in trades]),
        "insufficient_sample": n < min_sample,
    }
    # Below the minimum sample a win rate is noise: it is not shown.
    out["win_rate"] = None if n < min_sample or not base else len(wins) / len(base)
    if out["profit_factor"] == math.inf:
        out["profit_factor"] = None
        out["profit_factor_note"] = "no losing trade"
    return out


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def breakdown(trades: Iterable[dict], min_sample: int, by: tuple[str, ...] = ("asset_class", "strategy", "exit_reason")) -> dict:
    """Metrics for every combination of `by` (and the totals), plus the comparison of manual exits with their shadows."""
    trades = list(trades)
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        groups.setdefault(tuple(t.get(k) or "unknown" for k in by), []).append(t)
    rows = [{**dict(zip(by, key, strict=True)), **metrics(g, min_sample)} for key, g in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]])]
    return {"by": list(by), "rows": rows, "total": metrics(trades, min_sample), "shadow": shadow_summary(trades, min_sample),
            "min_sample": min_sample}


def shadow_summary(trades: list[dict], min_sample: int) -> dict:
    """Manual exits against the exits the system would have made, on the trades whose shadow has finished."""
    manual = [t for t in trades if t.get("exit_reason") in MANUAL_REASONS]
    done = [t for t in manual if (t.get("shadow") or {}).get("status") == "done" and t.get("r_multiple") is not None
            and t["shadow"].get("pnl_r") is not None]
    actual = [t["r_multiple"] for t in done]
    shadow = [t["shadow"]["pnl_r"] for t in done]
    reasons: dict[str, int] = {}
    for t in done:
        reasons[t["shadow"]["exit_reason"]] = reasons.get(t["shadow"]["exit_reason"], 0) + 1
    status: dict[str, int] = {}
    for t in manual:
        s = (t.get("shadow") or {}).get("status", "none")
        status[s] = status.get(s, 0) + 1
    return {"manual_trades": len(manual), "compared": len(done), "status": status,
            "actual_total_r": sum(actual) if done else None, "shadow_total_r": sum(shadow) if done else None,
            "actual_expectancy_r": _avg(actual), "shadow_expectancy_r": _avg(shadow), "shadow_exit_reasons": reasons,
            "insufficient_sample": len(done) < min_sample,
            "meaning": "R of the trades you closed by hand, against the R the system's own exit (stop, take profit, strategy "
                       "signal) would have given on the same trades. Positive difference: your manual exits added value."}

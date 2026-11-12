"""Performance statistics. All figures describe a SIMULATION and say nothing certain about the future."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..portfolio.engine import ClosedTrade

DISCLAIMER = ("Simulated results. Past and simulated performance do not guarantee future returns; "
              "live results will differ because of liquidity, latency, costs and changing market conditions.")


def _finite(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("1D").last().dropna().pct_change().dropna()


def periods_per_year(equity: pd.Series) -> float:
    """365 when the data trades on weekends (crypto), otherwise 252."""
    days = equity.resample("1D").last().dropna().index
    return 365.0 if len(days) and (days.day_of_week >= 5).mean() > 0.15 else 252.0


def drawdown_curve(equity: pd.Series) -> pd.Series:
    return 1 - equity / equity.cummax()


def compute_metrics(equity: pd.Series, trades: list[ClosedTrade], initial_capital: float, in_market: list[bool] | None = None) -> dict:
    final = float(equity.iloc[-1]) if len(equity) else initial_capital
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    years = max((equity.index[-1] - equity.index[0]) / pd.Timedelta(days=365.25), 1e-9) if len(equity) > 1 else 0.0
    total_return = final / initial_capital - 1
    dd = drawdown_curve(equity) if len(equity) else pd.Series(dtype=float)
    max_dd = float(dd.max()) if len(dd) else 0.0
    max_dd_amount = float((equity.cummax() - equity).max()) if len(equity) else 0.0
    rets = daily_returns(equity) if len(equity) > 1 else pd.Series(dtype=float)
    ppy = periods_per_year(equity) if len(equity) > 1 else 252.0
    sharpe = sortino = None
    if len(rets) >= 20 and rets.std(ddof=1) > 0:
        sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(ppy)
        downside = math.sqrt((np.minimum(rets, 0) ** 2).mean())
        sortino = rets.mean() / downside * math.sqrt(ppy) if downside > 0 else None
    annualized = (final / initial_capital) ** (1 / years) - 1 if years > 0 and final > 0 else None
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    return {
        "initial_capital": initial_capital, "final_equity": final,
        "net_profit": final - initial_capital, "gross_profit": gross_profit, "gross_loss": gross_loss,
        "total_return": total_return, "annualized_return": _finite(annualized),
        "number_of_trades": int(len(pnls)), "win_rate": _finite(len(wins) / len(pnls)) if len(pnls) else None,
        "profit_factor": _finite(gross_profit / gross_loss) if gross_loss > 0 else None,
        "expectancy": _finite(pnls.mean()) if len(pnls) else None,
        "average_win": _finite(wins.mean()) if len(wins) else None, "average_loss": _finite(losses.mean()) if len(losses) else None,
        "max_drawdown": max_dd, "max_drawdown_amount": max_dd_amount,
        "sharpe_ratio": _finite(sharpe), "sortino_ratio": _finite(sortino),
        "calmar_ratio": _finite(annualized / max_dd) if annualized is not None and max_dd > 0 else None,
        "recovery_factor": _finite((final - initial_capital) / max_dd_amount) if max_dd_amount > 0 else None,
        "exposure_time": _finite(float(np.mean(in_market))) if in_market else None,
        "total_commission_quote": float(sum(t.commission for t in trades)),
        "periods_per_year": ppy, "daily_observations": int(len(rets)),
    }


def monthly_returns(equity: pd.Series, initial_capital: float) -> list[dict]:
    if equity.empty:
        return []
    month_end = equity.resample("ME").last().dropna()
    prev = month_end.shift(1).fillna(initial_capital)
    return [{"month": ts.strftime("%Y-%m"), "return": float(v / p - 1)} for ts, v, p in zip(month_end.index, month_end, prev)]


def trade_distribution(trades: list[ClosedTrade], bins: int = 20) -> list[dict]:
    if not trades:
        return []
    counts, edges = np.histogram([t.pnl for t in trades], bins=min(bins, max(1, len(trades))))
    return [{"from": float(a), "to": float(b), "count": int(c)} for a, b, c in zip(edges[:-1], edges[1:], counts)]

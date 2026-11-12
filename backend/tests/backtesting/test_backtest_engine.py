import numpy as np
import pandas as pd
import pytest

from app.backtesting.engine import BacktestSpec, DeploymentSpec, run_backtest
from app.database.settings_store import RiskLimits
from app.domain.fees import CommissionModel
from app.market_data.quality import DataKind
from app.market_data.sources import Dataset, synthetic_bars
from app.paper_trading.broker import ExecutionModel
from app.strategies.base import Strategy, StrategyParams
from app.strategies.registry import _REGISTRY, register

BTC, ETH, AAPL = "CRYPTO:BTC/USDT", "CRYPTO:ETH/USDT", "STOCK:AAPL"
NO_COST = ExecutionModel(spread_bps=0, slippage_bps=0, participation_rate=None,
                         commissions={k: CommissionModel() for k in ("crypto", "stock", "etf", "forex", "future")})
LOOSE = RiskLimits(max_exposure_per_asset=5, max_exposure_per_strategy=5, max_exposure_per_broker=5, max_gross_leverage=5,
                   max_margin_utilization=1, max_correlated_exposure=5, max_daily_loss=0.5, max_drawdown=0.9)


def dataset(close, start="2024-01-01", tf="1h", high=None, low=None, open_=None):
    close = np.asarray(close, dtype=float)
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(close), freq=pd.Timedelta(hours=1) if tf == "1h" else pd.Timedelta(days=1), name="ts")
    o = np.concatenate([[close[0]], close[:-1]]) if open_ is None else np.asarray(open_, dtype=float)
    h = np.maximum(o, close) if high is None else np.asarray(high, dtype=float)
    l = np.minimum(o, close) if low is None else np.asarray(low, dtype=float)
    return Dataset(pd.DataFrame({"open": o, "high": h, "low": l, "close": close, "volume": 1e9}, index=idx), DataKind.HISTORICAL, "test")


@pytest.fixture
def scripted():
    """A strategy whose target per bar is given explicitly, so expected fills can be computed by hand."""

    class Scripted(Strategy):
        name, category = "scripted_test", "test"

        class Params(StrategyParams):
            targets: list[int]
            fixed_stop: float = 10.0

        def target(self, df):
            t = self.params.targets
            return np.array((t + [t[-1]] * len(df))[: len(df)])

        def generate(self, df):
            out = super().generate(df)
            out["target"] = self.target(df) if self.params.allow_short else np.clip(self.target(df), 0, 1)
            change = out["target"].diff().fillna(out["target"])
            out["action"] = np.select([change > 0, change < 0], ["BUY", "SELL"], "HOLD")
            out["stop_distance"] = self.params.fixed_stop
            out["take_profit_distance"] = self.params.take_profit_atr * self.params.fixed_stop if self.params.take_profit_atr else np.nan
            out["trailing_distance"] = self.params.trailing_atr * self.params.fixed_stop if self.params.trailing_atr else np.nan
            return out

    register(Scripted)
    yield "scripted_test"
    del _REGISTRY["scripted_test"]


def spec(data, deployments, capital=100_000.0, **kw):
    kw.setdefault("execution", NO_COST)
    kw.setdefault("limits", LOOSE)
    kw.setdefault("static_fx", {"USDT": 1.0, "USD": 1.0})
    return BacktestSpec(initial_capital=capital, deployments=deployments, data=data, **kw)


def test_fills_at_next_open_sizing_and_pnl_match_hand_calculation(scripted):
    #        bar:  0    1    2    3    4    5
    close = [100, 100, 104, 108, 112, 112]
    targets = [0, 1, 1, 1, 0, 0]  # go long on bar 1's close, flat on bar 4's close
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": targets, "fixed_stop": 10.0, "take_profit_atr": None})
    r = run_backtest(spec({(BTC, "1h"): dataset(close)}, [d]))
    assert len(r.trades) == 1
    t = r.trades[0]
    # Signal at close of bar 1 -> filled at the OPEN of bar 2 (= close of bar 1 = 100), never at bar 1's own prices.
    assert t.entry_ts == pd.Timestamp("2024-01-01 02:00", tz="UTC") and t.entry_price == 100
    assert t.exit_ts == pd.Timestamp("2024-01-01 05:00", tz="UTC") and t.exit_price == 112
    assert t.qty == pytest.approx(100.0)  # 1% of 100,000 at risk / 10 stop distance
    assert t.pnl == pytest.approx(1200.0) and r.metrics["net_profit"] == pytest.approx(1200.0)
    assert r.metrics["final_equity"] == pytest.approx(101_200.0) and t.exit_reason == "signal"
    # Equity is marked to market at every close while the position is open.
    assert r.equity.loc[pd.Timestamp("2024-01-01 04:00", tz="UTC")] == pytest.approx(100_000 + 100 * 8)


def test_stop_loss_take_profit_and_no_reentry_after_stop(scripted):
    close = [100, 100, 99, 85, 100, 130, 130]
    low = [100, 100, 98, 84, 85, 100, 130]
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1], "fixed_stop": 10.0, "take_profit_atr": None})
    r = run_backtest(spec({(BTC, "1h"): dataset(close, low=low)}, [d]))
    assert len(r.trades) == 1 and r.trades[0].exit_reason == "stop_loss"
    assert r.trades[0].exit_price == 90.0 and r.trades[0].pnl == pytest.approx(-1000.0)  # lost exactly the 1% that was risked
    assert r.open_positions == 0  # target stayed long, but a stopped-out position is not re-entered

    high = [100, 100, 101, 125, 125, 125, 125]
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1], "fixed_stop": 10.0, "take_profit_atr": 2.0})
    r = run_backtest(spec({(BTC, "1h"): dataset([100, 100, 100, 124, 124, 124, 124], high=high)}, [d]))
    assert r.trades[0].exit_reason == "take_profit" and r.trades[0].exit_price == 120.0 and r.trades[0].pnl == pytest.approx(2000.0)


def test_trailing_stop_ratchets_and_short_side(scripted):
    close = [100, 100, 110, 120, 130, 118, 118]
    low = [100, 100, 100, 110, 120, 117, 118]
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1], "fixed_stop": 10.0, "take_profit_atr": None, "trailing_atr": 1.0})
    r = run_backtest(spec({(BTC, "1h"): dataset(close, low=low)}, [d]))
    assert r.trades[0].exit_reason == "trailing_stop" and r.trades[0].exit_price == 120.0  # stop trailed to 130 - 10
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": [0, -1, -1, 0], "fixed_stop": 10.0, "take_profit_atr": None})
    r = run_backtest(spec({(BTC, "1h"): dataset([100, 100, 95, 90, 90, 90])}, [d]))
    assert r.trades[0].direction == -1 and r.trades[0].pnl == pytest.approx(100 * 10)
    r = run_backtest(spec({(BTC, "1h"): dataset([100, 100, 95, 90, 90, 90])}, [DeploymentSpec(scripted, BTC, "1h", {
        "targets": [0, -1, -1, 0], "fixed_stop": 10.0, "allow_short": False})]))
    assert r.trades == []


def test_costs_reduce_results_by_the_expected_amount(scripted):
    close = [100, 100, 104, 108, 112, 112]
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1, 1, 1, 0, 0], "fixed_stop": 10.0, "take_profit_atr": None})
    costly = ExecutionModel(spread_bps=20, slippage_bps=10, participation_rate=None, commissions={"crypto": CommissionModel(rate=0.001)})
    r = run_backtest(spec({(BTC, "1h"): dataset(close)}, [d], execution=costly))
    buy, sell = 100 * 1.002, 112 * 0.998
    expected = 100 * (sell - buy) - 0.001 * 100 * (buy + sell)
    assert r.metrics["net_profit"] == pytest.approx(expected) and r.trades[0].pnl == pytest.approx(expected)
    assert r.costs["commission"] == pytest.approx(0.001 * 100 * (buy + sell))


def test_backtest_is_reproducible_and_config_hash_tracks_inputs():
    data = {(BTC, "1h"): synthetic_bars("1h", 3000, seed=5)}
    deps = [DeploymentSpec("ema_crossover", BTC, "1h", {"fast": 10, "slow": 40}), DeploymentSpec("donchian_breakout", BTC, "1h")]
    a, b = run_backtest(spec(data, deps, execution=ExecutionModel())), run_backtest(spec(data, deps, execution=ExecutionModel()))
    assert a.config_hash == b.config_hash and a.metrics == b.metrics
    pd.testing.assert_series_equal(a.equity, b.equity)
    assert [t.to_dict() for t in a.trades] == [t.to_dict() for t in b.trades]
    assert a.metrics["number_of_trades"] > 20 and set(a.per_strategy) == {"ema_crossover", "donchian_breakout"}
    other = run_backtest(spec({(BTC, "1h"): synthetic_bars("1h", 3000, seed=6)}, deps, execution=ExecutionModel()))
    changed = run_backtest(spec(data, [DeploymentSpec("ema_crossover", BTC, "1h", {"fast": 11, "slow": 40}), deps[1]], execution=ExecutionModel()))
    assert len({a.config_hash, other.config_hash, changed.config_hash}) == 3
    assert any("SIMULATED" in w for w in a.warnings) and "do not guarantee" in a.disclaimer


def test_future_data_cannot_change_past_results():
    full = synthetic_bars("1h", 2500, seed=9).bars
    tampered = full.copy()
    tampered.iloc[1500:, :4] *= 1.7
    deps = [DeploymentSpec("macd", BTC, "1h"), DeploymentSpec("bollinger_reversion", BTC, "1h")]

    def run(frame):
        return run_backtest(spec({(BTC, "1h"): Dataset(frame, DataKind.SIMULATED, "t")}, deps, execution=ExecutionModel()))

    a, b = run(full), run(tampered)
    cutoff = full.index[1500]
    pd.testing.assert_series_equal(a.equity[a.equity.index <= cutoff], b.equity[b.equity.index <= cutoff])
    # The bar that OPENS at the cutoff already carries changed prices, so compare trades closed strictly before it.
    assert [t.to_dict() for t in a.trades if t.exit_ts < cutoff] == [t.to_dict() for t in b.trades if t.exit_ts < cutoff]
    assert len([t for t in a.trades if t.exit_ts < cutoff]) > 10


def test_multi_instrument_multi_timeframe_and_multi_currency_portfolio():
    data = {(BTC, "1h"): synthetic_bars("1h", 2000, seed=1, start_price=40_000), (ETH, "1h"): synthetic_bars("1h", 2000, seed=2, start_price=2_500),
            (AAPL, "1d"): synthetic_bars("1d", 600, seed=3, start="2022-01-03", start_price=150, annual_vol=0.3)}
    aapl = data[(AAPL, "1d")].bars
    data[(AAPL, "1d")] = Dataset(aapl[aapl.index.day_of_week < 5], DataKind.SIMULATED, "t")
    deps = [DeploymentSpec("ema_crossover", BTC, "1h", {"fast": 10, "slow": 30}), DeploymentSpec("rsi_reversion", BTC, "4h"),
            DeploymentSpec("donchian_breakout", ETH, "1h"), DeploymentSpec("sma_crossover", AAPL, "1d", {"fast": 5, "slow": 20})]
    r = run_backtest(spec(data, deps, static_fx={"USDT": 0.9, "USD": 0.92}, execution=ExecutionModel()))
    assert {t.instrument_id for t in r.trades} == {BTC, ETH, AAPL}
    assert {t.strategy for t in r.trades} == {"ema_crossover", "rsi_reversion", "donchian_breakout", "sma_crossover"}
    assert r.equity.index.is_monotonic_increasing and r.equity.index.is_unique
    aapl_trades = [t for t in r.trades if t.instrument_id == AAPL]
    assert all(t.entry_ts.day_of_week < 5 for t in map(lambda t: pd.Series({"entry_ts": pd.Timestamp(t.entry_ts)}), aapl_trades))
    assert any("Static FX" in w for w in r.warnings)
    # 4h signals come from completed 4h bars only: every rsi_reversion entry is at a 4h boundary.
    assert all(pd.Timestamp(t.entry_ts).hour % 4 == 0 for t in r.trades if t.strategy == "rsi_reversion")


def test_risk_limits_are_enforced_inside_the_backtest(scripted):
    close = [100.0] * 3 + [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110]
    d = DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1], "fixed_stop": 5.0, "take_profit_atr": None})
    tight = LOOSE.model_copy(update={"max_exposure_per_asset": 0.1})
    r = run_backtest(spec({(BTC, "1h"): dataset(close)}, [d], limits=tight))
    assert r.open_positions == 1 and r.orders_rejected == 0  # sized down to the cap instead of being rejected
    # Risk sizing alone would buy 200 units (1% / 5); the 10% exposure cap allows about 100.
    assert r.metrics["final_equity"] == pytest.approx(100_000 + 100 * 10, rel=1e-4)

    # Daily-loss breaker: after losing 3% in a day, a second strategy's entry is rejected.
    crash = [100.0, 100, 100, 60, 60, 60, 60, 60]
    deps = [DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1], "fixed_stop": 5.0, "take_profit_atr": None})]
    lim = LOOSE.model_copy(update={"max_daily_loss": 0.03})
    r = run_backtest(spec({(BTC, "1h"): dataset(crash, low=[100, 100, 100, 60, 60, 60, 60, 60], open_=[100, 100, 100, 100, 60, 60, 60, 60])}, deps, limits=lim))
    assert r.trades[0].exit_reason == "stop_loss" and r.trades[0].exit_price == 95.0
    kinds = [e["kind"] for e in r.risk_events]
    assert kinds == [] or "breaker_tripped" not in kinds  # a 1% stop-out does not trip a 3% daily limit
    big = [DeploymentSpec(scripted, BTC, "1h", {"targets": [0, 1], "fixed_stop": 5.0, "take_profit_atr": None}),
           DeploymentSpec("rate_of_change", ETH, "1h", {"period": 1, "threshold": 0.0, "atr_period": 2})]
    gap = dataset(crash, low=[100, 100, 100, 60, 60, 60, 60, 60], open_=[100, 100, 100, 60, 60, 60, 60, 60])  # gaps THROUGH the stop
    eth = dataset([10, 10, 10, 10, 10, 10.1, 10.2, 10.3], high=[10.4] * 8, low=[9.9] * 8)  # first ETH buy signal comes after the crash
    r = run_backtest(spec({(BTC, "1h"): gap, (ETH, "1h"): eth}, big, limits=lim))
    assert r.trades[0].exit_price == 60.0 and r.trades[0].pnl == pytest.approx(-200 * 40)  # gap loss is larger than the planned 1%
    assert "breaker_tripped" in [e["kind"] for e in r.risk_events]
    late_rejections = [e for e in r.risk_events if e["kind"] == "order_rejected" and "daily_loss" in " ".join(e["detail"]["reasons"])]
    assert late_rejections and r.orders_rejected >= 1


def test_trade_start_validation_and_metrics_fields():
    data = {(BTC, "1h"): synthetic_bars("1h", 2000, seed=4)}
    cut = data[(BTC, "1h")].bars.index[1000]
    r = run_backtest(spec(data, [DeploymentSpec("ema_crossover", BTC, "1h", {"fast": 10, "slow": 30})], trade_start=cut, execution=ExecutionModel()))
    assert r.trades and min(pd.Timestamp(t.entry_ts) for t in r.trades) >= cut
    expected = {"net_profit", "gross_profit", "total_return", "annualized_return", "win_rate", "profit_factor", "max_drawdown", "sharpe_ratio",
                "sortino_ratio", "calmar_ratio", "expectancy", "average_win", "average_loss", "number_of_trades", "exposure_time", "recovery_factor"}
    assert expected <= set(r.metrics)
    payload = r.to_dict()
    assert payload["equity_curve"] and payload["drawdown_curve"] and payload["monthly_returns"] and payload["trade_distribution"]
    assert sum(b["count"] for b in payload["trade_distribution"]) == r.metrics["number_of_trades"]
    for bad in (dict(capital=0), dict(deployments=[])):
        with pytest.raises(ValueError):
            run_backtest(spec(data, bad.get("deployments", [DeploymentSpec("macd", BTC, "1h")]), capital=bad.get("capital", 1000.0)))
    with pytest.raises(ValueError, match="No data"):
        run_backtest(spec({}, [DeploymentSpec("macd", BTC, "1h")]))
    with pytest.raises(ValueError, match="only once"):
        run_backtest(spec(data, [DeploymentSpec("macd", BTC, "1h"), DeploymentSpec("macd", BTC, "1h")]))

"""Trade measurement: base-currency P&L at the close's rate, costs and funding, initial risk and R, shadow exits, statistics."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.analytics.report import breakdown, metrics
from app.analytics.shadow import shadow_r, simulate_exit
from app.brokers.base import BrokerManager
from app.domain.models import AssetClass, Fill, Instrument, Side
from app.instruments.registry import InstrumentRegistry
from app.market_data.fx import FxRateProvider
from app.portfolio.engine import PortfolioEngine

T0 = datetime(2026, 3, 2, 14, tzinfo=timezone.utc)


def portfolio(*instruments, rates=(("USD", "EUR", 0.9),)):
    reg = InstrumentRegistry()
    for i in instruments:
        reg.register(i)
    fx = FxRateProvider(conversion_cost_bps=0.0)
    for a, b, r in rates:
        fx.add_rate(a, b, T0 - timedelta(hours=1), r, "test")
    return PortfolioEngine(reg, BrokerManager(), fx, "EUR")


def inst(iid, quote, cls=AssetClass.STOCK, **kw):
    return Instrument(id=iid, asset_class=cls, symbol=iid.split(":")[1], quote_currency=quote, calendar="24x7", lot_size=0.0001, **kw)


def trade(pf, iid, qty, entry, exit_, side=Side.BUY, commission=0.0, stop=None, strategy="s"):
    pf.on_fill(Fill(order_id="o1", broker_id="b", instrument_id=iid, side=side, qty=qty, price=entry, commission=commission, ts=T0, strategy=strategy))
    if stop:
        pf.set_initial_stop(("b", iid, strategy), stop)
    out = Side.SELL if side is Side.BUY else Side.BUY
    return pf.on_fill(Fill(order_id="o2", broker_id="b", instrument_id=iid, side=out, qty=qty, price=exit_, commission=commission,
                           ts=T0 + timedelta(hours=2), strategy=strategy))


# ---------------------------------------------------------------- currency conversion
def test_pnl_is_kept_in_the_quote_currency_and_converted_to_base_at_the_close():
    pf = portfolio(inst("STOCK:AAPL", "USD"))
    t = trade(pf, "STOCK:AAPL", 10, 100.0, 110.0, commission=1.0)
    assert t.pnl_quote == pytest.approx(100 - 2) and t.pnl_net_quote == pytest.approx(98)
    assert t.fx_rate == pytest.approx(0.9) and t.fx_source == "test" and t.pnl_base == pytest.approx(98 * 0.9) and t.pnl == pytest.approx(t.pnl_base)


def test_a_currency_without_a_rate_is_flagged_never_summed_in_another_unit():
    pf = portfolio(inst("CRYPTO:ETH/XYZ", "XYZ", AssetClass.CRYPTO))
    t = trade(pf, "CRYPTO:ETH/XYZ", 1, 100.0, 110.0)
    assert t.pnl_base is None and t.fx_source == "unavailable" and t.pnl == 0.0
    s = metrics([t.to_dict()], 30)
    assert s["without_fx"] == 1 and s["pnl_base"] == 0


def test_stablecoins_go_through_the_configured_alias_and_crypto_quotes_through_the_platforms_own_price():
    pf = portfolio(inst("CRYPTO:SOL/USDT", "USDT", AssetClass.CRYPTO), inst("CRYPTO:ETH/BTC", "BTC", AssetClass.CRYPTO))
    pf.fx_aliases = {"USDT": "USD"}
    t = trade(pf, "CRYPTO:SOL/USDT", 2, 100.0, 105.0)
    assert t.pnl_base == pytest.approx(10 * 0.9) and "USDT=USD at par (assumption)" in t.fx_source
    pf.prices["CRYPTO:BTC/USD"] = 50_000.0
    t = trade(pf, "CRYPTO:ETH/BTC", 1, 0.05, 0.06)
    assert t.pnl_base == pytest.approx(0.01 * 50_000 * 0.9) and "BTC/USD platform price" in t.fx_source


# ---------------------------------------------------------------- costs, funding, R
def test_r_multiple_uses_the_stop_at_entry_and_the_net_result_including_funding():
    pf = portfolio(inst("CRYPTO:BTC/USD", "USD", AssetClass.CRYPTO, perpetual=True))
    pf.cost_bps = lambda i: 7.5
    pf.on_fill(Fill(order_id="o1", broker_id="b", instrument_id="CRYPTO:BTC/USD", side=Side.BUY, qty=0.1, price=50_000, commission=5.0, ts=T0, strategy="s"))
    pf.set_initial_stop(("b", "CRYPTO:BTC/USD", "s"), 1_000.0)       # risk 0.1 x 1,000 = 100 USD
    pf.add_funding(("b", "CRYPTO:BTC/USD", "s"), 3.0)                   # funding paid while open
    t = pf.on_fill(Fill(order_id="o2", broker_id="b", instrument_id="CRYPTO:BTC/USD", side=Side.SELL, qty=0.1, price=52_000, commission=5.0,
                        ts=T0 + timedelta(hours=8), strategy="s"))
    assert t.funding_quote == pytest.approx(3.0) and t.pnl_net_quote == pytest.approx(200 - 10 - 3)
    assert t.initial_risk_quote == pytest.approx(100) and t.r_multiple == pytest.approx(1.87)
    assert t.initial_risk_base == pytest.approx(90) and t.est_cost_quote == pytest.approx(0.1 * 102_000 * 7.5 / 10_000)


def test_a_partial_close_carries_its_share_of_risk_and_funding():
    pf = portfolio(inst("STOCK:AAPL", "USD"))
    pf.on_fill(Fill(order_id="o1", broker_id="b", instrument_id="STOCK:AAPL", side=Side.BUY, qty=10, price=100, commission=0, ts=T0, strategy="s"))
    pf.set_initial_stop(("b", "STOCK:AAPL", "s"), 5.0)
    pf.add_funding(("b", "STOCK:AAPL", "s"), 2.0)
    t = pf.on_fill(Fill(order_id="o2", broker_id="b", instrument_id="STOCK:AAPL", side=Side.SELL, qty=4, price=110, commission=0, ts=T0, strategy="s"))
    assert t.initial_risk_quote == pytest.approx(20) and t.funding_quote == pytest.approx(0.8) and t.r_multiple == pytest.approx((40 - 0.8) / 20)


# ---------------------------------------------------------------- shadow exits
def bars(rows, start=T0):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def test_shadow_follows_stop_target_gaps_and_trailing_from_the_manual_close_on():
    b = bars([(100, 101, 99, 100), (100, 104, 99.5, 103), (103, 108, 102, 107)])
    r = simulate_exit(b, b.index[1], 1, stop=95.0, target=106.0)
    assert (r.reason, r.price, r.bars) == ("take_profit", 106.0, 2)
    gap = bars([(100, 101, 99, 100), (90, 91, 88, 89)])
    assert simulate_exit(gap, gap.index[0], 1, stop=95.0, target=110.0).price == 90.0   # opened through the stop: the open
    both = bars([(100, 107, 94, 100)])
    assert simulate_exit(both, both.index[0], 1, stop=95.0, target=106.0).reason == "stop_loss"
    assert simulate_exit(both, both.index[0], 1, stop=95.0, target=106.0, same_bar="target_first").reason == "take_profit"
    trail = bars([(100, 106, 99, 105), (105, 111, 104, 110), (110, 110, 104, 105)])
    r = simulate_exit(trail, trail.index[0], 1, stop=95.0, target=None, trailing=5.0)
    assert (r.reason, r.price) == ("stop_loss", 105.0)                 # trailed to 110 - 5 = 105
    short = bars([(100, 101, 96, 97)])
    assert simulate_exit(short, short.index[0], -1, stop=105.0, target=96.5).reason == "take_profit"


def test_shadow_takes_the_strategys_own_exit_at_the_bar_close_and_waits_while_nothing_happened():
    b = bars([(100, 101, 99, 100), (100, 102, 99, 101), (101, 102, 100, 101.5)])
    target = pd.Series([1, 1, 0], index=b.index)
    r = simulate_exit(b, b.index[0], 1, stop=90.0, target=120.0, strategy_target=target, bar_delta=pd.Timedelta(hours=1))
    assert (r.reason, r.price, r.ts) == ("signal", 101.5, b.index[2] + pd.Timedelta(hours=1))
    pending = simulate_exit(b, b.index[0], 1, stop=90.0, target=120.0)
    assert pending.reason is None and pending.bars == 3
    assert simulate_exit(b, b.index[0], 1, 90.0, 120.0, max_days=0.01).reason == "expired"
    assert shadow_r(1, 100, 106, 10, 1, 50.0, 4.0) == pytest.approx((60 - 4) / 50)


# ---------------------------------------------------------------- statistics
def rows(n, pnl=10.0, r=1.0, **kw):
    return [{"exit_ts": f"2026-03-{1 + k % 28:02d}T00:00:00", "pnl_base": pnl, "r_multiple": r, "commission": 1.0, **kw} for k in range(n)]


def test_metrics_hide_the_win_rate_below_the_minimum_sample_and_report_the_rest():
    small = metrics(rows(5, 10) + rows(3, -5, -0.5), 30)
    assert small["insufficient_sample"] and small["win_rate"] is None
    assert small["profit_factor"] == pytest.approx(50 / 15) and small["expectancy_base"] == pytest.approx((50 - 15) / 8)
    big = metrics(rows(20, 10) + rows(10, -5, -0.5), 30)
    assert not big["insufficient_sample"] and big["win_rate"] == pytest.approx(20 / 30)
    assert big["pnl_base_without_top3"] == pytest.approx(200 - 50 - 30)
    assert metrics(rows(2, -5) + rows(1, 20), 30)["max_drawdown_base"] >= 5


def test_breakdown_groups_by_asset_class_strategy_and_exit_and_compares_manual_exits_with_their_shadows():
    trades = (rows(2, 10, 1.0, asset_class="crypto", strategy="rsi_reversion", exit_reason="closed_at_broker",
                   shadow={"status": "done", "pnl_r": 2.0, "exit_reason": "take_profit"})
              + rows(3, -5, -1.0, asset_class="crypto", strategy="rsi_reversion", exit_reason="stop_loss")
              + rows(1, 7, 0.7, asset_class="stock", strategy="ema_crossover", exit_reason="signal"))
    out = breakdown(trades, 30)
    keys = {(r["asset_class"], r["strategy"], r["exit_reason"]) for r in out["rows"]}
    assert keys == {("crypto", "rsi_reversion", "closed_at_broker"), ("crypto", "rsi_reversion", "stop_loss"), ("stock", "ema_crossover", "signal")}
    assert out["total"]["manual_close_share"] == pytest.approx(2 / 6)
    sh = out["shadow"]
    assert sh["compared"] == 2 and sh["actual_total_r"] == pytest.approx(2.0) and sh["shadow_total_r"] == pytest.approx(4.0)

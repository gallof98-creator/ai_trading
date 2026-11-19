"""Accounts joining, leaving or changing their capital basis are flows of capital, never gains or losses; and a crypto
broker's fee taken out of the coins bought is booked as a commission instead of stopping trading as a discrepancy."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.domain.models import AccountBalance, AssetClass, Position
from tests.integration.test_semi_auto import T0, make_rt


def loss_and_drawdown(rt):
    st = rt.portfolio.revalue(rt.clock())
    return 1 - st.equity / st.day_start_equity, 1 - st.equity / st.peak_equity


class ExternalAccount:
    """A minimal non-simulated account whose equity and capital basis the test sets."""

    def __init__(self, broker_id, equity, basis="account"):
        self.broker_id, self.equity, self.capital_basis = broker_id, equity, basis
        self.capabilities = SimpleNamespace(is_simulated=False, asset_classes=frozenset({AssetClass.CRYPTO}))
        self.positions = []

    def get_account_balance(self):
        return AccountBalance(broker_id=self.broker_id, currency="EUR", cash=self.equity, equity=self.equity)

    def get_positions(self):
        return self.positions

    def is_connected(self):
        return True


def test_connecting_a_large_account_then_switching_it_to_a_small_virtual_capital_is_not_a_loss(tmp_path):
    rt = make_rt(tmp_path)
    assert loss_and_drawdown(rt) == (0.0, 0.0)
    alpaca = ExternalAccount("alpaca-paper", 86_000.0)  # the broker's own paper balance, counted before a virtual capital is set
    rt.brokers.register(alpaca)
    rt.now[0] += timedelta(minutes=1)
    assert loss_and_drawdown(rt) == pytest.approx((0.0, 0.0))
    alpaca.equity, alpaca.capital_basis = 1_000.0, "virtual:2026-11-16:1000"
    rt.now[0] += timedelta(minutes=1)
    assert loss_and_drawdown(rt) == pytest.approx((0.0, 0.0)), "a change of capital basis was taken for a 99% loss"
    alpaca.equity = 900.0  # a real loss on the virtual capital still counts
    rt.now[0] += timedelta(minutes=1)
    loss, dd = loss_and_drawdown(rt)
    assert loss == pytest.approx(100 / 2_000) and dd == pytest.approx(100 / 2_000)
    rt.brokers.remove("alpaca-paper")
    rt.now[0] += timedelta(minutes=1)
    loss, dd = loss_and_drawdown(rt)
    # The account left with what it had (a withdrawal at its current value): the 100 it lost today still counts, now
    # against the 1,100 that remained in play.
    assert loss == pytest.approx(100 / 1_100) and dd == pytest.approx(100 / 1_100)


def test_reference_levels_survive_a_restart_and_restart_once_after_an_update(tmp_path):
    rt = make_rt(tmp_path)
    rt.portfolio.revalue(rt.clock())
    state = rt.portfolio.to_state()
    assert state["contributions"] == {"paper-main": [pytest.approx(1000.0), "account"]}
    old = {k: v for k, v in state.items() if k != "contributions"}
    old["peak_equity"] = old["day_start_equity"] = 87_000.0   # recorded by an earlier version while a broker balance was counted
    rt.portfolio.load_state(old)
    assert loss_and_drawdown(rt) == (0.0, 0.0)


def test_a_crypto_fee_taken_in_coins_is_booked_as_commission_not_a_discrepancy(tmp_path):
    rt = make_rt(tmp_path)
    iid = "CRYPTO:BTC/USD"
    acct = ExternalAccount("alpaca-paper", 1_000.0)
    rt.brokers.register(acct)
    from app.portfolio.engine import Slice

    key = ("alpaca-paper", iid, "ema_crossover")
    rt.portfolio.slices[key] = Slice(0.01, 68_000.0, T0, 1.0)
    acct.positions = [Position(broker_id="alpaca-paper", instrument_id=iid, qty=0.009975, avg_price=68_000.0)]
    assert rt.portfolio.reconcile() == []
    assert rt.portfolio.slices[key].qty == pytest.approx(0.009975)
    assert rt.portfolio.slices[key].entry_commission == pytest.approx(1.0 + 0.000025 * 68_000.0)
    # More coins than the books, or a gap larger than any fee, is still a discrepancy.
    acct.positions = [Position(broker_id="alpaca-paper", instrument_id=iid, qty=0.0095, avg_price=68_000.0)]
    assert rt.portfolio.reconcile() and not rt.portfolio.reconciled
    acct.positions = [Position(broker_id="alpaca-paper", instrument_id=iid, qty=0.011, avg_price=68_000.0)]
    assert rt.portfolio.reconcile()


def test_positions_are_sized_on_the_capital_of_the_account_that_trades_not_on_all_accounts(tmp_path):
    """Two accounts of 1,000 EUR each. A position at one of them must be sized and capped on ITS 1,000 EUR: sizing on the
    2,000 of both made positions twice as large, and let one account hold twice its own capital."""
    from app.domain.models import Order, Side
    from app.risk.models import OrderIntent
    from app.risk.sizing import position_size

    rt = make_rt(tmp_path)
    rt.create_paper_account("paper-2", "Second", "admin")
    iid = "CRYPTO:BTC/USD"
    inst = rt.registry.get(iid)
    rt.portfolio.mark(iid, 50_000.0, rt.clock())
    state = rt.engine._risk_state(rt.clock())
    assert state.equity == pytest.approx(2_000.0) and state.account_equity == {"paper-main": pytest.approx(1_000.0), "paper-2": pytest.approx(1_000.0)}
    fx = 0.9
    # Risk 2% of 1,000 = 20 EUR at a 1,000 USD stop: 0.0222 BTC, not the 0.0444 that 2,000 EUR would give.
    qty = position_size(state.scoped("paper-main").equity, 0.02, 1_000.0, 50_000.0, inst, fx)
    assert qty == pytest.approx(20 / (1_000 * fx), rel=1e-3)
    # The 25% per-instrument cap is 250 EUR of THIS account.
    room = rt.risk.headroom("paper-main", inst, "s", state)
    assert room == pytest.approx(250.0, rel=1e-3)
    big = Order(broker_id="paper-main", instrument_id=iid, side=Side.BUY, qty=0.01, strategy="s")   # 450 EUR: 45% of the account
    decision = rt.risk.evaluate(OrderIntent(big, inst, 50_000.0, fx, 1_000.0), state)
    assert not decision.approved and any("exposure in CRYPTO:BTC/USD" in r for r in decision.reasons)
    # Positions at the other account do not use up this one's room.
    from app.risk.models import Exposure

    state.exposures.append(Exposure("paper-2", iid, "s", "crypto", "USD", 240.0, 240.0))
    assert rt.risk.headroom("paper-main", inst, "s", state) == pytest.approx(250.0, rel=1e-3)


def test_portfolio_returns_day_by_day_weekly_and_monthly_exclude_capital_moves(tmp_path):
    rt = make_rt(tmp_path)
    pf = rt.portfolio
    acct = ExternalAccount("alpaca-paper", 1_000.0)
    paper = rt.brokers.get("paper-main")
    day0 = T0.replace(hour=12)

    def at(day):
        rt.now[0] = day0 + timedelta(days=day)
        pf.revalue(rt.clock())

    at(0)                                    # 1,000 (internal paper)
    paper.cash += 20.0; at(1)                # +2%
    rt.brokers.register(acct); at(2)         # an account of 1,000 joins: a capital move, not +100%
    acct.equity = 1_000.0 - 40.6; at(3)       # -40.6 on 2,020 (1,020 + 1,000)
    r = pf.returns()
    by_day = {d["period"]: d["return"] for d in r["daily"]}
    days = sorted(by_day)
    assert by_day[days[0]] == pytest.approx(0.0)
    assert by_day[days[1]] == pytest.approx(0.02)
    assert by_day[days[2]] == pytest.approx(0.0), "linking an account was counted as a gain"
    assert by_day[days[3]] == pytest.approx(-40.6 / 2_020)
    assert r["since_start"] == pytest.approx(1.02 * (1 - 40.6 / 2_020) - 1)
    assert r["today"] == by_day[days[3]] and r["month_to_date"] is not None and r["week_to_date"] is not None
    assert sum(w["days"] for w in r["weekly"]) == 4 and sum(m["days"] for m in r["monthly"]) == 4
    # Kept across a restart.
    state = pf.to_state()
    pf.daily = {}
    pf.load_state(state)
    assert pf.returns()["since_start"] == pytest.approx(r["since_start"])


def _crypto_rt(tmp_path, **limits):
    from app.database.settings_store import save_settings

    rt = make_rt(tmp_path)
    rt.add_instruments(["ETH/USD", "SOL/USD", "LTC/USD"], "", "crypto", "admin")
    for i, px in (("CRYPTO:BTC/USD", 50_000.0), ("CRYPTO:ETH/USD", 3_000.0), ("CRYPTO:SOL/USD", 150.0), ("CRYPTO:LTC/USD", 80.0)):
        rt.portfolio.mark(i, px, rt.clock())
    with rt.db.session() as s:
        save_settings(s, rt.user_settings.model_copy(update={"risk": rt.user_settings.risk.model_copy(update=limits)}))
        rt.reload_settings(s)
    return rt


def _hold(rt, iid, qty, stop_distance):
    from app.domain.models import Fill, Side

    rt.portfolio.on_fill(Fill(order_id=f"x{iid}", broker_id="paper-main", instrument_id=iid, side=Side.BUY if qty > 0 else Side.SELL,
                              qty=abs(qty), price=rt.portfolio.prices[iid], commission=0, ts=rt.clock(), strategy="s"))
    rt.portfolio.set_initial_stop(("paper-main", iid, "s"), stop_distance)


def test_position_size_is_fixed_fractional_on_marked_to_market_equity():
    from app.domain.models import AssetClass, Instrument
    from app.risk.sizing import position_size

    inst = Instrument(id="CRYPTO:BTC/USD", asset_class=AssetClass.CRYPTO, symbol="BTC/USD", quote_currency="USD", lot_size=0.00001)
    # 2% of 1,000 EUR = 20 EUR at risk; stop 1,000 USD away at 0.9 EUR per USD: 20 / 900 BTC.
    assert position_size(1_000, 0.02, 1_000, 50_000, inst, 0.9) == pytest.approx(20 / 900, abs=1e-5)
    # Equity up 10% (unrealised gains count): the next position is 10% larger, without closing anything.
    assert position_size(1_100, 0.02, 1_000, 50_000, inst, 0.9) == pytest.approx(22 / 900, abs=1e-5)


def test_crypto_factor_cap_limits_positions_in_the_same_direction_and_their_total_risk_when_switched_on(tmp_path):
    from app.domain.models import Order, Side
    from app.risk.models import OrderIntent

    rt = _crypto_rt(tmp_path, crypto_factor_cap=True, crypto_max_same_direction=2, crypto_max_total_risk=0.03, max_exposure_per_asset=5,
                    max_gross_leverage=5, max_exposure_per_broker=5, max_correlated_exposure=5, max_exposure_per_strategy=5)
    _hold(rt, "CRYPTO:BTC/USD", 0.01, 1_000.0)     # risk 10 USD = 9 EUR
    _hold(rt, "CRYPTO:ETH/USD", 0.1, 100.0)        # risk 10 USD = 9 EUR
    state = rt.engine._risk_state(rt.clock())
    assert rt.risk.crypto_risk(state, 0.02) == pytest.approx(18.0)
    sol = rt.registry.get("CRYPTO:SOL/USD")
    third = Order(broker_id="paper-main", instrument_id=sol.id, side=Side.BUY, qty=0.1, strategy="s2")
    d = rt.risk.evaluate(OrderIntent(third, sol, 150.0, 0.9, 10.0), state)
    assert not d.approved and any("already 2 crypto positions long" in r for r in d.reasons)
    short = third.model_copy(update={"side": Side.SELL})          # the other direction is not the same bet
    assert not any("crypto positions" in r for r in rt.risk.evaluate(OrderIntent(short, sol, 150.0, 0.9, 10.0), state).reasons)
    # Total risk: 18 EUR held, the cap is 3% of 1,000 = 30 EUR: 12 EUR of room for a new crypto position.
    assert rt.risk.crypto_risk_room("paper-main", sol, state) == pytest.approx(12.0)
    big_short = Order(broker_id="paper-main", instrument_id=sol.id, side=Side.SELL, qty=1.0, strategy="s2")   # 1 x 20 USD = 18 EUR at risk
    assert any("risk to the stops of all crypto" in r for r in rt.risk.evaluate(OrderIntent(big_short, sol, 150.0, 0.9, 20.0), state).reasons)


def test_crypto_factor_cap_is_off_by_default_and_changes_nothing_then(tmp_path):
    from app.domain.models import Order, Side
    from app.risk.models import OrderIntent

    rt = _crypto_rt(tmp_path)
    assert rt.user_settings.risk.crypto_factor_cap is False and rt.user_settings.risk.risk_per_trade == 0.02
    for iid, q, sd in (("CRYPTO:BTC/USD", 0.001, 100.0), ("CRYPTO:ETH/USD", 0.01, 10.0), ("CRYPTO:SOL/USD", 0.1, 1.0)):
        _hold(rt, iid, q, sd)
    state = rt.engine._risk_state(rt.clock())
    ltc = rt.registry.get("CRYPTO:LTC/USD")
    o = Order(broker_id="paper-main", instrument_id=ltc.id, side=Side.BUY, qty=0.1, strategy="s2")
    assert not any("crypto" in r for r in rt.risk.evaluate(OrderIntent(o, ltc, 80.0, 0.9, 2.0), state).reasons)
    assert rt.risk.crypto_risk_room("paper-main", ltc, state) is None

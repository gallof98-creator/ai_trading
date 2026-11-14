"""Risk sizing with costs, stale-data and spread guards, semi-automatic proposals, and the Alpaca adapter."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.brokers.alpaca_adapter import LIVE_URL, PAPER_URL, AlpacaAdapter
from app.brokers.base import BrokerError, ConnectionLost, OrderRejected, RateLimited
from app.core.config import Settings
from app.database.base import Database
from app.database.models import AuditEntry, BrokerAccount, Notification
from app.database.settings_store import RiskLimits, UserSettings, initial_settings, save_settings
from app.domain.models import AssetClass, Instrument, Order, OrderStatus, OrderType, Side
from app.execution.runtime import RuntimeError_, TradingRuntime
from app.instruments.registry import InstrumentRegistry, default_instruments
from app.live_trading.gate import LiveTradingNotAuthorized, authorize_live_trading
from app.risk.engine import RiskEngine
from app.risk.models import OrderIntent, PortfolioState
from app.risk.sizing import position_size
from tests.integration.test_live import Feeder

T0 = datetime(2026, 11, 13, 15, 0, 0, tzinfo=timezone.utc)  # a Friday, inside the US session
STOCK = Instrument(id="STOCK:X", asset_class=AssetClass.STOCK, symbol="X", quote_currency="USD", calendar="XNYS", lot_size=1, min_qty=1)


def test_position_size_keeps_the_loss_including_costs_within_the_risk_budget():
    # 1,000 EUR at 2% = 20 EUR. Stop 2.00 USD away, 0.50 USD of estimated costs per share, 1 USD = 0.90 EUR.
    plain = position_size(1000, 0.02, 2.0, 100.0, STOCK, 0.9)
    with_costs = position_size(1000, 0.02, 2.0, 100.0, STOCK, 0.9, cost_per_unit=0.5)
    assert (plain, with_costs) == (11, 8)                 # 20 / (2.0 * 0.9) = 11.1 ; 20 / (2.5 * 0.9) = 8.9, rounded down
    assert with_costs * 2.5 * 0.9 <= 20 < plain * 2.5 * 0.9   # the uncorrected size would lose 24.75 EUR at the stop
    assert position_size(1000, 0.02, 2.0, 100.0, STOCK, 0.9, cost_per_unit=0.5, max_notional=300) == 3  # capital limit: 300 EUR / 90 EUR a share
    assert position_size(1000, 0.02, 50.0, 100.0, STOCK, 0.9) == 0  # below one share: no position, never rounded up

    limits = RiskLimits(risk_per_trade=0.02)
    risk = RiskEngine(lambda: limits)
    state = PortfolioState(ts=T0, equity=1000, day_start_equity=1000, peak_equity=1000)

    def intent(qty, **kw):
        return OrderIntent(Order(broker_id="b", instrument_id="STOCK:X", side=Side.BUY, qty=qty), STOCK, 20.0, 0.9, 2.0, **kw)

    assert risk.evaluate(intent(11), state).approved                      # 19.8 EUR at the stop when costs are ignored
    refused = risk.evaluate(intent(11, cost_per_unit=0.5), state)         # 24.75 EUR once costs are counted
    assert not refused.approved and "exceeds risk per trade 2.0000%" in refused.reasons[0]
    assert risk.evaluate(intent(8, cost_per_unit=0.5), state).approved
    stale = risk.evaluate(intent(8, data_age_seconds=61), state)
    assert not stale.approved and "price data is 61s old" in stale.reasons[0]
    assert risk.evaluate(intent(8, data_age_seconds=5, spread_bps=10), state).approved
    wide = risk.evaluate(intent(8, spread_bps=80), state)
    assert not wide.approved and "spread 80 bps exceeds limit 50 bps" in wide.reasons[0]
    closing = OrderIntent(Order(broker_id="b", instrument_id="STOCK:X", side=Side.SELL, qty=1, reduce_only=True), STOCK, 20.0, 0.9, None, data_age_seconds=999)
    assert risk.evaluate(closing, state).approved  # reducing risk is never blocked by stale data


def test_initial_profile_and_automatic_mode_guard():
    s = initial_settings()
    assert (s.initial_paper_capital, s.base_currency, s.trading_mode, s.risk.risk_per_trade) == (1000.0, "EUR", "semi_auto", 0.02)
    with pytest.raises(ValueError, match="safety limits"):
        UserSettings(trading_mode="auto")
    assert UserSettings(trading_mode="auto", safety_limits_confirmed=True).trading_mode == "auto"
    with pytest.raises(ValueError):
        RiskLimits(risk_per_trade=0.06)  # the platform never allows more than 5% per trade


# ---------------------------------------------------------------- semi-automatic proposals on a live feed
def make_rt(tmp_path, name="t"):
    db = Database(f"sqlite:///{tmp_path}/{name}.db")
    db.create_all()  # no settings saved: the installation starts from the initial profile
    now = [T0]
    rt = TradingRuntime(db, Settings(_env_file=None), clock=lambda: now[0])
    rt.start()
    rt.create_paper_account("paper-main", "Main", "admin")
    rt.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", T0 - timedelta(hours=1))
    rt.add_instruments(["BTC/USD"], "", "crypto", "admin")
    feeder = Feeder(rt.live_hub)
    rt.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.asset_class in (AssetClass.CRYPTO, AssetClass.STOCK) else None)]
    rt.now, rt.feeder = now, feeder
    return rt


def tick(rt, symbol, price, minute, second=10):
    ts = T0 + timedelta(minutes=minute, seconds=second)
    rt.now[0] = ts + timedelta(milliseconds=200)
    rt.feeder.trade(symbol, price, ts, 0.01)


def proposal_on_live_feed(tmp_path, name="t", symbol="BTC/USD", iid="CRYPTO:BTC/USD", broker="paper-main", base=68000.0):
    """A falling then rising price makes the fast average cross above the slow one on one-minute bars."""
    rt = make_rt(tmp_path, name)
    dep = rt.add_deployment("ema_crossover", {"fast": 2, "slow": 5, "allow_short": False}, iid, "1m", broker, "admin")
    rt.update_deployment(dep, "admin", active=True)
    rt.live_subscribe([iid], "admin")
    minute = 0
    path = [base * (1 - 0.0005 * k) for k in range(60)] + [base * (0.97 + 0.002 * k) for k in range(1, 30)]
    for price in path:
        tick(rt, symbol, price, minute)
        minute += 1
        if rt.engine.approvals:
            break
    assert rt.engine.approvals, "no proposal was produced"
    return rt, minute, next(iter(rt.engine.approvals.values())).intent.price  # the proposal's own entry price


def test_semi_automatic_proposal_shows_everything_and_sends_nothing_until_approved(tmp_path):
    rt, minute, price = proposal_on_live_feed(tmp_path)
    assert rt.user_settings.trading_mode == "semi_auto" and not rt.portfolio.slices
    assert not [o for o in rt.engine.orders.values() if o.status is OrderStatus.SUBMITTED]  # nothing went to the broker
    [p] = rt.proposals()
    assert (p["symbol"], p["side"], p["strategy"], p["timeframe"], p["broker_id"], p["currency"]) == ("BTC/USD", "BUY", "ema_crossover", "1m", "paper-main", "USD")
    assert p["stop_price"] < p["entry_price"] < p["take_profit_price"] and p["reward_risk"] == pytest.approx(2.0)   # stop 2 ATR, target 4 ATR
    assert 0 < p["risk_base"] <= 20.0 and p["risk_fraction"] <= 0.02 and p["base_currency"] == "EUR" and p["fx_rate"] == 0.9   # 2% of 1,000 EUR
    stop_distance = p["entry_price"] - p["stop_price"]
    cost = rt.cost_per_unit(rt.registry.get("CRYPTO:BTC/USD"), p["entry_price"])
    by_risk = 20.0 / ((stop_distance + cost) * 0.9)          # Risk Amount / Loss per Unit, costs and FX included
    by_capital = 0.25 * 1000 / (p["entry_price"] * 0.9)      # at most 25% of equity in one instrument
    assert p["qty"] == pytest.approx(min(by_risk, by_capital), abs=2e-5) and by_capital < by_risk  # here the capital limit is the tighter one
    assert p["notional_base"] <= 250.0
    assert p["estimated_costs_base"] > 0 and p["notional_base"] == pytest.approx(p["qty"] * p["entry_price"] * 0.9)
    r = p["rationale"]
    assert r["strategy"] == "ema_crossover" and r["category"] == "trend" and r["params"]["fast"] == 2 and r["signal_price"] == p["entry_price"]
    assert p["live"]["fresh"] and p["live"]["status"] == "LIVE" and 0 < p["seconds_left"] <= 15 * 60

    # Approval on a fresh price: the risk engine judges again, then the order goes to the broker and fills on the next live minute.
    tick(rt, "BTC/USD", price * 1.0001, minute, 30)
    order = rt.resolve_approval(p["id"], True, "trader@example.com")
    assert order.status is OrderStatus.SUBMITTED and rt.proposals() == []
    with pytest.raises(RuntimeError_, match="No pending approval"):
        rt.resolve_approval(p["id"], True, "trader@example.com")  # one approval sends one order
    tick(rt, "BTC/USD", price * 1.0002, minute + 1)
    tick(rt, "BTC/USD", price * 1.0003, minute + 2)
    key = ("paper-main", "CRYPTO:BTC/USD", "ema_crossover")
    assert rt.portfolio.qty(key) == pytest.approx(p["qty"])
    stops = [o for o in rt.engine.orders.values() if o.is_open and o.type is OrderType.STOP]
    assert len(stops) == 1 and stops[0].reduce_only
    with rt.db.session() as s:
        assert "order.approved" in [a.action for a in s.query(AuditEntry)]


@pytest.mark.parametrize("scenario", ["rejected", "expired", "stale", "moved", "kill_switch", "expired_at_click"])
def test_a_proposal_is_never_sent_when_conditions_changed(tmp_path, scenario):
    rt, minute, price = proposal_on_live_feed(tmp_path, scenario)
    [p] = rt.proposals()
    sent = lambda: [o for o in rt.engine.orders.values() if o.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED) and not o.reduce_only]  # noqa: E731
    if scenario == "rejected":
        assert rt.resolve_approval(p["id"], False, "trader@example.com").status is OrderStatus.CANCELLED
    elif scenario == "expired":
        rt.now[0] += timedelta(minutes=16)
        assert rt.proposals() == []  # lapsed on its own
        with rt.db.session() as s:
            assert any("Proposal expired" in n.title for n in s.query(Notification))
        with pytest.raises(RuntimeError_, match="No pending approval"):
            rt.resolve_approval(p["id"], True, "trader@example.com")
    elif scenario == "expired_at_click":
        rt.now[0] += timedelta(minutes=16)
        with pytest.raises(RuntimeError_, match="the proposal expired"):
            rt.resolve_approval(p["id"], True, "trader@example.com")
    elif scenario == "stale":
        rt.now[0] += timedelta(minutes=3)  # no tick for three minutes: the live price is no longer usable
        with pytest.raises(RuntimeError_, match="stale or missing"):
            rt.resolve_approval(p["id"], True, "trader@example.com")
    elif scenario == "moved":
        tick(rt, "BTC/USD", price * 1.02, minute, 40)  # the price ran away from the proposed entry
        with pytest.raises(RuntimeError_, match="the price moved"):
            rt.resolve_approval(p["id"], True, "trader@example.com")
    else:
        tick(rt, "BTC/USD", price * 1.0001, minute, 30)
        rt.kill_switch(True, "emergency stop", "admin")
        assert rt.resolve_approval(p["id"], True, "trader@example.com").status is OrderStatus.REJECTED
    assert sent() == [] and not rt.portfolio.slices and p["id"] not in rt.engine.approvals


# ---------------------------------------------------------------- Alpaca adapter against an emulated API
class FakeAlpaca:
    def __init__(self):
        self.equity, self.cash, self.buying_power = 100_000.0, 100_000.0, 200_000.0
        self.orders: dict[str, dict] = {}
        self.positions: list[dict] = []
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail: int | None = None
        self.assets = [{"symbol": "AAPL", "tradable": True, "shortable": True, "fractionable": True, "class": "us_equity"},
                       {"symbol": "SPY", "tradable": True, "shortable": False, "fractionable": True, "class": "us_equity"},
                       {"symbol": "BTC/USD", "tradable": True, "shortable": False, "fractionable": True, "class": "crypto"}]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        import json

        path, body = request.url.path, json.loads(request.content) if request.content else None
        self.requests.append((request.method, str(request.url).split("?")[0], body))
        if request.headers.get("APCA-API-KEY-ID") != "PK-PAPER":
            return httpx.Response(401, json={"message": "unauthorized."})
        if self.fail:
            code, self.fail = self.fail, None
            return httpx.Response(code, json={"message": "insufficient buying power"}, headers={"Retry-After": "2"} if code == 429 else {})
        if path == "/v2/account":
            return httpx.Response(200, json={"equity": str(self.equity), "cash": str(self.cash), "buying_power": str(self.buying_power), "currency": "USD", "status": "ACTIVE"})
        if path == "/v2/assets":
            wanted = request.url.params["asset_class"]
            return httpx.Response(200, json=[a for a in self.assets if a["class"] == wanted])
        if path == "/v2/positions":
            return httpx.Response(200, json=self.positions)
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": True, "next_close": "2026-11-13T21:00:00Z"})
        if path == "/v2/orders" and request.method == "POST":
            if any(o["client_order_id"] == body["client_order_id"] for o in self.orders.values()):
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            oid = f"alp-{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, "status": "accepted", "filled_qty": "0", "filled_avg_price": None, **body}
            return httpx.Response(200, json=self.orders[oid])
        if path == "/v2/orders" and request.method == "GET":
            return httpx.Response(200, json=[o for o in self.orders.values() if o["status"] in ("accepted", "new", "partially_filled")])
        if path == "/v2/orders:by_client_order_id":
            found = [o for o in self.orders.values() if o["client_order_id"] == request.url.params["client_order_id"]]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={"message": "order not found"})
        if path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None:
                return httpx.Response(404, json={"message": "order not found"})
            if request.method == "DELETE":
                if order["status"] == "filled":
                    return httpx.Response(422, json={"message": "order is not cancelable"})
                order["status"] = "canceled"
                return httpx.Response(204)
            if request.method == "PATCH":
                order.update(body)
            return httpx.Response(200, json=order)
        return httpx.Response(404, json={"message": "not found"})

    def fill(self, oid: str, price: float, qty: float | None = None):
        o = self.orders[oid]
        done = float(qty if qty is not None else o["qty"])
        o.update(status="filled" if done >= float(o["qty"]) else "partially_filled", filled_qty=str(done), filled_avg_price=str(price))


def adapter(fake, registry=None, **kw):
    registry = registry or InstrumentRegistry()
    if not registry.all():
        for inst in default_instruments():
            registry.register(inst)
    return AlpacaAdapter("alpaca-paper", registry, "PK-PAPER", "SECRET", client=fake.client(), **kw), registry


def test_alpaca_adapter_orders_account_and_errors():
    fake = FakeAlpaca()
    a, registry = adapter(fake, virtual_capital=1000.0, capital_fx=1.08)
    assert a.trading_url == PAPER_URL and a.environment == "paper" and not a.is_live
    a.connect()
    assert registry.is_mapped("alpaca-paper", "STOCK:AAPL") and registry.is_mapped("alpaca-paper", "ETF:SPY")
    assert not registry.is_mapped("alpaca-paper", "FOREX:EUR/USD") and not registry.is_mapped("alpaca-paper", "CRYPTO:BTC/USDT")  # not everything is tradable here

    # Virtual capital: 1,000 EUR converted once at 1.08, then moved only by the broker account's own profit and loss.
    bal = a.get_account_balance()
    assert (bal.currency, bal.equity, bal.cash) == ("USD", pytest.approx(1080.0), pytest.approx(1080.0))
    fake.equity += 12.5
    assert a.get_account_balance().equity == pytest.approx(1092.5)
    assert a.broker_account()["equity"] == 100_012.5 and a.broker_account()["buying_power"] == 200_000  # the broker's figures stay separate
    entry = a.adjust_virtual_capital(500, 1.10, "deposit")
    assert entry["amount_account"] == pytest.approx(550) and a.get_account_balance().equity == pytest.approx(1642.5)
    with pytest.raises(BrokerError, match="cannot exceed"):
        a.adjust_virtual_capital(-5000, 1.10, "withdrawal")
    assert [e["kind"] for e in a.to_state()["ledger"]] == ["initial_capital", "deposit"] and a.to_state()["capital_fx"] == 1.08

    buy = Order(id="o1", client_order_id="dep1|bar|open", broker_id="alpaca-paper", instrument_id="STOCK:AAPL", side=Side.BUY, qty=3, strategy="ema")
    assert a.unsupported_reasons(buy) == []
    sent = a.submit_order(buy)
    assert sent.status is OrderStatus.SUBMITTED and sent.broker_order_id == "alp-1" and sent.is_simulated is False
    assert fake.requests[-1] == ("POST", f"{PAPER_URL}/v2/orders", {"symbol": "AAPL", "qty": "3", "side": "buy", "type": "market",
                                                                    "client_order_id": "dep1|bar|open", "time_in_force": "day"})
    # A timeout after sending is resolved by asking the broker for the client order id, never by sending again.
    found = a.find_order("dep1|bar|open", "STOCK:AAPL")
    assert found.broker_order_id == "alp-1" and a.find_order("unknown", "STOCK:AAPL") is None
    fake.fill("alp-1", 201.5, qty=1)
    assert a.get_order_status(sent).status is OrderStatus.PARTIALLY_FILLED
    fake.fill("alp-1", 201.5)
    done = a.get_order_status(sent)
    assert (done.status, done.filled_qty, done.avg_fill_price) == (OrderStatus.FILLED, 3, 201.5)

    stop = a.submit_order(Order(id="o2", client_order_id="o2:stop", broker_id="alpaca-paper", instrument_id="STOCK:AAPL", side=Side.SELL, qty=3,
                                type=OrderType.STOP, stop_price=190.0, reduce_only=True))
    assert fake.orders["alp-2"]["type"] == "stop" and fake.orders["alp-2"]["stop_price"] == "190.0" and fake.orders["alp-2"]["time_in_force"] == "gtc"
    moved = a.replace_order(stop, stop_price=195.0)
    assert moved.stop_price == 195.0 and fake.orders["alp-2"]["stop_price"] == "195.0"
    assert [o.broker_order_id for o in a.get_open_orders()] == ["alp-2"]
    assert a.cancel_order(stop).status is OrderStatus.CANCELLED
    assert a.cancel_order(done).status is OrderStatus.FILLED  # cancelling a filled order reports the truth instead of failing

    fake.positions = [{"symbol": "AAPL", "qty": "3", "side": "long", "avg_entry_price": "201.5", "current_price": "203"},
                      {"symbol": "TSLA", "qty": "1", "side": "long", "avg_entry_price": "1", "current_price": "1"}]
    [pos] = a.get_positions()
    assert (pos.instrument_id, pos.qty, pos.avg_price, pos.last_price) == ("STOCK:AAPL", 3, 201.5, 203)

    def order(iid, side=Side.BUY, qty=1.0, **kw):
        return Order(broker_id="alpaca-paper", instrument_id=iid, side=side, qty=qty, **kw)

    assert any("cannot be sold short" in r for r in a.unsupported_reasons(order("ETF:SPY", Side.SELL)))
    assert a.unsupported_reasons(order("ETF:SPY", Side.SELL, reduce_only=True)) == []
    assert any("no symbol mapping" in r for r in a.unsupported_reasons(order("FOREX:EUR/USD", qty=1000)))
    assert any("buying power" in r for r in a.unsupported_reasons(order("STOCK:AAPL", qty=5000, type=OrderType.LIMIT, limit_price=200.0)))
    fake.assets[0]["tradable"] = False
    a.connect()
    assert any("not tradable" in r for r in a.unsupported_reasons(order("STOCK:AAPL")))

    for code, error in ((429, RateLimited), (500, ConnectionLost), (422, OrderRejected), (403, OrderRejected)):
        fake.fail = code
        with pytest.raises(error):
            a.submit_order(order("ETF:SPY", client_order_id=f"x{code}"))
    fake.fail = 429
    try:
        a.broker_account()
    except RateLimited as exc:
        assert exc.retry_after == 2
    with pytest.raises(BrokerError, match="rejected the API key"):
        AlpacaAdapter("x", registry, "WRONG", "SECRET", client=fake.client()).connect()
    with pytest.raises(BrokerError, match="needs an API key"):
        AlpacaAdapter("x", registry, "", "", client=fake.client()).connect()


def test_alpaca_live_endpoint_is_reachable_only_through_the_live_gate():
    registry = InstrumentRegistry()
    assert AlpacaAdapter("x", registry, "k", "s").trading_url == PAPER_URL  # nothing but an authorization selects the live endpoint
    with pytest.raises(LiveTradingNotAuthorized):
        authorize_live_trading(Settings(_env_file=None))
    with pytest.raises(LiveTradingNotAuthorized):
        AlpacaAdapter("x", registry, "k", "s", authorization=object())  # a forged authorization is refused
    enabled = Settings(_env_file=None, live_trading_enabled=True, live_trading_confirmation="I UNDERSTAND THE RISKS OF LIVE TRADING",
                       jwt_secret="x" * 48, environment="production")
    if enabled.live_trading_authorized:
        assert AlpacaAdapter("x", registry, "k", "s", authorization=authorize_live_trading(enabled)).trading_url == LIVE_URL


def test_semi_automatic_flow_through_alpaca_paper_with_a_virtual_eur_ledger(tmp_path):
    from app.security.credentials import store_credentials
    from app.security.vault import CredentialVault

    fake = FakeAlpaca()
    key = CredentialVault.generate_key()
    db = Database(f"sqlite:///{tmp_path}/a.db")
    db.create_all()
    now = [T0]
    rt = TradingRuntime(db, Settings(_env_file=None, credential_key=key), clock=lambda: now[0])
    rt.start()
    rt.now = now
    rt.add_fx_rate("EUR", "USD", 1.08, "manual", "admin", T0 - timedelta(hours=1))
    with db.session() as s:
        s.add(BrokerAccount(id="alpaca-paper", adapter="alpaca", label="Alpaca paper", is_live=False, currency="USD", config={}))
        s.flush()
        store_credentials(s, CredentialVault(key), "alpaca-paper", {"api_key": "PK-PAPER", "api_secret": "SECRET"}, ["trade"], "admin")
    out = rt.connect_broker("alpaca-paper", "admin", http_client=fake.client())
    assert out["connected"] and out["capabilities"]["trading_enabled"]
    status = next(b for b in rt.broker_status() if b["id"] == "alpaca-paper")
    assert status["environment"] == "paper" and status["broker_account"]["equity"] == 100_000 and status["virtual"]["virtual_capital_base"] == 1000
    assert rt.portfolio.equity(T0) == pytest.approx(1000.0)  # the platform trades 1,000 EUR, not the broker's 100,000 USD

    feeder = Feeder(rt.live_hub)
    rt.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.asset_class is AssetClass.STOCK else None)]
    rt.feeder = feeder
    dep = rt.add_deployment("ema_crossover", {"fast": 2, "slow": 5, "allow_short": False}, "STOCK:AAPL", "1m", "alpaca-paper", "admin")
    rt.update_deployment(dep, "admin", active=True)
    rt.live_subscribe(["STOCK:AAPL"], "admin")
    minute, price = 0, 200.0
    for price in [200 * (1 - 0.0005 * k) for k in range(60)] + [200 * (0.97 + 0.002 * k) for k in range(1, 30)]:
        tick(rt, "AAPL", price, minute)
        minute += 1
        if rt.engine.approvals:
            break
    [p] = rt.proposals()
    price = p["entry_price"]
    assert p["is_simulated"] is False and p["broker_id"] == "alpaca-paper" and p["qty"] == int(p["qty"]) >= 1 and p["risk_base"] <= 20.0
    assert not fake.orders  # semi-automatic: nothing reaches Alpaca before the click
    tick(rt, "AAPL", price * 1.0001, minute, 30)
    order = rt.resolve_approval(p["id"], True, "trader@example.com")
    assert order.status is OrderStatus.SUBMITTED and len(fake.orders) == 1
    sent = fake.orders["alp-1"]
    assert (sent["symbol"], sent["side"], sent["type"], float(sent["qty"])) == ("AAPL", "buy", "market", p["qty"])

    fake.fill("alp-1", price * 1.0002)
    fake.positions = [{"symbol": "AAPL", "qty": sent["qty"], "side": "long", "avg_entry_price": str(price * 1.0002), "current_price": str(price * 1.0002)}]
    tick(rt, "AAPL", price * 1.0003, minute + 1)   # the next live minute polls the broker: the fill is booked and the stop placed
    tick(rt, "AAPL", price * 1.0004, minute + 2)
    assert rt.portfolio.qty(("alpaca-paper", "STOCK:AAPL", "ema_crossover")) == p["qty"]
    stops = [o for o in fake.orders.values() if o["type"] == "stop"]
    assert len(stops) == 1 and stops[0]["side"] == "sell" and float(stops[0]["qty"]) == p["qty"] and float(stops[0]["stop_price"]) < price
    assert rt.portfolio.reconcile() == []  # the platform's books agree with the broker's positions

    # Restart: the virtual ledger baseline is restored, not re-created from today's broker equity.
    fake.equity += 30
    again = TradingRuntime(db, Settings(_env_file=None, credential_key=key, live_feed_autostart=False), clock=lambda: now[0])
    again.start()
    again.connect_broker("alpaca-paper", "admin", http_client=fake.client())
    assert again.brokers.get("alpaca-paper").get_account_balance().equity == pytest.approx(1080 + 30)
    rt.live.stop()

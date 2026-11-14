"""Live market data: tick hub, Alpaca stream and polling fallback, Finnhub stream, engine integration, SSE to the browser."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import pytest
from websockets.sync.server import serve

from app.core.config import Settings
from app.database.base import Database
from app.database.settings_store import UserSettings, save_settings
from app.domain.models import AssetClass, Instrument
from app.execution.runtime import MANUAL_STRATEGY, RuntimeError_, TradingRuntime
from app.market_data.live import AlpacaLive, FeedStatus, FinnhubLive, LiveFeed, LiveHub, Tick, wait_for
from app.market_data.quality import DataKind

T0 = datetime(2026, 11, 13, 15, 0, 0, tzinfo=timezone.utc)
BTC = Instrument(id="CRYPTO:BTC/USD", asset_class=AssetClass.CRYPTO, symbol="BTC/USD", base_currency="BTC", quote_currency="USD", calendar="24x7",
                 tick_size=0.01, lot_size=0.00001, min_qty=0.00001)


def tick(price, seconds, kind="trade", size=1.0, received_after=0.2, **kw):
    ts = T0 + timedelta(seconds=seconds)
    return Tick("CRYPTO:BTC/USD", price, ts, ts + timedelta(seconds=received_after), "test", FeedStatus.LIVE, kind, size, **kw)


def test_hub_builds_minute_bars_and_reports_provenance_and_staleness():
    done = []
    now = [T0]
    hub = LiveHub(clock=lambda: now[0], on_bar=lambda iid, frame, provider: done.append((iid, frame, provider)), stale_after=timedelta(seconds=30))
    events = []
    token = hub.subscribe(events.append, {"CRYPTO:BTC/USD"})
    other = []
    hub.subscribe(other.append, {"STOCK:AAPL"})
    for price, sec, size in ((100, 1, 2), (103, 20, 1), (99, 40, 3), (101, 59, 1)):
        hub.publish(tick(price, sec, size=size))
    assert not done and events[-1]["bar"] == {"t": int(T0.timestamp()), "o": 100, "h": 103, "l": 99, "c": 101, "v": 7}
    hub.publish(tick(102, 61, size=5))  # first tick of the next minute completes the previous bar
    iid, frame, provider = done[0]
    assert (iid, provider) == ("CRYPTO:BTC/USD", "test") and frame.index[0] == pd.Timestamp(T0)
    assert frame.iloc[0].tolist() == [100, 103, 99, 101, 7]
    hub.publish(tick(500, 30))  # a late tick for a finished minute is counted, never rewrites history
    assert hub.out_of_order == 1 and len(done) == 1 and events[-1]["bar"]["h"] == 102
    hub.publish(tick(102.5, 65, kind="quote", bid=102.4, ask=102.6))
    assert events[-1]["bar"]["v"] == 5  # quotes move the price but add no volume
    e = events[0]
    assert e["provider"] == "test" and e["status"] == "LIVE" and e["latency_ms"] == 200 and e["ts_source"] < e["ts_received"] and not other

    now[0] = T0 + timedelta(seconds=70)
    q = hub.quote("CRYPTO:BTC/USD")
    assert q["fresh"] and q["age_seconds"] == 5 and q["spread"] == pytest.approx(0.2) and q["status"] == "LIVE"
    now[0] = T0 + timedelta(seconds=200)
    assert hub.quote("CRYPTO:BTC/USD")["fresh"] is False  # an old price is never presented as usable
    delayed = Tick("STOCK:AAPL", 10, now[0], now[0], "slow", FeedStatus.DELAYED)
    hub.publish(delayed)
    assert hub.quote("STOCK:AAPL")["fresh"] is False and hub.quote("STOCK:AAPL")["status"] == "DELAYED"  # delayed data is never live
    assert hub.quote("STOCK:NOPE") is None
    st = hub.status()["providers"]["test"]
    assert st["ticks_total"] == 7 and st["seconds_since_last_tick"] > 100
    hub.unsubscribe(token)
    hub.publish(tick(1, 300))
    assert events[-1]["price"] != 1

    def broken(_):
        raise RuntimeError("browser went away")

    hub.subscribe(broken)
    hub.publish(tick(2, 301))  # a failing subscriber does not stop the feed
    assert hub.latest["CRYPTO:BTC/USD"].price == 2


class FakeAlpacaStream:
    """Speaks Alpaca's market-data WebSocket protocol: connected, auth, subscribe, then trade and quote messages."""

    def __init__(self, accept_auth=True):
        self.accept_auth, self.subscriptions, self.clients, self.auth = accept_auth, [], [], None
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, ws):
        ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
        self.auth = json.loads(ws.recv())
        if not self.accept_auth:
            ws.send(json.dumps([{"T": "error", "code": 402, "msg": "auth failed"}]))
            return
        ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
        self.clients.append(ws)
        try:
            for raw in ws:
                self.subscriptions.append(json.loads(raw))
        except Exception:
            pass

    def send(self, *messages):
        for ws in self.clients:
            ws.send(json.dumps(list(messages)))

    def close(self):
        self.server.shutdown()


def run(source):
    stop = threading.Event()
    threading.Thread(target=source.run, args=(stop,), daemon=True).start()
    return stop


def test_alpaca_stream_delivers_trades_and_quotes_pushed_by_the_server():
    server = FakeAlpacaStream()
    hub = LiveHub()
    source = AlpacaLive(hub, "crypto", "KEY", "SECRET", stream_url=f"ws://127.0.0.1:{server.port}")
    source.set_symbols({"BTC/USD": "CRYPTO:BTC/USD"})
    stop = run(source)
    try:
        assert wait_for(lambda: any(m.get("action") == "subscribe" for m in server.subscriptions))
        assert server.auth == {"action": "auth", "key": "KEY", "secret": "SECRET"}
        assert server.subscriptions[-1] == {"action": "subscribe", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}
        sent = datetime.now(timezone.utc)
        server.send({"T": "t", "S": "BTC/USD", "p": 68000.5, "s": 0.01, "t": sent.isoformat()}, {"T": "t", "S": "DOGE/USD", "p": 1, "s": 1, "t": sent.isoformat()})
        assert wait_for(lambda: "CRYPTO:BTC/USD" in hub.latest)
        t = hub.latest["CRYPTO:BTC/USD"]
        assert (t.price, t.kind, t.size, t.provider, t.status) == (68000.5, "trade", 0.01, "alpaca-crypto", FeedStatus.LIVE)
        assert t.ts_source == sent and 0 <= t.latency_ms < 2000  # pushed, not fetched: it arrives within moments of being sent
        server.send({"T": "q", "S": "BTC/USD", "bp": 68000.0, "ap": 68002.0, "t": datetime.now(timezone.utc).isoformat()})
        assert wait_for(lambda: hub.latest["CRYPTO:BTC/USD"].kind == "quote")
        assert hub.latest["CRYPTO:BTC/USD"].price == 68001.0 and hub.quote("CRYPTO:BTC/USD")["spread"] == 2.0
        st = hub.status()["providers"]["alpaca-crypto"]
        assert st["state"] == "streaming" and st["transport"] == "websocket" and st["ticks_total"] == 2
        source.set_symbols({"BTC/USD": "CRYPTO:BTC/USD", "ETH/USD": "CRYPTO:ETH/USD"})
        assert wait_for(lambda: server.subscriptions[-1].get("trades") == ["BTC/USD", "ETH/USD"])
    finally:
        stop.set()
        server.close()


def alpaca_rest(state):
    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append((request.url.path, dict(request.url.params), request.headers.get("APCA-API-KEY-ID")))
        if state.get("status"):
            return httpx.Response(state["status"], json={"message": "nope"})
        what = "trades" if "trades" in request.url.path else "quotes"
        return httpx.Response(200, json={what: state[what]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_alpaca_falls_back_to_polling_when_streaming_is_impossible_and_says_so():
    state = {"calls": [], "trades": {"BTC/USD": {"p": 100.0, "s": 0.5, "t": "2026-11-13T15:00:01Z"}},
             "quotes": {"BTC/USD": {"bp": 99.0, "ap": 101.0, "t": "2026-11-13T15:00:02Z"}}}
    hub = LiveHub(clock=lambda: T0 + timedelta(seconds=20))
    # The stream URL points at a closed port: the connection fails exactly as it does on a network that blocks WebSockets.
    source = AlpacaLive(hub, "crypto", "KEY", "SECRET", stream_url="ws://127.0.0.1:9", poll_seconds=0.05, client=alpaca_rest(state))
    source.set_symbols({"BTC/USD": "CRYPTO:BTC/USD"})
    stop = run(source)
    try:
        assert wait_for(lambda: hub.status()["providers"].get("alpaca-crypto", {}).get("ticks_total", 0) >= 2)
        st = hub.status()["providers"]["alpaca-crypto"]
        assert st["state"] == "polling" and "REST requests" in st["transport"] and "stream:" in st["last_error"]
        time.sleep(0.3)
        assert hub.status()["providers"]["alpaca-crypto"]["ticks_total"] == 2  # an unchanged trade or quote is not republished as news
        state["trades"] = {"BTC/USD": {"p": 105.0, "s": 0.1, "t": "2026-11-13T15:00:09Z"}}
        assert wait_for(lambda: hub.last_trade["CRYPTO:BTC/USD"].price == 105.0)
        path, params, key = state["calls"][0]
        assert path.startswith("/v1beta3/crypto/us/latest/") and params == {"symbols": "BTC/USD"} and key == "KEY"
        state["trades"] = {"BTC/USD": {"p": 1.0, "s": 0.1, "t": "2026-11-13T14:40:00Z"}}  # the "latest" trade of a thin market, 20 minutes old
        time.sleep(0.3)
        assert hub.last_trade["CRYPTO:BTC/USD"].price == 105.0  # history is not republished as a live event
        state["status"] = 401
        assert wait_for(lambda: hub.status()["providers"]["alpaca-crypto"]["state"] == "error")
    finally:
        stop.set()

    stocks_state = {"calls": [], "trades": {"AAPL": {"p": 200.0, "s": 10, "t": "2026-11-13T15:00:01Z"}}, "quotes": {}}
    hub2 = LiveHub(clock=lambda: T0 + timedelta(seconds=20))
    stocks = AlpacaLive(hub2, "stocks", "KEY", "SECRET", allow_stream=False, poll_seconds=0.05, client=alpaca_rest(stocks_state))
    stocks.set_symbols({"AAPL": "STOCK:AAPL"})
    stop = run(stocks)
    try:
        assert wait_for(lambda: "STOCK:AAPL" in hub2.latest)
        assert hub2.latest["STOCK:AAPL"].venue == "IEX"  # one exchange, labelled as such
        assert any(c[0] == "/v2/stocks/trades/latest" and c[1] == {"symbols": "AAPL", "feed": "iex"} for c in stocks_state["calls"])
    finally:
        stop.set()


def test_finnhub_stream_and_key_never_leaks():
    received = []

    def handle(ws):
        received.append(ws.request.path)
        try:
            for raw in ws:
                received.append(json.loads(raw))
                ws.send(json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": 201.5, "t": int(time.time() * 1000), "v": 100}]}))
        except Exception:
            pass

    server = serve(handle, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hub = LiveHub()
    source = FinnhubLive(hub, "SECRETKEY", stream_url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
    source.set_symbols({"AAPL": "STOCK:AAPL"})
    stop = run(source)
    try:
        assert wait_for(lambda: "STOCK:AAPL" in hub.latest)
        assert hub.latest["STOCK:AAPL"].price == 201.5 and received[0] == "/?token=SECRETKEY" and received[1] == {"type": "subscribe", "symbol": "AAPL"}
    finally:
        stop.set()
        server.shutdown()
    dead = LiveHub()
    broken = FinnhubLive(dead, "SECRETKEY", stream_url="ws://127.0.0.1:9")
    broken.set_symbols({"AAPL": "STOCK:AAPL"})
    stop = run(broken)
    assert wait_for(lambda: dead.status()["providers"].get("finnhub", {}).get("errors", 0) >= 1)
    stop.set()
    assert "SECRETKEY" not in json.dumps(dead.status())


# ---------------------------------------------------------------- inside the platform
class Feeder:
    """A live source the test drives by hand."""

    name = "manual-feed"

    def __init__(self, hub):
        self.hub, self.symbols = hub, {}

    def set_symbols(self, symbols):
        self.symbols = dict(symbols)

    def run(self, stop):
        self.hub.set_state(self.name, "streaming", "test")
        stop.wait()

    def trade(self, symbol, price, ts, size=1.0):
        self.hub.publish(Tick(self.symbols[symbol], price, ts, ts + timedelta(milliseconds=150), self.name, FeedStatus.LIVE, "trade", size))


@pytest.fixture
def live_rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    with db.session() as s:
        save_settings(s, UserSettings(initial_paper_capital=1000, base_currency="EUR"))
    now = [T0]
    rt = TradingRuntime(db, Settings(_env_file=None), clock=lambda: now[0])
    rt.start()
    rt.create_paper_account("paper-main", "Main", "admin")
    rt.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", T0 - timedelta(hours=1))
    rt.add_instruments(["BTC/USD"], "CRYPTO", "crypto", "admin")
    feeder = Feeder(rt.live_hub)
    rt.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.asset_class is AssetClass.CRYPTO else None)]
    rt.now, rt.feeder = now, feeder
    return rt


def test_live_bars_are_stored_charted_and_fill_paper_orders(live_rt):
    from app.charts import datafeed

    rt, feeder = live_rt, live_rt.feeder
    out = rt.live_subscribe(["CRYPTO:BTC/USD", "STOCK:AAPL"], "admin")
    assert out["subscribed"] == {"CRYPTO:BTC/USD": "manual-feed"} and "STOCK:AAPL" in out["unavailable"]  # coverage gaps are stated, not hidden
    with pytest.raises(RuntimeError_, match="Unknown instruments"):
        rt.live_subscribe(["CRYPTO:NOPE/USD"], "admin")
    for minute in range(3):
        for sec, price in ((5, 68000 + minute * 10), (30, 68020 + minute * 10), (55, 68010 + minute * 10)):
            feeder.trade("BTC/USD", price, T0 + timedelta(minutes=minute, seconds=sec), 0.01)
    rt.now[0] = T0 + timedelta(minutes=3)
    with rt.db.session() as s:
        stored = rt.store.load(s, "CRYPTO:BTC/USD", "1m")
        assert stored.kind is DataKind.REALTIME and stored.source == "live:manual-feed" and len(stored.bars) == 2  # the third minute is still forming
        assert stored.bars.iloc[0].tolist() == [68000, 68020, 68000, 68010, pytest.approx(0.03)]
        info = datafeed.resolve(s, rt.registry, "CRYPTO:BTC/USD")
        assert info["data"]["kind"] == "realtime" and "1" in info["supported_resolutions"] and "5" in info["supported_resolutions"]
        assert datafeed.history(s, "CRYPTO:BTC/USD", "1", None, None, countback=10)["c"] == [68010, 68020]
    assert rt.live_price("CRYPTO:BTC/USD")["price"] == 68030 and rt.portfolio.prices["CRYPTO:BTC/USD"] == 68020

    # A paper order placed from the chart fills at the open of the next live minute, and its stop then works on live bars.
    order = rt.chart_order("paper-main", "CRYPTO:BTC/USD", "buy", 0.001, 0.02, "admin")
    assert order["status"] == "submitted"
    feeder.trade("BTC/USD", 68040, T0 + timedelta(minutes=3, seconds=2), 0.01)   # completes minute 2; the order was created after it opened
    key = ("paper-main", "CRYPTO:BTC/USD", MANUAL_STRATEGY)
    assert rt.portfolio.qty(key) == 0
    feeder.trade("BTC/USD", 68050, T0 + timedelta(minutes=4, seconds=2), 0.01)   # completes minute 3, which opened after the order: fill at its open
    assert rt.portfolio.qty(key) == pytest.approx(0.001)
    assert rt.portfolio.slices[key].avg_price == pytest.approx(68040, rel=2e-3)
    feeder.trade("BTC/USD", 60000, T0 + timedelta(minutes=4, seconds=30), 0.01)  # crash through the 2% stop
    feeder.trade("BTC/USD", 60000, T0 + timedelta(minutes=5, seconds=1), 0.01)
    assert rt.portfolio.qty(key) == 0 and rt.portfolio.trades[-1].exit_reason == "stop_loss"
    assert rt.drive_stored()["instruments"] == 0  # live instruments are not also advanced from stored daily bars

    # Subscriptions survive a restart.
    again = TradingRuntime(rt.db, Settings(_env_file=None, live_feed_autostart=False), clock=lambda: rt.now[0])
    again.start()
    with again.db.session() as s:
        from app.database.models import AppSetting
        assert s.get(AppSetting, "live:subscriptions").value == {"instrument_ids": ["CRYPTO:BTC/USD"]}
    rt.live_unsubscribe(["CRYPTO:BTC/USD"], "admin")
    assert rt.live.subscribed == {}
    rt.live.stop()


def test_live_bars_drive_a_deployed_strategy_on_a_higher_timeframe(live_rt):
    rt, feeder = live_rt, live_rt.feeder
    dep = rt.add_deployment("ema_crossover", {"fast": 2, "slow": 4}, "CRYPTO:BTC/USD", "5m", "paper-main", "admin")
    rt.update_deployment(dep, "admin", active=True)
    rt.live_subscribe(["CRYPTO:BTC/USD"], "admin")
    for minute in range(31):
        feeder.trade("BTC/USD", 68000 + 15 * minute, T0 + timedelta(minutes=minute, seconds=10), 0.01)
    with rt.db.session() as s:
        five = rt.store.load(s, "CRYPTO:BTC/USD", "5m")
        ones = rt.store.load(s, "CRYPTO:BTC/USD", "1m")
    assert len(ones.bars) == 30 and len(five.bars) == 6 and five.kind is DataKind.REALTIME
    assert five.bars["open"].iloc[1] == ones.bars["open"].iloc[5] and five.bars["close"].iloc[1] == ones.bars["close"].iloc[9]
    assert five.bars.index[-1] == pd.Timestamp(T0 + timedelta(minutes=25))
    rt.live.stop()


def test_stream_reaches_an_http_client_as_server_sent_events(tmp_path):
    """The whole chain over real HTTP: a tick published on the hub arrives on an open /api/live/stream connection."""
    import socket

    import uvicorn

    from app.main import create_app
    from app.security.roles import Role
    from app.security.users import create_user
    from app.security.vault import CredentialVault

    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    with db.session() as s:
        create_user(s, "admin@example.com", "correct-horse-battery", Role.ADMIN)
    app = create_app(Settings(_env_file=None, database_url="sqlite://", jwt_secret="t" * 48, credential_key=CredentialVault.generate_key()), db)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    assert wait_for(lambda: server.started, 20)
    try:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=20, trust_env=False) as c:
            assert c.get("/api/live/stream").status_code == 401
            token = c.post("/api/auth/login", json={"email": "admin@example.com", "password": "correct-horse-battery"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            hub = app.state.container.runtime.live_hub
            got = []

            def publish():
                wait_for(lambda: hub.status()["subscribers"] == 1, 10)
                for i in range(3):
                    now = datetime.now(timezone.utc)
                    hub.publish(Tick("CRYPTO:BTC/USD", 68000.0 + i, now, now, "test", FeedStatus.LIVE, "trade", 0.01))
                    hub.publish(Tick("CRYPTO:ETH/USD", 1.0, now, now, "test", FeedStatus.LIVE))  # not requested: must not arrive
                    time.sleep(0.05)

            threading.Thread(target=publish, daemon=True).start()
            with c.stream("GET", "/api/live/stream?symbols=CRYPTO:BTC/USD", headers=headers) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        got.append((json.loads(line[6:]), datetime.now(timezone.utc)))
                    if len(got) == 3:
                        break
            assert [e["price"] for e, _ in got] == [68000.0, 68001.0, 68002.0] and {e["instrument_id"] for e, _ in got} == {"CRYPTO:BTC/USD"}
            for event, arrived in got:  # pushed to the client as it happens: well under a second from source to client
                assert (arrived - datetime.fromisoformat(event["ts_source"])).total_seconds() < 1.0
            status = c.get("/api/live/status", headers=headers).json()
            assert status["providers"]["test"]["ticks_total"] == 6 and "IEX" in status["note"]
            assert wait_for(lambda: hub.status()["subscribers"] == 0, 15)  # the connection is cleaned up when the client leaves
    finally:
        server.should_exit = True

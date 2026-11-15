"""Default installation: Alpaca is the single data source. The other connectors exist but are switched off and hidden."""
import httpx
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.database.models import BrokerAccount, Security
from app.execution.runtime import RuntimeError_, TradingRuntime
from app.global_markets.discovery import AlpacaDiscovery, ProviderAuthError
from app.global_markets.exchanges import EXCHANGES, exchanges_for
from app.domain.models import AssetClass, Instrument
from app.instruments.registry import default_instruments
from app.security.vault import CredentialVault

ASSETS = [
    {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "tradable": True, "status": "active"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "exchange": "ARCA", "tradable": True},
    {"symbol": "BAC.PRL", "name": "Bank of America 7.25% Non-Cumulative Preferred Stock", "exchange": "NYSE", "tradable": True},
    {"symbol": "XYZW", "name": "XYZ Acquisition Corp Warrants", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "BABA", "name": "Alibaba Group Holding Limited American Depositary Shares", "exchange": "NYSE", "tradable": True},
    {"symbol": "OTCX", "name": "Some OTC Company", "exchange": "OTC", "tradable": True},        # over the counter: not listed
    {"symbol": "DEAD", "name": "Halted Corp", "exchange": "NYSE", "tradable": False},           # cannot be traded
]


def fake_get(calls):
    def get(url, **kw):
        calls.append((url, kw.get("params"), kw.get("headers")))
        if kw["headers"]["APCA-API-KEY-ID"] != "PK":
            return httpx.Response(401, json={"message": "unauthorized"})
        return httpx.Response(200, json=ASSETS)

    return get


def test_alpaca_listing_keeps_only_tradable_listed_assets_and_says_how_types_were_inferred():
    calls = []
    us = EXCHANGES["US"]
    records = AlpacaDiscovery("PK", "SK", http_get=fake_get(calls)).list_securities(us)
    assert [(r.ticker, r.security_type, r.mic) for r in records] == [
        ("AAPL", "common_stock", "XNAS"), ("SPY", "etf", "ARCX"), ("BAC.PRL", "preferred_stock", "XNYS"), ("XYZW", "other", "XNAS"), ("BABA", "adr", "XNYS")]
    assert all(r.isin is None and r.figi is None and r.currency == "USD" and "inferred" in r.raw_type for r in records)  # nothing Alpaca does not publish is invented
    url, params, headers = calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/assets" and params == {"status": "active", "asset_class": "us_equity"} and headers["APCA-API-SECRET-KEY"] == "SK"
    assert [e.code for e in exchanges_for("alpaca")] == ["US"]
    with pytest.raises(Exception, match="US instruments only"):
        AlpacaDiscovery("PK", "SK", http_get=fake_get(calls)).list_securities(EXCHANGES["MI"])
    with pytest.raises(ProviderAuthError):
        AlpacaDiscovery("WRONG", "SK", http_get=fake_get(calls)).list_securities(us)
    with pytest.raises(ProviderAuthError):
        AlpacaDiscovery("PK", "")


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    key = CredentialVault.generate_key()
    runtime = TradingRuntime(db, Settings(_env_file=None, credential_key=key, enabled_providers="alpaca"))
    runtime.start()
    runtime.vault_key = key
    return runtime


def test_only_alpaca_is_offered_and_the_other_connectors_refuse_to_run(rt):
    assert Settings(_env_file=None, enabled_providers="alpaca").provider_enabled("alpaca")
    assert Settings.model_fields["enabled_providers"].default == "alpaca"  # what a new installation gets
    assert [p["name"] for p in rt.provider_status()] == ["alpaca"]
    assert [p["name"] for p in rt.global_markets.providers()] == ["alpaca"]  # no Finnhub, Alpha Vantage, EODHD or OpenFIGI
    for name in ("tiingo", "alphavantage", "eodhd"):
        with pytest.raises(RuntimeError_, match="switched off"):
            rt._provider(name)
    with pytest.raises(ValueError, match="switched off"):
        rt.global_markets.start_sync("finnhub", "admin")
    everything = TradingRuntime(rt.db, Settings(_env_file=None, credential_key=rt.vault_key, enabled_providers="all"))
    assert len(everything.provider_status()) == 4 and len(everything.global_markets.providers()) == 5  # switched back on by one setting
    assert [s for s in rt.configure_live()] == ["alpaca-crypto"]  # crypto needs no key; stocks join once a key exists


def test_one_alpaca_key_serves_listing_history_and_live_data(rt):
    from app.security.credentials import store_credentials

    assert rt._provider_credentials("alpaca") is None and rt.provider_status()[0]["configured"] is False
    with rt.db.session() as s:
        s.add(BrokerAccount(id="alpaca-paper", adapter="alpaca", label="Alpaca paper", is_live=False, currency="USD", config={}))
        s.flush()
        store_credentials(s, CredentialVault(rt.vault_key), "alpaca-paper", {"api_key": "PK", "api_secret": "SK"}, ["trade"], "admin")
    creds, source = rt._provider_credentials("alpaca")
    assert creds == {"api_key": "PK", "api_secret": "SK"} and source == "broker account alpaca-paper"  # no second key to enter
    status = rt.provider_status()[0]
    assert status["configured"] and status["source"] == "broker account alpaca-paper" and status["key_hint"] == "…PK"
    assert rt.configure_live() == ["alpaca-crypto", "alpaca-stocks"]

    calls = []
    rt.global_markets.discovery_factory = lambda name, key: AlpacaDiscovery(key, "SK", http_get=fake_get(calls))
    rt.global_markets.figi_factory = lambda key: pytest.fail("OpenFIGI must not be contacted when it is switched off")
    rt.global_markets.start_sync("alpaca", "admin")
    from app.market_data.live import wait_for

    assert wait_for(lambda: rt.global_markets.job.snapshot()["status"] not in ("pending", "running"), 30)
    snap = rt.global_markets.job.snapshot()
    assert snap["status"] == "finished", snap
    with rt.db.session() as s:
        rows = {r.ticker: r for r in s.query(Security)}
    assert set(rows) == {"AAPL", "SPY", "BAC.PRL", "XYZW", "BABA"} and rows["SPY"].security_type == "etf" and rows["AAPL"].sources["alpaca"]["symbol"] == "AAPL"


# ---------------------------------------------------------------- Alpaca as the price and listing provider
from datetime import date  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.market_data.providers import AlpacaProvider, ProviderError  # noqa: E402

CRYPTO_ASSETS = [{"symbol": "BTC/USD", "name": "Bitcoin", "tradable": True}, {"symbol": "ETH/USD", "name": "Ethereum", "tradable": True},
                 {"symbol": "BTC/USDT", "name": "Bitcoin / Tether", "tradable": True}, {"symbol": "XRP/USD", "name": "Ripple", "tradable": False}]


def alpaca_http(calls):
    def bars(symbols, n=400, crypto=False):
        idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=n, freq="1D") if crypto else pd.bdate_range(end=pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1), periods=n)
        return {s: [{"t": (t if crypto else t.tz_convert("America/New_York").normalize() if t.tzinfo else t).isoformat().replace("+00:00", "Z"),
                     "o": 100 + k * 0.1, "h": 101 + k * 0.1, "l": 99 + k * 0.1, "c": 100.5 + k * 0.1, "v": 1000 + k} for k, t in enumerate(idx)] for s in symbols}

    def get(url, params=None, timeout=None, headers=None):
        calls.append((url, dict(params or {})))
        if headers.get("APCA-API-KEY-ID") != "PK":
            return SimpleNamespace(status_code=401, json=lambda: {"message": "unauthorized"})
        if url.endswith("/v2/assets"):
            body = CRYPTO_ASSETS if params["asset_class"] == "crypto" else ASSETS
        elif "/crypto/us/bars" in url:
            body = {"bars": bars(params["symbols"].split(","), crypto=True), "next_page_token": None}
        elif url.endswith("/v2/stocks/bars"):
            body = {"bars": bars([s for s in params["symbols"].split(",") if s != "XYZW"]), "next_page_token": None}
        else:
            return SimpleNamespace(status_code=404, json=lambda: {})
        return SimpleNamespace(status_code=200, json=lambda: body)

    return get


import pandas as pd  # noqa: E402


def test_alpaca_lists_stocks_etfs_and_crypto_and_states_that_it_has_no_forex():
    calls = []
    p = AlpacaProvider("PK", "SK", http_get=alpaca_http(calls))
    us = p.list_symbols("US")
    assert [(r["ticker"], r["type"]) for r in us] == [("AAPL", "stock"), ("SPY", "etf"), ("BABA", "stock")]   # preferred shares, warrants, OTC and untradable assets are left out
    assert [r["ticker"] for r in p.list_symbols("CRYPTO")] == ["BTC/USD", "ETH/USD"]                          # USD pairs that can be traded
    assert calls[0] == ("https://paper-api.alpaca.markets/v2/assets", {"status": "active", "asset_class": "us_equity"})
    assert p.markets == p.listing_markets == {"US", "CRYPTO"} and "No forex" in p.pricing
    with pytest.raises(ProviderError, match="no symbol list"):
        p.list_symbols("FX")
    with pytest.raises(Exception, match="refused the request"):
        AlpacaProvider("WRONG", "SK", http_get=alpaca_http(calls)).list_symbols("US")


def test_importing_and_downloading_from_alpaca_covers_stocks_etfs_and_crypto(rt):
    calls = []
    rt.set_provider_key("alpaca", "PK", "admin", "SK")
    rt.provider_factory = lambda name, key: AlpacaProvider(key, "SK", http_get=alpaca_http(calls))
    rt.download_options = {"sleep": lambda s: None, "min_interval": 0}
    rt.auto_metrics = False
    status = rt.provider_status()[0]
    assert status["markets"] == ["CRYPTO", "US"] and status["listing_markets"] == ["CRYPTO", "US"]

    stocks_only = rt.import_symbols("alpaca", "US", ["stock"], "admin")
    assert stocks_only["created"] == 1 and "ETF:SPY" in {i.id for i in rt.registry.all()}  # AAPL and SPY ship with the platform; BABA is new
    both = rt.import_symbols("alpaca", "US", ["stock", "etf"], "admin")
    assert both["listed"] == 3 and both["existing"] == 3
    crypto = rt.import_symbols("alpaca", "CRYPTO", ["stock"], "admin")
    assert crypto["created"] == 2 and {"CRYPTO:BTC/USD", "CRYPTO:ETH/USD"} <= {i.id for i in rt.registry.all()}

    rt.start_download("alpaca", "admin")            # every market Alpaca covers, in one go
    assert rt.download.completed.wait(60)
    snap = rt.download.snapshot()
    assert snap["status"] == "finished" and snap["failed_count"] == 0, snap
    with rt.db.session() as s:
        covered = {c["instrument_id"]: c for c in rt.store.coverage(s) if c["timeframe"] == "1d"}
        today = pd.Timestamp.now(tz="UTC").normalize()
        assert {"STOCK:AAPL", "STOCK:BABA", "ETF:SPY", "CRYPTO:BTC/USD", "CRYPTO:ETH/USD"} <= set(covered)
        assert "FOREX:EUR/USD" not in covered and "CRYPTO:BTC/USDT" not in covered   # Alpaca serves neither: not downloaded, not invented
        btc = rt.store.load(s, "CRYPTO:BTC/USD", "1d")
        assert btc.source == "alpaca-crypto" and btc.bars.index[-1] < today             # the forming day is left out
        assert rt.store.load(s, "ETF:SPY", "1d").source == "alpaca"
    assert any("/v1beta3/crypto/us/bars" in u and p["symbols"] in ("BTC/USD,ETH/USD", "ETH/USD,BTC/USD") for u, p in calls)

    rt.start_download("alpaca", "admin", market="CRYPTO", only_missing=False)   # one market only
    assert rt.download.completed.wait(60) and rt.download.snapshot()["total"] == 2
    with pytest.raises(RuntimeError_, match="does not cover"):
        rt.start_download("alpaca", "admin", instrument_ids=["FOREX:EUR/USD"])


# ---------------------------------------------------------------- refused keys, rate limits and unreachable accounts
def scripted(responses, calls):
    """An HTTP stub answering from a list of (status, body), recording the key each request carried."""
    def get(url, params=None, timeout=None, headers=None):
        calls.append((url, dict(params or {}), (headers or {}).get("APCA-API-KEY-ID")))
        status, body = responses.pop(0) if responses else (200, {"bars": {}, "next_page_token": None})
        return SimpleNamespace(status_code=status, json=lambda: body)

    return get


def test_a_refused_key_is_replaced_by_another_stored_key_and_crypto_needs_none():
    calls = []
    bar = {"t": "2026-11-10T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}
    ok_stock = (200, {"bars": {"AAPL": [bar]}, "next_page_token": None})
    p = AlpacaProvider("OLD1", "S", http_get=scripted([(401, {"message": "unauthorized."}), ok_stock], calls), source="Markets data-provider key",
                       alternatives=[("NEW2", "S2", "broker account alpaca-paper")])
    aapl = next(i for i in default_instruments() if i.id == "STOCK:AAPL")
    assert len(p.daily_bars(aapl).bars) == 1
    assert [c[2] for c in calls] == ["OLD1", "NEW2"] and p.source == "broker account alpaca-paper"
    assert "refused the key ...OLD1" in p.notes[0] and "...NEW2 (broker account alpaca-paper)" in p.notes[0]

    calls.clear()
    btc = Instrument(id="CRYPTO:BTC/USD", asset_class=AssetClass.CRYPTO, symbol="BTC/USD", base_currency="BTC", quote_currency="USD", calendar="24x7",
                     tick_size=0.01, lot_size=0.00001, min_qty=0.00001)
    crypto_ok = (200, {"bars": {"BTC/USD": [{"t": "2026-11-10T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]}, "next_page_token": None})
    p = AlpacaProvider("BAD", "S", http_get=scripted([(403, {"message": "forbidden."}), crypto_ok], calls))
    assert len(p.daily_bars(btc).bars) == 1 and [c[2] for c in calls] == ["BAD", None]   # second request without any key
    assert "without a key" in p.notes[0]

    calls.clear()
    p = AlpacaProvider("K", "S", http_get=scripted([(403, {"message": "subscription does not permit querying recent SIP data"}), ok_stock], calls))
    ds = p.daily_bars(aapl)
    assert [c[1]["feed"] for c in calls] == ["sip", "iex"] and ds.source == "alpaca-iex" and "IEX" in p.notes[0]

    p = AlpacaProvider("ZZZZ", "S", http_get=scripted([(401, {"message": "unauthorized."})], []), source="broker account alpaca-paper")
    with pytest.raises(ProviderAuthError) as err:
        p.daily_bars(aapl)
    text = str(err.value)
    assert "HTTP 401: unauthorized." in text and "...ZZZZ (broker account alpaca-paper)" in text and "regenerated" in text


def test_every_stored_alpaca_key_is_offered_newest_place_first_without_duplicates(rt):
    from app.security.credentials import store_credentials

    rt.set_provider_key("alpaca", "OLDKEY", "admin", "OLDSECRET")
    with rt.db.session() as s:
        for account, key in (("alpaca-paper", "NEWKEY"), ("alpaca-dup", "OLDKEY")):
            s.add(BrokerAccount(id=account, adapter="alpaca", label=account, is_live=False, currency="USD", config={}))
            s.flush()
            store_credentials(s, CredentialVault(rt.vault_key), account, {"api_key": key, "api_secret": "x"}, ["trade"], "admin")
    assert [(c["api_key"], src) for c, src in rt.alpaca_credentials()] == [("OLDKEY", "Markets data-provider key"), ("NEWKEY", "broker account alpaca-paper")]
    provider = rt._provider("alpaca")
    assert provider.source == "Markets data-provider key" and [a[0] for a in provider._alternatives] == ["NEWKEY"]


def test_the_alpaca_account_is_read_once_per_interval_and_its_last_figures_survive_an_outage():
    from tests.integration.test_semi_auto import FakeAlpaca, adapter

    fake = FakeAlpaca()
    a, _ = adapter(fake, virtual_capital=1000.0, capital_fx=1.08)
    a.connect()
    fake.requests.clear()
    for _ in range(20):
        a.get_account_balance()
        a.get_positions()
    assert sum(1 for m, u, _ in fake.requests if u.endswith("/v2/account")) == 1 and sum(1 for m, u, _ in fake.requests if u.endswith("/v2/positions")) == 1
    a.invalidate()
    fake.fail = 429  # rate limited
    bal = a.get_account_balance()
    assert bal.equity == pytest.approx(1080.0) and a.stale and "rate limit" in a.last_error   # last good figures, flagged
    a.invalidate()
    assert a.get_account_balance().equity == pytest.approx(1080.0) and not a.stale             # recovers by itself
    fresh, _ = adapter(fake, virtual_capital=1000.0, capital_fx=1.08)
    fake.fail = 500
    with pytest.raises(Exception):
        fresh.broker_account()   # nothing known yet: an outage is reported, not hidden


def test_pages_stay_up_and_no_new_risk_is_taken_while_a_broker_cannot_be_read(client):
    from datetime import datetime, timezone

    from app.brokers.base import BrokerError
    from app.domain.models import Order, Side
    from app.risk.models import OrderIntent
    from tests.conftest import login
    from tests.execution.fakes import FakeCcxt
    from app.brokers.ccxt_adapter import CcxtAdapter

    admin = login(client)
    rt = client.app.state.container.runtime
    client.post("/api/paper/accounts", headers=admin, json={"id": "paper-main", "label": "Main"})
    broken = CcxtAdapter("broken", rt.registry, FakeCcxt())
    broken.connect()

    def unreadable():
        raise BrokerError("Alpaca rejected the API key for this endpoint")

    broken.get_account_balance = unreadable
    rt.brokers.register(broken)
    peak_before = rt.portfolio.peak_equity
    r = client.get("/api/overview", headers=admin)
    assert r.status_code == 200, r.text
    assert any(b["broker_id"] == "paper-main" for b in r.json()["brokers"]) and "broken" in rt.portfolio.valuation_problems
    assert client.get("/api/quant/dashboard", headers=admin).status_code == 200
    ts = datetime.now(timezone.utc)
    state = rt.portfolio.revalue(ts)
    assert rt.risk.monitor(state) == [] and not rt.risk.breakers and rt.portfolio.peak_equity == peak_before   # an unreadable account is not a loss
    inst = rt.registry.get("STOCK:AAPL")
    decision = rt.risk.evaluate(OrderIntent(Order(broker_id="paper-main", instrument_id=inst.id, side=Side.BUY, qty=1), inst, 100.0, 1.0, 5.0), state)
    assert not decision.approved and "cannot be read right now" in decision.reasons[0]
    rt.brokers.remove("broken")
    rt.portfolio.equity(ts)
    assert rt.portfolio.valuation_problems == {}


def test_an_unexpected_server_error_reaches_the_browser_as_readable_json_with_cors(client):
    from tests.conftest import login

    admin = login(client)
    rt = client.app.state.container.runtime

    def boom():
        raise ValueError("something unexpected")

    rt.overview = boom
    r = client.get("/api/overview", headers={**admin, "Origin": "http://localhost:3000"})
    assert r.status_code == 500 and r.json()["detail"] == "Server error (ValueError): something unexpected"
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

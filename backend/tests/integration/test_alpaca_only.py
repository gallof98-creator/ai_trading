"""Default installation: Alpaca is the single data source. The other connectors exist but are switched off and hidden."""
import httpx
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.database.models import BrokerAccount, Security
from app.execution.runtime import RuntimeError_, TradingRuntime
from app.global_markets.discovery import AlpacaDiscovery, ProviderAuthError
from app.global_markets.exchanges import EXCHANGES, exchanges_for
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

"""Global security catalog: discovery, de-duplication, history, coverage. No test contacts a real service;
OpenFIGI and Alpha Vantage payloads are copies of live responses, Finnhub's follows its documentation."""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database.base import Database
from app.database.models import Security, SecurityEvent, SyncRun
from app.database.settings_store import UserSettings
from app.global_markets import catalog
from app.global_markets.discovery import (AlphaVantageDiscovery, EodhdDiscovery, FinnhubDiscovery, ListingRecord, OpenFigi, classify)
from app.global_markets.exchanges import EXCHANGES, exchanges_for, get_exchange
from app.market_data.providers import ProviderAuthError, ProviderError, ProviderRateLimited
from tests.conftest import VIEWER, login

T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
MI, US = EXCHANGES["MI"], EXCHANGES["US"]


def rec(ticker, name="", kind="common_stock", isin=None, figi=None, raw="Common Stock", currency="EUR", mic="XMIL", scf=None):
    return ListingRecord(ticker=ticker, name=name or ticker, security_type=kind, raw_type=raw, currency=currency, provider_symbol=f"{ticker}.X", mic=mic,
                         isin=isin, figi=figi, share_class_figi=scf)


@pytest.fixture
def session():
    db = Database("sqlite://")
    db.create_all()
    with db.session() as s:
        yield s


def events(session, security):
    return [(e.kind, e.old, e.new, e.source) for e in session.scalars(select(SecurityEvent).where(SecurityEvent.security_id == security.id).order_by(SecurityEvent.id))]


def by_ticker(session, ticker, exchange="MI"):
    return session.scalar(select(Security).where(Security.ticker == ticker, Security.exchange == exchange))


# ---------------------------------------------------------------- registry and classification
def test_exchange_registry_covers_the_requested_markets_without_claiming_provider_support():
    names = " | ".join(e.name for e in EXCHANGES.values())
    for wanted in ("NYSE", "Nasdaq", "Toronto", "TSX Venture", "London", "Xetra", "Frankfurt", "Paris", "Amsterdam", "Brussels", "Milan", "SIX Swiss",
                   "Stockholm", "Helsinki", "Copenhagen", "Madrid", "Tokyo", "Hong Kong", "Shanghai", "Shenzhen", "National Stock Exchange of India",
                   "Bombay", "Singapore", "Korea", "Australian", "New Zealand"):
        assert wanted in names, wanted
    assert len({e.mic for e in EXCHANGES.values()}) == len(EXCHANGES)  # one primary MIC each
    assert [e.code for e in exchanges_for("alphavantage")] == ["US"]  # a provider is only offered where it has a code
    assert len(exchanges_for("finnhub")) > 35 and "MI" in [e.code for e in exchanges_for("eodhd")]
    with pytest.raises(ValueError):
        get_exchange("ZZ")


def test_instrument_type_classification():
    cases = {"Common Stock": "common_stock", "Preference": "preferred_stock", "Preferred Stock": "preferred_stock", "ETP": "etf", "ETF": "etf", "REIT": "reit",
             "ADR": "adr", "GDR": "depositary_receipt", "NVDR": "depositary_receipt", "Closed-End Fund": "closed_end_fund", "Warrant": "other",
             "Unit": "other", "Right": "other", "": "other", "Something New": "other"}
    for raw, expected in cases.items():
        assert classify(raw) == expected, raw


# ---------------------------------------------------------------- synchronisation rules
def test_first_sync_catalogs_listings_with_identifiers_and_history(session):
    out = catalog.sync_exchange(session, "finnhub", MI, [rec("ENI", "Eni SpA", isin="IT0003132476", figi="BBG000FVRV79"), rec("ENEL", "Enel SpA", isin="IT0003128367"),
                                                         rec("ENI", "Eni duplicate row", isin="IT0003132476", figi="BBG000FVRV79")], T0)
    assert (out["received"], out["new"], out["duplicates_in_feed"]) == (3, 2, 1)
    eni = by_ticker(session, "ENI")
    assert (eni.isin, eni.figi, eni.exchange, eni.mic, eni.country, eni.currency, eni.listing_status) == ("IT0003132476", "BBG000FVRV79", "MI", "XMIL", "IT", "EUR", "active")
    assert eni.sources == {"finnhub": {"symbol": "ENI.X", "synced_at": T0.isoformat()}} and events(session, eni) == [("listed", None, "ENI", "finnhub")]
    again = catalog.sync_exchange(session, "finnhub", MI, [rec("ENI", "Eni SpA", isin="IT0003132476", figi="BBG000FVRV79"), rec("ENEL", "Enel SpA", isin="IT0003128367")], T0 + timedelta(days=1))
    assert (again["new"], again["unchanged"], again["updated"]) == (0, 2, 0)  # re-running changes nothing
    assert session.scalar(select(Security.id).where(Security.ticker == "ENI").order_by(Security.id.desc())) == eni.id
    assert len(list(session.scalars(select(SyncRun)))) == 2


def test_ticker_change_is_followed_by_identifier_not_treated_as_a_new_company(session):
    catalog.sync_exchange(session, "finnhub", MI, [rec("FCA", "Fiat Chrysler", isin="NL0010877643", figi="BBG00DSV2R39")], T0)
    out = catalog.sync_exchange(session, "finnhub", MI, [rec("STLAM", "Stellantis NV", isin="NL0010877643", figi="BBG00DSV2R39")], T0 + timedelta(days=30))
    assert (out["new"], out["ticker_changes"], out["delisted"]) == (0, 1, 0)
    rows = list(session.scalars(select(Security)))
    assert len(rows) == 1 and rows[0].ticker == "STLAM" and rows[0].name == "Stellantis NV"
    assert events(session, rows[0])[1:] == [("ticker_changed", "FCA", "STLAM", "finnhub"), ("name_changed", "Fiat Chrysler", "Stellantis NV", "finnhub")]


def test_reused_ticker_is_a_different_security(session):
    catalog.sync_exchange(session, "finnhub", MI, [rec("ABC", "Old Company", isin="IT0000000001"), rec("KEEP", isin="IT0000000009")], T0)
    out = catalog.sync_exchange(session, "finnhub", MI, [rec("ABC", "New Company", isin="IT0000000002"), rec("KEEP", isin="IT0000000009")], T0 + timedelta(days=400))
    assert (out["new"], out["delisted"], out["ticker_changes"]) == (1, 1, 0)
    rows = {s.isin: s for s in session.scalars(select(Security).where(Security.ticker == "ABC"))}
    assert rows["IT0000000001"].listing_status == "delisted" and rows["IT0000000002"].listing_status == "active"
    assert rows["IT0000000001"].id != rows["IT0000000002"].id


def test_delisting_relisting_and_protection_against_truncated_answers(session):
    full = [rec(f"T{i:02d}", isin=f"IT00000001{i:02d}") for i in range(20)]
    catalog.sync_exchange(session, "finnhub", MI, full, T0)
    out = catalog.sync_exchange(session, "finnhub", MI, full[:19], T0 + timedelta(days=1))
    gone = by_ticker(session, "T19")
    assert out["delisted"] == 1 and gone.listing_status == "delisted" and gone.sources["finnhub"]["missing_since"]
    assert events(session, gone)[-1] == ("delisted", "T19", None, "finnhub")
    truncated = catalog.sync_exchange(session, "finnhub", MI, full[:3], T0 + timedelta(days=2))  # e.g. a provider outage
    assert truncated["delisted"] == 0 and "incomplete answer" in truncated["delist_check"]
    assert session.scalar(select(Security).where(Security.listing_status == "delisted", Security.ticker != "T19")) is None
    back = catalog.sync_exchange(session, "finnhub", MI, full, T0 + timedelta(days=3))
    assert back["relisted"] == 1 and by_ticker(session, "T19").listing_status == "active"
    assert "missing_since" not in by_ticker(session, "T19").sources["finnhub"]


def test_two_providers_merge_into_one_security_and_fill_each_others_gaps(session):
    catalog.sync_exchange(session, "finnhub", MI, [rec("ENI", "ENI SPA", figi="BBG000FVRV79", scf="BBG001S7QGH9"), rec("RACE", "FERRARI", figi="BBG00BS9K4L1", kind="other", raw="")], T0)
    out = catalog.sync_exchange(session, "eodhd", MI, [rec("ENI", "Eni SpA", isin="IT0003132476"), rec("RACE", "Ferrari NV", isin="NL0011585146"), rec("ISP", "Intesa", isin="IT0000072618")], T0)
    assert (out["new"], out["updated"]) == (1, 2)
    eni = by_ticker(session, "ENI")
    assert len(list(session.scalars(select(Security)))) == 3  # no duplicates
    assert (eni.isin, eni.figi) == ("IT0003132476", "BBG000FVRV79") and sorted(eni.sources) == ["eodhd", "finnhub"]
    assert ("identifier_added", None, "isin=IT0003132476", "eodhd") in events(session, eni)
    assert by_ticker(session, "RACE").security_type == "common_stock"  # the more specific provider refines an unknown type
    # Dropped by one provider but still listed by the other: not delisted.
    catalog.sync_exchange(session, "finnhub", MI, [rec("RACE", "FERRARI", figi="BBG00BS9K4L1")], T0 + timedelta(days=1))
    assert by_ticker(session, "ENI").listing_status == "active" and by_ticker(session, "ENI").sources["finnhub"]["missing_since"]
    catalog.sync_exchange(session, "eodhd", MI, [rec("RACE", "Ferrari NV", isin="NL0011585146"), rec("ISP", "Intesa", isin="IT0000072618")], T0 + timedelta(days=2))
    assert by_ticker(session, "ENI").listing_status == "delisted"  # now nobody lists it


def test_same_ticker_on_two_exchanges_stays_two_listings_linked_by_identifier(session):
    catalog.sync_exchange(session, "finnhub", MI, [rec("STLAM", "Stellantis", isin="NL00150001Q9", scf="BBG00Z1Z3X51")], T0)
    catalog.sync_exchange(session, "finnhub", EXCHANGES["PA"], [rec("STLAP", "Stellantis", isin="NL00150001Q9", scf="BBG00Z1Z3X51", mic="XPAR")], T0)
    milan = by_ticker(session, "STLAM")
    prof = catalog.profile(session, milan.id)
    assert [o["exchange"] for o in prof["other_listings"]] == ["PA"] and prof["history"][0]["kind"] == "listed"
    assert catalog.profile(session, 999999) is None


# ---------------------------------------------------------------- provider parsing
def _resp(status=200, body=None, text=None):
    return SimpleNamespace(status_code=status, json=lambda: body, text=text if text is not None else json.dumps(body))


def test_finnhub_parsing_and_errors_follow_the_documented_format():
    seen = {}
    rows = [{"currency": "EUR", "description": "ENI SPA", "displaySymbol": "ENI.MI", "figi": "BBG000FVRV79", "isin": None, "mic": "XMIL",
             "shareClassFIGI": "BBG001S7QGH9", "symbol": "ENI.MI", "symbol2": "", "type": "Common Stock"},
            {"currency": "EUR", "description": "ISHARES CORE", "displaySymbol": "SWDA.MI", "figi": "BBG000XXXXX1", "mic": "ETFP", "symbol": "SWDA.MI", "type": "ETP"},
            {"description": "no symbol", "type": "Common Stock"}]

    def get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return _resp(seen.get("code", 200), seen.get("body", rows))

    p = FinnhubDiscovery("KEY-1234", http_get=get)
    out = p.list_securities(MI)
    assert seen["url"] == "https://finnhub.io/api/v1/stock/symbol" and seen["params"] == {"exchange": "MI"} and seen["headers"] == {"X-Finnhub-Token": "KEY-1234"}
    assert [(r.ticker, r.security_type, r.figi, r.share_class_figi, r.mic, r.provider_symbol) for r in out] == \
        [("ENI", "common_stock", "BBG000FVRV79", "BBG001S7QGH9", "XMIL", "ENI.MI"), ("SWDA", "etf", "BBG000XXXXX1", None, "ETFP", "SWDA.MI")]
    for code, exc in ((401, ProviderAuthError), (403, ProviderAuthError), (429, ProviderRateLimited), (500, ProviderError)):
        seen["code"] = code
        with pytest.raises(exc) as info:
            p.list_securities(MI)
        assert "KEY-1234" not in str(info.value)
    seen.update(code=200, body={"error": "x"})
    with pytest.raises(ProviderError):
        p.list_securities(MI)
    with pytest.raises(ProviderAuthError):
        FinnhubDiscovery("")


AV_LISTING = """symbol,name,exchange,assetType,ipoDate,delistingDate,status
A,Agilent Technologies Inc,NYSE,Stock,1999-11-18,null,Active
BRK-B,Berkshire Hathaway Inc - Class B,NYSE,Stock,1996-05-09,null,Active
ABR-P-D,Arbor Realty Trust Inc,NYSE,Stock,2021-06-02,null,Active
AACTWS,Ares Acquisition Corp II Warrant,NYSE,Stock,2023-06-12,null,Active
SPY,SPDR S&P 500 ETF Trust,NYSE ARCA,ETF,1993-01-29,null,Active
QQQ,Invesco QQQ,NASDAQ,ETF,1999-03-10,null,Active
OLD,Gone Corp,NYSE,Stock,1990-01-01,2020-01-01,Delisted
"""


def test_alpha_vantage_listing_keeps_every_instrument_but_separates_types_and_venues():
    p = AlphaVantageDiscovery("k", http_get=lambda url, params=None, timeout=None: _resp(text=AV_LISTING))
    out = {r.ticker: r for r in p.list_securities(US)}
    assert {t: r.security_type for t, r in out.items()} == {"A": "common_stock", "BRK.B": "common_stock", "ABR-P-D": "preferred_stock", "AACTWS": "other",
                                                             "SPY": "etf", "QQQ": "etf"}
    assert (out["SPY"].mic, out["QQQ"].mic, out["A"].mic, out["BRK.B"].provider_symbol) == ("ARCX", "XNAS", "XNYS", "BRK-B")
    with pytest.raises(ProviderError):
        p.list_securities(MI)
    limited = AlphaVantageDiscovery("k", http_get=lambda url, params=None, timeout=None: _resp(body={"Information": "our standard API rate limit is 25 requests per day"}))
    with pytest.raises(ProviderRateLimited):
        limited.list_securities(US)


def test_eodhd_listing_parsing():
    rows = [{"Code": "ENI", "Name": "Eni SpA", "Country": "Italy", "Exchange": "MI", "Currency": "EUR", "Type": "Common Stock", "Isin": "IT0003132476"},
            {"Code": "X", "Name": "Fund", "Exchange": "MI", "Currency": "EUR", "Type": "FUND", "Isin": None}]
    p = EodhdDiscovery("k", http_get=lambda url, params=None, timeout=None: _resp(body=rows))
    out = p.list_securities(MI)
    assert [(r.ticker, r.security_type, r.isin, r.provider_symbol) for r in out] == [("ENI", "common_stock", "IT0003132476", "ENI.MI"), ("X", "other", None, "X.MI")]


FIGI_LIVE = [{"data": [{"figi": "BBG000FVRV79", "name": "ENI SPA", "ticker": "ENI", "exchCode": "IM", "compositeFIGI": "BBG000FVRTJ1", "securityType": "Common Stock",
                        "marketSector": "Equity", "shareClassFIGI": "BBG001S7QGH9", "securityType2": "Common Stock", "securityDescription": "ENI"}]},
             {"warning": "No identifier found."},
             {"data": [{"figi": "A"}, {"figi": "B"}]}]


def test_openfigi_enrichment_adds_identifiers_and_never_guesses(session):
    """FIGI_LIVE[0] is a verbatim live response for ENI's ISIN on Milan."""
    catalog.sync_exchange(session, "eodhd", MI, [rec("ENI", "Eni SpA", isin="IT0003132476", kind="other", raw=""), rec("NOPE", isin="IT0000000000"), rec("AMBIG")], T0)
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return _resp(body=FIGI_LIVE)

    figi = OpenFigi(http_post=post)
    assert (figi.batch_size, OpenFigi("key").batch_size) == (10, 100)
    out = catalog.enrich_with_figi(session, figi, limit=10)
    assert out == {"checked": 3, "matched": 1}
    assert sent["body"] == [{"idType": "ID_ISIN", "idValue": "IT0003132476", "exchCode": "IM"}, {"idType": "ID_ISIN", "idValue": "IT0000000000", "exchCode": "IM"},
                            {"idType": "TICKER", "idValue": "AMBIG", "exchCode": "IM"}] and "X-OPENFIGI-APIKEY" not in sent["headers"]
    eni = by_ticker(session, "ENI")
    assert (eni.figi, eni.composite_figi, eni.share_class_figi, eni.security_type) == ("BBG000FVRV79", "BBG000FVRTJ1", "BBG001S7QGH9", "common_stock")
    assert by_ticker(session, "NOPE").figi is None and by_ticker(session, "AMBIG").figi is None  # not found / ambiguous -> left empty
    assert catalog.enrich_with_figi(session, figi, limit=10) == {"checked": 0, "matched": 0}  # each security is asked once
    with pytest.raises(ValueError):
        figi.lookup([{"ticker": "X"}] * 11)
    with pytest.raises(ProviderRateLimited):
        OpenFigi(http_post=lambda *a, **k: _resp(429, {})).lookup([{"ticker": "X"}])


# ---------------------------------------------------------------- search and coverage
def test_search_filters_and_coverage_report(session):
    catalog.sync_exchange(session, "finnhub", MI, [rec("ENI", "Eni SpA", isin="IT0003132476", figi="BBG000FVRV79"), rec("SWDA", "iShares Core MSCI World", kind="etf", raw="ETP"),
                                                  rec("OLD", "Old SpA", isin="IT0000000001")], T0)
    catalog.sync_exchange(session, "finnhub", MI, [rec("ENI", "Eni SpA", isin="IT0003132476", figi="BBG000FVRV79"), rec("SWDA", "iShares Core MSCI World", kind="etf", raw="ETP")], T0 + timedelta(days=1))
    catalog.sync_exchange(session, "alphavantage", US, [rec("AAPL", "Apple Inc", currency="USD", mic="XNAS"), rec("ENI", "Other ENI", currency="USD", mic="XNYS")], T0)
    catalog.record_failed_sync(session, "eodhd", MI, "EODHD rejected the API key")
    session.flush()

    def find(**kw):
        return [(i["exchange"], i["ticker"]) for i in catalog.search(session, **kw)["items"]]

    assert find(q="eni") == [("MI", "ENI"), ("US", "ENI")]  # ticker
    assert find(q="IT0003132476") == [("MI", "ENI")] and find(q="BBG000FVRV79") == [("MI", "ENI")]  # ISIN, FIGI
    assert find(q="apple") == [("US", "AAPL")] and find(q="ishares core") == [("MI", "SWDA")]  # company name
    assert find(exchanges=["MI"]) == [("MI", "ENI"), ("MI", "SWDA")] and find(countries=["US"]) == [("US", "AAPL"), ("US", "ENI")]
    assert find(types=["etf"]) == [("MI", "SWDA")] and find(status="delisted") == [("MI", "OLD")] and len(find(status=None)) == 5
    assert find(exchanges=["MI", "US"], types=["common_stock"], limit=2, offset=1) == [("US", "AAPL"), ("US", "ENI")]
    assert catalog.search(session, exchanges=["MI", "US"])["total"] == 4

    rep = catalog.coverage_report(session)
    mi = next(e for e in rep["exchanges"] if e["code"] == "MI")
    assert (mi["discovered"], mi["active"], mi["delisted"], mi["with_isin"], mi["with_figi"], mi["equities_active"]) == (3, 2, 1, 2, 1, 1)
    assert mi["by_type"]["etf"] == 1 and mi["by_type"]["common_stock"] == 1  # ETFs are counted apart from equities
    assert mi["providers"]["finnhub"]["status"] == "ok" and mi["providers"]["finnhub"]["received"] == 2
    assert mi["providers"]["eodhd"] == {"status": "failed", "last_sync": mi["providers"]["eodhd"]["last_sync"], "received": None, "message": "EODHD rejected the API key"}
    tokyo = next(e for e in rep["exchanges"] if e["code"] == "T")
    assert tokyo["discovered"] == 0 and tokyo["providers"]["finnhub"] == {"status": "never synchronised"}
    assert rep["exchanges_with_data"] == 2 and rep["totals"]["discovered"] == 5 and rep["totals"]["with_realtime_data"] == 0
    assert "not a complete record" in rep["statement"]


# ---------------------------------------------------------------- through the API
class FakeDiscovery:
    name, label = "finnhub", "Fake"

    def __init__(self, data, fail=None):
        self.data, self.fail = data, fail or {}

    def list_securities(self, exchange):
        if exchange.code in self.fail:
            raise self.fail[exchange.code]
        return self.data.get(exchange.code, [])


def wait(client, admin):
    for _ in range(200):
        st = client.get("/api/global/sync", headers=admin).json()
        if st["status"] not in ("pending", "running"):
            return st
        time.sleep(0.05)
    raise AssertionError("sync did not finish")


@pytest.fixture
def api(client):
    admin = login(client)
    client.put("/api/settings", headers=admin, json=UserSettings(initial_paper_capital=100_000).model_dump())
    gm = client.app.state.container.runtime.global_markets
    gm.job_options = {"pause": 0, "sleep": lambda s: None}
    gm.figi_factory = lambda key: OpenFigi(http_post=lambda url, json=None, headers=None, timeout=None: _resp(body=[{"warning": "No identifier found."}] * len(json)))
    return client, admin, gm


def test_catalog_sync_search_profile_and_activation_over_http(api):
    client, admin, gm = api
    assert client.get("/api/global/coverage", headers=admin).json()["totals"]["discovered"] == 0
    assert "No API key" in client.post("/api/global/sync", headers=admin, json={"provider": "finnhub"}).json()["detail"]
    assert client.put("/api/market/providers/finnhub/key", headers=admin, json={"api_key": "fh-key-1234"}).status_code == 204
    data = {"MI": [rec("ENI", "Eni SpA", isin="IT0003132476", figi="BBG000FVRV79"), rec("ENEL", "Enel SpA", isin="IT0003128367"), rec("WAR", "Warrant", kind="other", raw="Warrant")],
            "US": [rec("AAPL", "Apple Inc", currency="USD", mic="XNAS", figi="BBG000B9XRY4"), rec("NVDA", "NVIDIA Corp", currency="USD", mic="XNAS")],
            "T": [rec("7203", "Toyota Motor", currency="JPY", mic="XTKS", isin="JP3633400001")]}
    gm.discovery_factory = lambda name, key: FakeDiscovery(data, fail={"HK": ProviderError("no data for this exchange")})
    for body, msg in (({"provider": "finnhub", "exchanges": ["ZZ"]}, "Unknown exchange"), ({"provider": "nope"}, "Unknown discovery provider")):
        r = client.post("/api/global/sync", headers=admin, json=body)
        assert r.status_code == 400 and msg in r.json()["detail"]
    assert client.post("/api/global/sync", headers=login(client, VIEWER), json={"provider": "finnhub"}).status_code == 403
    assert client.post("/api/global/sync", headers=admin, json={"provider": "finnhub", "exchanges": ["MI", "US", "T", "HK"]}).json()["total"] == 4
    st = wait(client, admin)
    assert st["status"] == "finished" and [(r["exchange"], r["status"]) for r in st["results"]] == [("MI", "ok"), ("US", "ok"), ("T", "ok"), ("HK", "failed")]
    assert st["figi"]["checked"] == 4 and st["results"][1]["linked_to_instruments"] == 1  # AAPL already existed as an instrument

    rep = client.get("/api/global/coverage", headers=admin).json()
    assert rep["totals"]["discovered"] == 6 and rep["exchanges_with_data"] == 3
    hk = next(e for e in rep["exchanges"] if e["code"] == "HK")
    assert hk["providers"]["finnhub"]["status"] == "failed" and "no data" in hk["providers"]["finnhub"]["message"]

    found = client.get("/api/global/securities", headers=admin, params={"q": "IT0003132476"}).json()
    assert found["total"] == 1 and found["items"][0]["ticker"] == "ENI"
    multi = client.get("/api/global/securities", headers=admin, params=[("exchange", "MI"), ("exchange", "T"), ("type", "common_stock")]).json()
    assert [i["ticker"] for i in multi["items"]] == ["ENEL", "ENI", "7203"]
    assert client.get("/api/global/securities", headers=admin, params={"type": "bond"}).status_code == 422
    prof = client.get(f"/api/global/securities/{found['items'][0]['id']}", headers=admin).json()
    assert prof["history"][0]["kind"] == "listed" and prof["providers"] == ["finnhub"]
    assert client.get("/api/global/securities/424242", headers=admin).status_code == 404

    ids = {i["ticker"]: i["id"] for i in client.get("/api/global/securities", headers=admin, params={"limit": 50}).json()["items"]}
    out = client.post("/api/global/securities/activate", headers=admin, json={"ids": [ids["ENI"], ids["NVDA"], ids["7203"], ids["WAR"], ids["AAPL"], 99999]}).json()
    assert sorted(out["activated"]) == sorted([ids["ENI"], ids["NVDA"]]) and out["already_active"] == [ids["AAPL"]]
    reasons = {u["id"]: u["reason"] for u in out["unsupported"]}
    assert "not supported yet for Tokyo" in reasons[ids["7203"]] and "cannot be traded" in reasons[ids["WAR"]] and reasons[99999] == "unknown security"
    listed = {i["id"] for i in client.get("/api/instruments", headers=admin, params={"q": "STOCK:"}).json()}
    assert {"STOCK:ENI.MI", "STOCK:NVDA"} <= listed
    assert client.get("/api/global/securities", headers=admin, params={"q": "ENI"}).json()["items"][0]["instrument_id"] == "STOCK:ENI.MI"
    provs = {p["name"]: p for p in client.get("/api/global/providers", headers=admin).json()}
    assert provs["finnhub"]["configured"] and provs["finnhub"]["key_hint"] == "…1234" and not provs["eodhd"]["configured"] and provs["openfigi"]["configured"]


def test_bad_key_stops_the_job_and_scheduled_sync_only_runs_when_due(api):
    client, admin, gm = api
    client.put("/api/market/providers/finnhub/key", headers=admin, json={"api_key": "fh-key-1234"})
    gm.discovery_factory = lambda name, key: FakeDiscovery({}, fail={"MI": ProviderAuthError("Finnhub rejected the API key")})
    client.post("/api/global/sync", headers=admin, json={"provider": "finnhub", "exchanges": ["MI", "PA", "DE"]})
    st = wait(client, admin)
    assert st["status"] == "failed" and st["done"] == 1 and "rejected" in st["message"]  # no point asking for the other exchanges

    gm.discovery_factory = lambda name, key: FakeDiscovery({"MI": [rec("ENI", isin="IT0003132476")]})
    assert gm.run_scheduled(0) is None  # scheduling switched off
    assert gm.run_scheduled(24) == "finnhub"
    assert wait(client, admin)["status"] == "finished"
    assert gm.run_scheduled(24) is None  # just synchronised
    assert gm.run_scheduled(24, now=datetime.now(timezone.utc) + timedelta(hours=25)) == "finnhub"
    wait(client, admin)


def test_openfigi_separates_reits_and_adrs_when_the_listing_only_said_stock(session):
    catalog.sync_exchange(session, "alphavantage", US, [rec("O", "Realty Income", raw="Stock", currency="USD"), rec("BABA", "Alibaba ADR", raw="Stock", currency="USD"),
                                                       rec("KO", "Coca-Cola", raw="Stock", currency="USD"), rec("ENI", "Eni", raw="Common Stock", currency="USD")], T0)
    hits = {"O": "REIT", "BABA": "ADR", "KO": "Common Stock", "ENI": "ADR"}

    def post(url, json=None, headers=None, timeout=None):
        return _resp(body=[{"data": [{"figi": f"BBG{j['idValue']:>09}"[:12], "securityType": hits[j["idValue"]]}]} for j in json])

    catalog.enrich_with_figi(session, OpenFigi(http_post=post), limit=10)
    kinds = {s.ticker: s.security_type for s in session.scalars(select(Security))}
    assert kinds == {"O": "reit", "BABA": "adr", "KO": "common_stock", "ENI": "common_stock"}  # a specific provider type is not overridden
    assert ("type_changed", "common_stock", "reit", "openfigi") in events(session, by_ticker(session, "O", "US"))


def test_search_puts_exact_ticker_before_prefix_and_name_matches(session):
    catalog.sync_exchange(session, "alphavantage", US, [rec(t, n, currency="USD") for t, n in (("AAPB", "GraniteShares 2x Long AAPL"), ("AAPL", "Apple Inc"), ("AAPLX", "Longer"),
                                                                                             ("ZZZ", "Aapl tracker fund"), ("MSFT", "Microsoft"))], T0)
    assert [i["ticker"] for i in catalog.search(session, q="aapl")["items"]] == ["AAPL", "AAPLX", "AAPB", "ZZZ"]

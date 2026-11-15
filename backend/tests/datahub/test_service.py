"""The data platform service end to end: ingestion, catalog, resume, budgets, credentials, health, isolation from trading."""
import ast
from pathlib import Path

import httpx
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import AppSetting, AuditEntry, DataIngestionRun, DataSeries, OrderRow
from app.datahub import tasks
from app.datahub.http import SourceDisabled
from app.datahub.service import DataHub, DataHubError
from app.security.vault import CredentialVault

from . import fakes


def make(db, tmp_path, exchanges: dict | None = None, **overrides) -> tuple[DataHub, dict]:
    values = {"database_url": "sqlite://", "credential_key": CredentialVault.generate_key(), "data_lake_dir": str(tmp_path / "lake"),
              "fred_api_key": "k" * 32, "sec_user_agent": "Research Desk desk@example.com", **overrides}
    settings = Settings(_env_file=None, **values)
    apis = {"fred": fakes.Fred(), "sec": fakes.Sec()}
    made: dict = exchanges if exchanges is not None else {}

    def exchange(name: str):
        return made.setdefault(name, fakes.Exchange(page=1000 if name == "binance" else 720, recent_only=name == "kraken"))

    hub = DataHub(db, settings, transports={k: httpx.MockTransport(v) for k, v in apis.items()}, exchange_factory=exchange)
    return hub, {**apis, "exchanges": made}


START = pd.Timestamp(fakes.T0, unit="ms", tz="UTC")


def test_macro_ingestion_writes_the_lake_the_catalog_and_the_log(db, tmp_path):
    hub, apis = make(db, tmp_path)
    out = hub.ingest("fred", "macro", ["CPIAUCSL", "DGS10", "NOPE"], actor="tester@example.com")
    assert (out["keys"], out["ok"], out["failed"], out["rows_new"], out["requests"]) == (3, 2, 1, 8, 3)
    by_key = {r["key"]: r for r in out["results"]}
    assert by_key["NOPE"]["status"] == "failed" and "FRED" in by_key["NOPE"]["message"]  # one bad series does not stop the others
    assert by_key["DGS10"]["quality"]["missing_values"] == 1 and by_key["CPIAUCSL"]["quality"]["notes"]["pit"] == "vintage"
    assert len(hub.lake.read("macro", source="fred", series_id="CPIAUCSL")) == 5

    catalog = {c["key"]: c for c in hub.catalog("macro")}
    assert set(catalog) == {"CPIAUCSL", "DGS10"} and catalog["CPIAUCSL"]["rows"] == 5 and catalog["CPIAUCSL"]["last"].startswith("2024-02-01")
    assert catalog["DGS10"]["cadence_seconds"] == 86400 and catalog["DGS10"]["stale"] is True  # data from 2024 is overdue today
    assert catalog["CPIAUCSL"]["detail"]["title"].startswith("Consumer price index")
    with db.session() as session:
        runs = list(session.scalars(select(DataIngestionRun).order_by(DataIngestionRun.id)))
        assert [r.status for r in runs].count("ok") == 2 and all(r.actor == "tester@example.com" and r.finished_at is not None for r in runs)
        assert sum(r.requests for r in runs) == 3 and all(r.fetch_ms >= 0 for r in runs)
    log = hub.runs(provider="fred")
    assert len(log) == 3 and {r["status"] for r in log} == {"ok", "failed"}
    health = hub.health()["fred"]
    assert (health["runs"], health["ok"], health["failed"], health["requests"]) == (3, 2, 1, 3) and health["availability"] == pytest.approx(2 / 3, abs=1e-4)
    assert health["last_error"] and health["last_success"]

    again = hub.ingest("fred", "macro", ["CPIAUCSL"])
    assert again["rows_new"] == 0 and again["rows_revised"] == 0  # the same figures again: nothing is rewritten
    # A daily series already stored is only re-read from a month before its newest observation.
    hub.ingest("fred", "macro", ["DGS10"])
    assert apis["fred"].calls[-1]["observation_start"] == "2024-02-12"


def test_crypto_bars_resume_where_they_stopped_and_venues_stay_separate(db, tmp_path):
    hub, apis = make(db, tmp_path)
    first = hub.ingest("binance", "bars", ["BTC/USDT"], "1m", start=START, end=START + pd.Timedelta(minutes=999))
    assert first["rows_new"] == 1000 and first["results"][0]["quality"]["ok"] is True
    resumed = hub.ingest("binance", "bars", ["BTC/USDT"], "1m", end=START + pd.Timedelta(minutes=2499))
    calls = [c for c in apis["exchanges"]["binance"].calls if c[0] == "ohlcv"]
    assert calls[1][3] == fakes.T0 + 1000 * fakes.MINUTE  # asked for the bar after the newest one stored, not everything again
    assert resumed["rows_new"] == 1500 and len(hub.lake.bars("binance", "BTC/USDT", "1m")) == 2500

    kraken = hub.ingest("kraken", "bars", ["BTC/USD"], "1m", start=START)
    result = kraken["results"][0]
    assert result["rows_new"] == 720 and "history_truncated_at" in result["quality"]["notes"]  # Kraken's 720-candle limit is reported
    assert {(c["provider"], c["key"], c["rows"]) for c in hub.catalog("bars")} == {("binance", "BTC/USDT", 2500), ("kraken", "BTC/USD", 720)}
    assert set(hub.lake.read("bars", source="binance")["source"]) == {"binance"}
    assert all(e.closed for e in apis["exchanges"].values())


def test_bad_bars_are_removed_counted_and_logged(db, tmp_path):
    exchange = fakes.Exchange(history=300)
    exchange.bad_candle_at = 150
    hub, _ = make(db, tmp_path, exchanges={"binance": exchange})
    out = hub.ingest("binance", "bars", ["BTC/USDT"], "1m", start=START, end=START + pd.Timedelta(minutes=299))
    quality = out["results"][0]["quality"]
    assert out["rows_new"] == 299 and quality["invalid_rows_removed"] == 1 and quality["missing_bars"] == 1 and quality["ok"] is False
    assert quality["missing_examples"] == [(START + pd.Timedelta(minutes=150)).isoformat()]
    with db.session() as session:
        assert session.scalars(select(DataIngestionRun)).one().quality["invalid_rows_removed"] == 1


def test_order_books_trades_and_sec_data(db, tmp_path):
    hub, apis = make(db, tmp_path)
    book = hub.ingest("kraken", "orderbook", ["BTC/USD"], depth=2)
    assert book["rows_new"] == 1 and hub.lake.read("orderbook", source="kraken")["levels"].iloc[0] == 2
    trades = hub.ingest("kraken", "trades", ["BTC/USD"])
    assert trades["rows_new"] == 20
    more = hub.ingest("kraken", "trades", ["BTC/USD"])  # resumes from the newest stored trade
    assert [c for c in apis["exchanges"]["kraken"].calls if c[0] == "trades"][-1][2] == int(hub.lake.read("trades", source="kraken")["ts"].iloc[19].timestamp() * 1000)
    assert more["rows_new"] == 19  # the trade at the resume point was already stored and is not duplicated

    sec = hub.ingest("sec", "fundamentals", ["AAPL", "ZZZZ"])
    by_key = {r["key"]: r for r in sec["results"]}
    assert by_key["AAPL"]["status"] == "ok" and by_key["AAPL"]["rows_new"] == 5 and by_key["ZZZZ"]["status"] == "failed"
    assert by_key["AAPL"]["quality"]["availability"] == {"accepted": 4, "filing_date_rule": 1}
    catalog = {(c["dataset"], c["key"]): c for c in hub.catalog(provider="sec")}
    assert catalog[("fundamentals", "AAPL")]["rows"] == 5 and catalog[("filings", "AAPL")]["rows"] == 3  # one request stores both datasets
    assert catalog[("fundamentals", "AAPL")]["detail"] == {"ticker": "AAPL", "cik": "0000320193", "name": "Apple Inc."}
    hub.ingest("sec", "filings", ["AAPL"])
    ticker_requests = [u for u, _ in apis["sec"].calls if u.endswith("company_tickers.json")]
    assert len(ticker_requests) == 1  # the ticker list is cached on disk between ingestions


def test_sources_stay_off_until_they_have_what_they_need(db, tmp_path):
    hub, _ = make(db, tmp_path, fmp_api_key="", alpaca_api_key="", data_sources="fred,sec,kraken,coingecko,fmp,alpaca")
    status = {p["name"]: p for p in hub.provider_status()}
    assert status["fred"]["ready"] and status["sec"]["ready"] and status["kraken"]["ready"] and status["coingecko"]["ready"]
    assert not status["fmp"]["ready"] and "API key" in status["fmp"]["state"]
    assert not status["alpaca"]["ready"] and not status["binance"]["ready"] and "switched off" in status["binance"]["state"]
    assert all(p["licence"] and p["rate_limit"] and p["history"] and p["limitations"] for p in status.values())
    for provider, dataset, timeframe in (("fmp", "company_profile", ""), ("binance", "bars", "1m"), ("alpaca", "bars", "1m")):
        with pytest.raises(SourceDisabled):
            hub.ingest(provider, dataset, ["X"], timeframe)
    for args in (("nobody", "bars"), ("fred", "bars"), ("kraken", "bars", ["BTC/USD"], "7m")):
        with pytest.raises(DataHubError):
            hub.ingest(*args)
    with db.session() as session:
        assert session.scalars(select(DataIngestionRun)).first() is None  # refused before any request was made

    # A key stored encrypted through the platform's key store switches the source on, without a restart.
    with db.session() as session:
        session.add(AppSetting(key="provider:fmp", value={"ciphertext": CredentialVault(hub.settings.credential_key).encrypt({"api_key": "stored-key", "api_secret": ""})}))
    assert {p["name"]: p["ready"] for p in hub.provider_status()}["fmp"] is True and hub.credentials("fmp")["api_key"] == "stored-key"
    no_agent, _ = make(db, tmp_path, sec_user_agent="")
    assert "QAT_SEC_USER_AGENT" in {p["name"]: p["state"] for p in no_agent.provider_status()}["sec"]


def test_free_plan_budget_is_enforced_across_ingestions(db, tmp_path):
    hub, _ = make(db, tmp_path, fmp_api_key="fmp-key", fmp_daily_budget=3)
    sent = []

    def fmp(request):
        sent.append(request.url.params["symbol"])
        return httpx.Response(200, json=[{"symbol": request.url.params["symbol"], "companyName": "X", "sector": "Tech", "marketCap": 1e9}])

    hub.transports["fmp"] = httpx.MockTransport(fmp)
    first = hub.ingest("fmp", "company_profile", ["AAPL", "MSFT"])
    assert first["ok"] == 2 and hub.usage("fmp")["requests_today"] == 2
    second = hub.ingest("fmp", "company_profile", ["NVDA", "AMZN", "META"])
    statuses = sorted(r["status"] for r in second["results"])
    assert statuses == ["limited", "limited", "ok"] and len(sent) == 3  # the fourth and fifth requests were never sent
    assert "budget" in next(r["message"] for r in second["results"] if r["status"] == "limited")
    assert hub.usage("fmp")["requests_today"] == 3


def test_background_jobs_and_default_keys(db, tmp_path):
    hub, _ = make(db, tmp_path, data_macro_series="DGS10,DGS2", data_crypto_pairs="binance:BTC/USDT,kraken:BTC/USD,kraken:ETH/USD")
    job = hub.start("fred", "macro")
    hub.jobs[job["id"]]["_thread"].join(timeout=30)
    done = hub.job(job["id"])
    assert done["status"] == "finished" and done["result"]["keys"] == 2 and done["finished_at"]  # no keys given: the configured list
    assert hub._default_keys("kraken", "bars") == ["BTC/USD", "ETH/USD"] and hub._default_keys("binance", "bars") == ["BTC/USDT"]
    with pytest.raises(LookupError):
        hub.job("missing")
    with pytest.raises(SourceDisabled):
        hub.start("fmp", "company_profile", ["AAPL"])  # refused at once, not inside the background thread


def test_schedule_is_off_by_default_and_skips_sources_that_are_not_ready(db, tmp_path, monkeypatch):
    assert tasks.schedule(Settings(_env_file=None)) == {}
    on = Settings(_env_file=None, data_schedule_enabled=True, data_equity_symbols="AAPL,SPY")
    entries = tasks.schedule(on)
    assert set(entries) == {"data-fast", "data-bars", "data-hourly", "data-daily"} and all(e["options"]["queue"] == "ingest" for e in entries.values())
    assert "data-fast" not in tasks.schedule(Settings(_env_file=None, data_schedule_enabled=True, data_orderbook_seconds=0))
    assert {(j["provider"], j["dataset"]) for j in tasks.plan("daily", on)} == {("fred", "macro"), ("coingecko", "crypto_market"), ("sec", "fundamentals"),
                                                                              ("fmp", "company_profile")}
    assert not any(j["provider"] in ("sec", "fmp", "alpaca") for g in ("bars", "daily") for j in tasks.plan(g, Settings(_env_file=None)))
    from app.core.tasks import celery_app

    assert celery_app.conf.task_routes == {"data.*": {"queue": "ingest"}} and "app.datahub.tasks" in celery_app.conf.imports

    hub, _ = make(db, tmp_path, data_macro_series="DGS10", data_coingecko_ids="bitcoin", data_sources="fred")
    monkeypatch.setattr(tasks, "hub", lambda: hub)
    monkeypatch.setattr(tasks, "get_settings", lambda: hub.settings)
    out = tasks.collect("daily")
    assert out[0]["provider"] == "fred" and out[0]["ok"] == 1 and "skipped" in out[1] and out[1]["provider"] == "coingecko"
    assert tasks.ingest("fred", "macro", ["DGS2"])["rows_new"] == 3


def test_the_data_platform_cannot_reach_trading(db, tmp_path):
    """Static check: no module of the package imports the trading side. Plus: ingesting creates no order and no trading audit entry."""
    forbidden = ("brokers", "execution", "risk", "strategies", "signals", "paper_trading", "live_trading", "portfolio")
    root = Path(__file__).resolve().parents[2] / "app" / "datahub"
    seen = 0
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                seen += 1
                parts = name.split(".")
                assert not any(p in forbidden for p in parts), f"{path.name} imports {name}"
    assert seen > 30
    hub, _ = make(db, tmp_path)
    with db.session() as session:
        audit_before = len(session.scalars(select(AuditEntry)).all())
    hub.ingest("binance", "bars", ["BTC/USDT"], "1m", start=START, end=START + pd.Timedelta(minutes=50))
    hub.ingest("fred", "macro", ["DGS10"])
    with db.session() as session:
        assert session.scalars(select(OrderRow)).first() is None and len(session.scalars(select(AuditEntry)).all()) == audit_before
        assert session.scalars(select(DataSeries)).first() is not None

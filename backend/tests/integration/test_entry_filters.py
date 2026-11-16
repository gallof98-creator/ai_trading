"""Entry filters: quarterly results, fundamentals and news. The same rules in the validation backtest, the scan and the
trading engine; point in time; and they only ever remove entries."""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.domain.models import Order, Side
from app.quant_ai import filters as flt
from app.quant_ai import opportunities as opp
from tests.integration.test_opportunities import CYC, rt  # noqa: F401  (fixture)


def signals(targets):
    idx = pd.date_range("2024-01-01", periods=len(targets), freq="1D", tz="UTC")
    t = pd.Series(targets, index=idx)
    change = t.diff().fillna(t)
    return pd.DataFrame({"target": t, "action": np.select([change > 0, change < 0], ["BUY", "SELL"], "HOLD"), "close": 1.0}, index=idx)


def test_a_blocked_entry_removes_the_whole_position_and_never_enters_late():
    s = signals([0, 1, 1, 1, 0, -1, -1, 0, 1, 1])
    blocked = pd.Series(False, index=s.index)
    blocked.iloc[1] = True                      # the first long opens on a blocked bar
    out = flt.apply_entry_block(s, blocked_any=blocked)
    assert out["target"].tolist() == [0, 0, 0, 0, 0, -1, -1, 0, 1, 1]
    assert out["action"].tolist() == ["HOLD", "HOLD", "HOLD", "HOLD", "HOLD", "SELL", "HOLD", "BUY", "BUY", "HOLD"]
    long_only = pd.Series(False, index=s.index)
    long_only.iloc[5] = long_only.iloc[8] = True  # blocks longs only: the short on bar 5 stays, the long on bar 8 goes
    assert flt.apply_entry_block(s, blocked_long=long_only)["target"].tolist() == [0, 1, 1, 1, 0, -1, -1, 0, 0, 0]
    blocked_mid = pd.Series(False, index=s.index)
    blocked_mid.iloc[2] = True                   # a block in the middle of a position never closes it
    assert flt.apply_entry_block(s, blocked_any=blocked_mid)["target"].tolist() == s["target"].tolist()


def filings(rows):
    return pd.DataFrame([{"form": f, "items": i, "available_at": pd.Timestamp(t, tz="UTC")} for f, i, t in rows])


def test_results_releases_and_their_windows_are_built_from_what_was_known():
    f = filings([("8-K", "2.02,9.01", "2024-02-01 21:30"), ("10-Q", "", "2024-02-02 21:00"), ("8-K", "5.07", "2024-03-10 21:00"),
                 ("8-K", "2.02,9.01", "2024-05-02 21:30"), ("8-K", "2.02", "2024-08-01 21:30")])
    rel = flt.releases_from_filings(f)
    assert rel == [pd.Timestamp("2024-02-01 21:30", tz="UTC"), pd.Timestamp("2024-05-02 21:30", tz="UTC"), pd.Timestamp("2024-08-01 21:30", tz="UTC")]
    cfg = flt.FilterSettings(earnings_days_before=2, earnings_hours_after=24)
    w = flt.earnings_windows(rel, cfg)
    at = lambda s: flt.window_at(pd.Timestamp(s, tz="UTC"), w)  # noqa: E731
    assert "released 2024-02-01" in at("2024-02-02 15:00")          # the day after a release
    assert at("2024-02-03 15:00") is None
    assert at("2024-04-15") is None
    assert at("2024-04-30 15:00") is None
    assert "expected around 2024-05-02" in at("2024-05-01 10:00")    # 91 days after the first release, within two days before
    assert "released 2024-05-02" in at("2024-05-03 10:00")
    assert "expected around 2024-08-01" in at("2024-07-31 10:00")   # the usual interval, from the releases known by then
    assert "expected" in at("2024-11-01") or at("2024-11-01") is None
    # With no 8-K 2.02 at all, the periodic reports stand in.
    assert flt.releases_from_filings(filings([("10-Q", "", "2024-05-05 12:00"), ("10-K", "", "2024-02-20 12:00")])) == [
        pd.Timestamp("2024-02-20 12:00", tz="UTC"), pd.Timestamp("2024-05-05 12:00", tz="UTC")]


def facts(rows):
    return flt.facts_frame([[c, s, e, v, a] for c, s, e, v, a in rows])


def test_fundamentals_are_judged_on_the_reports_published_at_the_time():
    f = facts([
        ("Revenues", "2021-01-01", "2021-12-31", 100.0, "2022-02-15"), ("NetIncomeLoss", "2021-01-01", "2021-12-31", 10.0, "2022-02-15"),
        ("StockholdersEquity", "2021-12-31", "2021-12-31", 50.0, "2022-02-15"), ("Liabilities", "2021-12-31", "2021-12-31", 100.0, "2022-02-15"),
        ("Revenues", "2022-01-01", "2022-12-31", 80.0, "2023-02-15"), ("NetIncomeLoss", "2022-01-01", "2022-12-31", -5.0, "2023-02-15"),
        ("StockholdersEquity", "2022-12-31", "2022-12-31", 45.0, "2023-02-15"),
    ])
    cfg = flt.FilterSettings()
    before = flt.snapshot(f, pd.Timestamp("2022-06-01", tz="UTC"))
    assert before.net_income == 10 and before.revenue == 100 and before.revenue_growth is None and before.failures(cfg) == []
    assert before.margin == pytest.approx(0.1) and before.roe == pytest.approx(0.2) and before.leverage == pytest.approx(2.0)
    after = flt.snapshot(f, pd.Timestamp("2023-03-01", tz="UTC"))
    assert after.revenue_growth == pytest.approx(-0.2)
    assert any("loss" in x for x in after.failures(cfg)) and any("revenue down 20%" in x for x in after.failures(cfg))
    times = pd.DatetimeIndex(pd.to_datetime(["2021-06-01", "2022-06-01", "2023-02-14", "2023-02-16", "2024-01-01"], utc=True))
    assert flt.fundamentals_blocked(f, times, cfg).tolist() == [False, False, False, True, True]


# ---------------------------------------------------------------- through the platform
STOCK = "STOCK:ACME"


def with_stock(rt):
    """The fixture's cyclical history, now as a US stock with SEC data and news injected."""
    from app.market_data.quality import DataKind
    from app.market_data.sources import Dataset

    rt.add_instruments(["ACME"], "US", "stock", "admin")
    with rt.db.session() as s:
        rt.store.save(s, STOCK, "1d", Dataset(rt.test_bars, DataKind.HISTORICAL, "test"))
    return rt


def quarterly(start, end):
    out, t = [], pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=21)
    while t < pd.Timestamp(end, tz="UTC"):
        out.append(("8-K", "2.02", t.strftime("%Y-%m-%d %H:%M")))
        t += pd.Timedelta(days=91)
    return filings(out)


def install_sec(rt, filing_frame, fact_rows):
    rt.company.fetch_tickers = lambda ua: {"ACME": {"cik": "0000000001", "title": "Acme"}}
    rt.company.fetch_filings = lambda cik, tk, ua: filing_frame.assign(accession=[f"a{k}" for k in range(len(filing_frame))])
    fr = facts(fact_rows)
    rt.company.fetch_facts = lambda cik, tk, ua, acc: fr.assign(unit="USD")


def test_validation_and_scan_apply_the_earnings_filter_and_record_it(rt):
    with_stock(rt)
    bars = rt.test_bars
    install_sec(rt, quarterly(bars.index[0].strftime("%Y-%m-%d"), (bars.index[-1] + pd.Timedelta(days=200)).strftime("%Y-%m-%d")), [])
    out = rt.company.refresh_sec([STOCK], rt.clock())
    assert out["filings_refreshed"] == 1 and rt.company.signature(STOCK, "1d") == {"earnings": True, "fundamentals": False}
    with rt.db.session() as s:
        spec = opp.prepare(s, rt, "ema_crossover", STOCK, "1d")
        v = opp.validate(s, rt, "ema_crossover", STOCK, "1d", "admin", allow_short=True)
    assert spec.blocked_any is not None and spec.blocked_any.sum() > 0 and spec.blocked_long is None
    assert v["filters"] == {"earnings": True, "fundamentals": False}
    # Every entry the strategy made in the validation history fell outside the windows.
    w = flt.earnings_windows(rt.company.releases("ACME"), rt.company.settings())
    frame = flt.apply_entry_block(opp.create_strategy("ema_crossover", v["params"]).generate(bars), spec.blocked_any)
    opens = frame.index[(frame["target"] != frame["target"].shift()) & (frame["target"] != 0)]
    assert len(opens) and all(flt.window_at(t + pd.Timedelta(days=1), w) is None for t in opens)


def test_news_and_results_stop_new_entries_live_but_never_exits(rt):
    with_stock(rt)
    now = pd.Timestamp(rt.clock())
    install_sec(rt, filings([("8-K", "2.02", (now - pd.Timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"))]), [])
    rt.company.refresh_sec([STOCK], rt.clock())
    dep_id = rt.add_deployment("ema_crossover", {}, STOCK, "1d", "paper-main", "admin")
    rt.update_deployment(dep_id, "admin", active=True)
    entry = Order(broker_id="paper-main", instrument_id=STOCK, side=Side.BUY, qty=1, strategy="ema_crossover")
    assert "earnings filter: quarterly results released" in rt.entry_block(entry)
    manual = entry.model_copy(update={"strategy": "manual"})
    assert rt.entry_block(manual) is None                    # the user's own orders are the user's decision
    rt.now[0] += timedelta(days=2)
    assert rt.entry_block(entry) is None
    rt.company.fetch_news = lambda symbols, since, creds: [
        {"id": 1, "ts": (pd.Timestamp(rt.clock()) - pd.Timedelta(minutes=10)).isoformat(), "headline": "Acme recalls widgets", "source": "benzinga", "symbols": ["ACME"]},
        {"id": 2, "ts": (pd.Timestamp(rt.clock()) - pd.Timedelta(minutes=5)).isoformat(), "headline": "Markets wrap", "source": "benzinga",
         "symbols": ["ACME", "AAA", "BBB", "CCC", "DDD"]}]
    assert rt.company.refresh_news([STOCK, CYC], rt.clock()) == {"symbols": 2, "new_items": 2}
    assert 'news filter: "Acme recalls widgets"' in rt.entry_block(entry)
    rt.now[0] += timedelta(minutes=61)
    assert rt.entry_block(entry) is None                     # the pause is over; the market-wide item never counted


def test_fundamentals_block_long_entries_of_daily_strategies_only(rt):
    with_stock(rt)
    install_sec(rt, filings([("8-K", "2.02", "2020-01-01 21:00")]), [
        ("Revenues", "2020-01-01", "2020-12-31", 100.0, "2021-02-15"), ("NetIncomeLoss", "2020-01-01", "2020-12-31", -10.0, "2021-02-15"),
        ("StockholdersEquity", "2020-12-31", "2020-12-31", 50.0, "2021-02-15")])
    rt.company.refresh_sec([STOCK], rt.clock())
    assert rt.company.signature(STOCK, "1d")["fundamentals"] and not rt.company.signature(STOCK, "15m")["fundamentals"]
    assert "fundamentals filter: loss in the last fiscal year" in rt.company.live_block(STOCK, rt.clock(), True, "1d")
    assert rt.company.live_block(STOCK, rt.clock(), False, "1d") is None      # a short in a loss-making company is not refused
    assert rt.company.live_block(STOCK, rt.clock(), True, "1h") is None       # quarterly figures say nothing about an hour
    snap = rt.company.fundamentals_now(STOCK, rt.clock())
    assert snap.to_dict()["net_income"] == -10 and snap.score() is not None
    # The SEC data survives a restart.
    rt.company.companies.clear()
    rt.company.load()
    assert rt.company.signature(STOCK, "1d")["fundamentals"]


def test_a_changed_filter_sends_the_validation_back_for_a_retest(rt):
    with_stock(rt)
    with rt.db.session() as s:
        v = opp.validate(s, rt, "ema_crossover", STOCK, "1d", "admin", allow_short=True)
    assert v["filters"] == {"earnings": False, "fundamentals": False}
    install_sec(rt, filings([("8-K", "2.02", "2023-01-01 21:00")]), [])
    rt.company.refresh_sec([STOCK], rt.clock())
    assert rt.company.signature(STOCK, "1d") != v["filters"]   # the autopilot treats it as due (see Autopilot._validate)


def test_only_the_needed_concepts_are_downloaded_and_dated_by_their_filing():
    import asyncio

    from app.datahub.http import SourceNotFound
    from app.quant_ai.company_data import concept_facts

    asked = []

    class Client:
        async def get_json(self, path):
            asked.append(path.rsplit("/", 1)[1])
            if "NetIncomeLoss" in path:
                return {"units": {"USD": [{"start": "2023-01-01", "end": "2023-12-31", "val": 5, "accn": "A1", "filed": "2024-02-01"},
                                          {"start": "2024-01-01", "end": "2024-12-31", "val": 7, "accn": "A2", "filed": "2025-02-01"}]}}
            raise SourceNotFound("not reported")

    accepted = pd.Series({"A1": pd.Timestamp("2024-02-01 21:05", tz="UTC")})
    frame = asyncio.run(concept_facts(None, Client(), "1", accepted))
    assert asked == [f"{c}.json" for c in flt.CONCEPTS]
    assert frame["value"].tolist() == [5, 7] and frame["available_at"].iloc[0] == pd.Timestamp("2024-02-01 21:05", tz="UTC")
    assert frame["available_at"].iloc[1] == pd.Timestamp("2025-02-01 18:00", tz="America/New_York").tz_convert("UTC")  # no acceptance time known
    assert flt.snapshot(flt.facts_frame([[r.concept, r.period_start.isoformat(), r.period_end.isoformat(), r.value, r.available_at.isoformat()]
                                         for r in frame.itertuples()]), pd.Timestamp("2025-03-01", tz="UTC")).net_income == 7

"""Autopilot: universe, prices, validation, live subscriptions and scans without a click; orders still need one."""
from datetime import timedelta

from app.quant_ai import opportunities as opp

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.database.base import Database
from app.database.settings_store import AutopilotSettings, initial_settings, save_settings
from app.domain.models import AssetClass, OrderStatus
from app.execution.runtime import TradingRuntime
from app.market_data.calendars import get_calendar
from app.market_data.providers import PriceProvider, SymbolNotFound
from app.market_data.quality import DataKind, validate_bars
from app.market_data.sources import Dataset
from app.security.vault import CredentialVault
from tests.integration.test_live import Feeder
from tests.integration.test_opportunities import cut_at_entry


def us_series(seed, n=1700, period=60, amp=0.25, base=100.0):
    """Test data on US trading days: regular swings a trend rule can follow."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-02", periods=n, tz="UTC", name="ts")
    logp = np.log(base) + amp * np.sin(2 * np.pi * np.arange(n) / period + seed) + np.cumsum(rng.normal(0, 0.004, n))
    c = np.exp(logp)
    o = np.concatenate([[c[0]], c[:-1]])
    frame = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998, "close": c, "volume": 5e6}, index=idx)
    return validate_bars(frame, "1d", get_calendar("XNYS"))[0]


class FakeAlpacaData(PriceProvider):
    name, label, markets, min_interval, needs_secret = "alpaca", "Alpaca", frozenset({"US"}), 0.0, True

    def __init__(self, frames):
        self.frames, self.requests = frames, []

    def list_symbols(self, market):
        return []

    def daily_bars(self, instrument, start=None, end=None):
        self.requests.append((instrument.symbol, start))
        if instrument.symbol not in self.frames:
            raise SymbolNotFound(instrument.symbol)
        bars = self.frames[instrument.symbol]
        if start is not None:
            bars = bars[bars.index >= pd.Timestamp(start, tz="UTC")]
        return Dataset(bars, DataKind.HISTORICAL, "alpaca")


@pytest.fixture
def rt(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.create_all()
    key = CredentialVault.generate_key()
    aaa = cut_at_entry(us_series(1))          # ends on a bar where the trend strategy opens a position
    end = aaa.index[-1]
    frames = {"AAA": aaa, "BBB": us_series(2)[:end], "CCC": us_series(3, base=2.0)[:end]}   # CCC is a penny stock: below the price floor
    now = [end.to_pydatetime() + timedelta(days=1, hours=1)]
    runtime = TradingRuntime(db, Settings(_env_file=None, credential_key=key, enabled_providers="alpaca"), clock=lambda: now[0])
    runtime.start()
    runtime.create_paper_account("paper-main", "Main", "admin")
    runtime.add_fx_rate("USD", "EUR", 0.9, "manual", "admin", now[0] - timedelta(hours=2))
    runtime.set_provider_key("alpaca", "PK-TEST", "admin", "SK-TEST")
    data = FakeAlpacaData(frames)
    runtime.provider_factory = lambda name, key: data
    runtime.download_options = {"sleep": lambda s: None, "min_interval": 0}
    feeder = Feeder(runtime.live_hub)
    runtime.live_source_factory = lambda _rt: [(feeder, lambda inst: inst.symbol if inst.asset_class is AssetClass.STOCK else None)]
    runtime.autopilot.most_active = lambda key, secret, top: ["AAA", "BBB", "CCC", "NOPE"]
    runtime.autopilot.wait_seconds = 60
    runtime.autopilot.fetch_fx = lambda: [("EUR", "USD", now[0] - timedelta(hours=3), 1.1)]  # the ECB publishes one rate a day
    runtime.now, runtime.data, runtime.frames = now, data, frames
    return runtime


def configure(rt, **kw):
    kw.setdefault("crypto_pairs", [])     # crypto and ETFs have their own tests below
    kw.setdefault("etfs", [])
    kw.setdefault("allow_short", True)  # the trend rule then trades often enough on the test series to be fully validated
    cfg = AutopilotSettings(enabled=True, strategies=["ema_crossover", "rsi_reversion"], max_instruments=10, min_traded_value=1e6, **kw)
    with rt.db.session() as s:
        save_settings(s, initial_settings().model_copy(update={"autopilot": cfg}))
        rt.reload_settings(s)


def test_a_cycle_goes_from_an_empty_universe_to_a_proposal_without_a_click(rt):
    assert rt.autopilot.tick() is None and rt.autopilot.status()["enabled"] is False  # off until the user switches it on
    configure(rt)
    out = rt.autopilot.tick()
    assert out["universe"]["added"] == ["STOCK:AAA", "STOCK:BBB", "STOCK:CCC", "STOCK:NOPE"] and out["universe"]["histories_requested"] == 4
    assert out["candidates"] == {"count": 2, "by_liquidity": 2, "crypto": 0, "live_only": 0, "broker_id": "paper-main"}  # the penny stock and the one without data are left out
    counts = {k: out["validation"][k] for k in ("run", "passed", "candidates", "could_not_run", "remaining")}
    assert counts == {"run": 4, "passed": 2, "candidates": 0, "could_not_run": 0, "remaining": 0}  # the trend rule passes, the mean-reversion rule does not
    assert out["validation"]["multiple_testing"]["setups_tested"] == 4 and out["validation"]["speed"]["validations"] == 4
    # Every candidate is followed live (within the streaming limit here, so all stream).
    assert out["live"]["followed"] == 2 and out["live"]["streamed"] == 2 and out["live"]["unavailable"] == 0
    assert set(rt.live.subscribed) == {"STOCK:AAA", "STOCK:BBB"}
    assert out["scan"]["validated_setups"] == 2 and out["scan"]["proposed"] == 1
    assert any("rsi_reversion AAA 1d: did not pass validation" == r for r in out["scan"]["reasons"])

    # The outcome is a proposal. Nothing was sent, and the mode and limits are untouched.
    [p] = rt.proposals()
    assert (p["symbol"], p["side"], p["strategy"]) == ("AAA", "BUY", "ema_crossover") and p["risk_base"] <= 20.0
    assert not [o for o in rt.engine.orders.values() if o.status is OrderStatus.SUBMITTED]
    assert rt.user_settings.trading_mode == "semi_auto" and rt.user_settings.risk.risk_per_trade == 0.02
    st = rt.autopilot.status()
    assert st["sends_orders_by_itself"] is False and st["cycles"] == 1 and st["alpaca_key"] and "every order still waits for your approval" in st["note"]
    assert any("1 new proposal" in a["text"] for a in st["activity"]) and any("4 validations run: 2 validated, 0 candidates, 2 rejected" in a["text"] for a in st["activity"])

    # Not due yet: the next tick does nothing. Once due, nothing is repeated needlessly.
    assert rt.autopilot.tick() is None
    rt.now[0] += timedelta(minutes=16)
    again = rt.autopilot.tick()
    assert "skipped" in again["universe"] and "skipped" in again["prices"] and again["validation"]["run"] == 0 and again["scan"]["proposed"] == 0
    assert len(rt.proposals()) == 1 and again["scan"]["funnel"]["already"] == 1  # still waiting for a decision: not duplicated
    assert again["scan"]["blockers"][0][1] >= 1 and isinstance(again["scan"]["blockers"][0][0], str)
    # A proposal from a daily bar lives twelve hours. Once it has lapsed, the same entry is not proposed a second time.
    rt.now[0] += timedelta(hours=13)
    later = rt.autopilot.tick()
    assert rt.proposals() == [] and later["scan"]["proposed"] == 0 and any("cancelled (proposal expired)" in r for r in later["scan"]["reasons"])


def test_prices_are_refreshed_daily_and_expired_validations_are_redone(rt):
    configure(rt)
    rt.autopilot.tick()
    first_requests = len(rt.data.requests)
    # Next day: one more bar exists at the provider.
    for symbol, frame in list(rt.frames.items()):
        nxt = frame.index[-1] + pd.offsets.BDay(1)
        last = frame["close"].iloc[-1]
        rt.frames[symbol] = pd.concat([frame, pd.DataFrame({"open": last, "high": last * 1.01, "low": last * 0.99, "close": last * 1.002, "volume": 5e6},
                                                           index=pd.DatetimeIndex([nxt], name="ts"))])
    rt.now[0] += timedelta(days=1)
    out = rt.autopilot.tick()
    assert out["prices"] == {"new_bars": 3, "failed": 0} and len(rt.data.requests) > first_requests
    with rt.db.session() as s:
        assert rt.store.load(s, "STOCK:AAA", "1d").bars.index[-1] == rt.frames["AAA"].index[-1]
    assert out["validation"]["run"] == 0  # still valid: not repeated
    rt.now[0] += timedelta(days=31)
    out = rt.autopilot.run_cycle()
    assert out["exchange_rates"] == {"rates": 1}
    assert out["validation"]["run"] == 4  # thirty days later every validation has lapsed and is redone


def test_autopilot_respects_the_kill_switch_missing_key_and_one_failing_step(rt):
    configure(rt, seed_most_active=False)
    out = rt.autopilot.tick()
    assert out["universe"]["skipped"] == "0 US instruments already have prices" and out["universe"]["etfs"] == 0 and out["candidates"]["count"] == 0
    configure(rt)
    rt.set_provider_key("alpaca", None, "admin")
    out = rt.autopilot.run_cycle()
    assert "no Alpaca key" in out["universe"]["skipped"] and "no Alpaca key" in out["prices"]["skipped"]
    rt.set_provider_key("alpaca", "PK-TEST", "admin", "SK-TEST")

    def broken(key, secret, top):
        raise ConnectionError("screener unreachable")

    rt.autopilot.most_active = broken
    rt.autopilot.state["last_seed"] = None
    out = rt.autopilot.run_cycle()
    assert "screener unreachable" in out["universe"]["error"] and "scan" in out  # the other steps still ran
    rt.kill_switch(True, "EMERGENCY STOP", "admin")
    assert rt.autopilot.run_cycle()["scan"] == {"skipped": "trading is paused by the risk engine"}
    assert rt.proposals() == []


def test_autopilot_endpoints(client):
    from tests.conftest import VIEWER, login

    admin, viewer = login(client), login(client, VIEWER)
    st = client.get("/api/quant/autopilot", headers=viewer).json()
    assert st["enabled"] is False and st["settings"]["cycle_minutes"] == 15 and st["settings"]["timeframes"] == ["1d"]
    body = {**st["settings"], "enabled": True, "max_instruments": 12}
    assert client.put("/api/quant/autopilot", headers=viewer, json=body).status_code == 403
    assert client.put("/api/quant/autopilot", headers=admin, json={**body, "strategies": ["nope"]}).status_code == 422
    assert client.put("/api/quant/autopilot", headers=admin, json={**body, "cycle_minutes": 1}).status_code == 422
    # The autopilot's own settings cannot carry a trading mode or a risk limit.
    assert client.put("/api/quant/autopilot", headers=admin, json={**body, "trading_mode": "auto"}).json()["trading_mode"] == "semi_auto"
    saved = client.put("/api/quant/autopilot", headers=admin, json=body).json()
    assert saved["enabled"] is True and saved["settings"]["max_instruments"] == 12
    assert client.get("/api/settings", headers=admin).json()["autopilot"]["enabled"] is True
    assert client.post("/api/quant/autopilot/run", headers=viewer).status_code == 403
    client.app.state.container.runtime.autopilot.fetch_crypto = lambda pair: pd.DataFrame()
    client.app.state.container.runtime.autopilot.fetch_fx = lambda: []  # the background cycle must not reach the internet from a test
    assert client.post("/api/quant/autopilot/run", headers=admin).status_code == 202


def test_long_only_default_reaches_the_candidate_level_and_trades_it_on_paper_only_when_allowed(rt):
    configure(rt, allow_short=False)
    out = rt.autopilot.tick()
    assert out["validation"]["passed"] == 0 and out["validation"]["candidates"] == 2 and out["scan"]["candidate_setups"] == 2
    assert out["scan"]["proposed"] == 1 and rt.proposals()[0]["rationale"]["validation"]["level"] == "candidate"
    assert rt.proposals()[0]["side"] == "BUY"  # long-only: never a short sale

    other = rt  # a second pass with candidates switched off proposes nothing new and says why
    configure(other, allow_short=False, paper_candidates=False)
    other.now[0] += timedelta(minutes=16)
    again = other.autopilot.tick()
    assert again["scan"]["proposed"] == 0 and any("candidate only" in r for r in again["scan"]["reasons"])


def test_crypto_pairs_are_added_priced_and_analysed_without_a_key(rt):
    from tests.integration.test_opportunities import cyclical

    rt.set_provider_key("alpaca", None, "admin")  # crypto data needs no key
    end = rt.frames["AAA"].index[-1]
    bars = cyclical()
    bars.index = pd.date_range(end=end, periods=len(bars), freq="1D", tz="UTC", name="ts")
    bars["volume"] = 3.0   # Alpaca's crypto bars carry its own small venue volume, far below any stock liquidity floor
    asked = []

    def fetch(pair):
        asked.append(pair)
        if pair == "NOPE/USD":
            raise ConnectionError("unknown pair")
        return bars

    rt.autopilot.fetch_crypto = fetch
    configure(rt, crypto_pairs=["btc/usd", "ETH/USD", "NOPE/USD"], seed_most_active=False)
    out = rt.autopilot.tick()
    assert asked == ["BTC/USD", "ETH/USD", "NOPE/USD"]
    assert out["crypto"]["refreshed"] == 2 and out["crypto"]["not_available"][0].startswith("NOPE/USD (ConnectionError")
    assert out["candidates"]["crypto"] == 2 and out["candidates"]["by_liquidity"] == 0   # followed whatever Alpaca's own venue volume says
    # Every series was validated. Which level each reaches depends on the take profit the validation picks in sample:
    # on this series a near target that worked in sample failed on unseen data, so the trend rule is rejected.
    assert out["validation"]["run"] == 4 and out["validation"]["passed"] + out["validation"]["candidates"] == 2
    with rt.db.session() as s:
        stored = rt.store.load(s, "CRYPTO:BTC/USD", "1d")
    assert stored.source == "alpaca-crypto" and stored.kind.value == "historical" and len(stored.bars) == len(bars)
    assert rt.registry.get("CRYPTO:BTC/USD").quote_currency == "USD" and rt.registry.is_mapped("paper-main", "CRYPTO:BTC/USD")
    rt.now[0] += timedelta(minutes=16)
    assert rt.autopilot.tick()["crypto"] == {"skipped": "already updated in the last 20 hours"} and len(asked) == 3


def test_the_forming_daily_bar_of_a_crypto_pair_is_never_stored():
    import httpx

    from app.quant_ai.autopilot import alpaca_crypto_daily

    now = pd.Timestamp("2026-11-14 10:30", tz="UTC").to_pydatetime()
    pages = [{"bars": {"BTC/USD": [{"t": "2026-11-12T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}, "next_page_token": "p2"},
             {"bars": {"BTC/USD": [{"t": "2026-11-13T00:00:00Z", "o": 1.5, "h": 3, "l": 1, "c": 2, "v": 20},
                                   {"t": "2026-11-14T00:00:00Z", "o": 2, "h": 2.5, "l": 1.9, "c": 2.2, "v": 3}]}, "next_page_token": None}]
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=pages[len(calls) - 1])

    frame = alpaca_crypto_daily("BTC/USD", client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)
    assert [str(t.date()) for t in frame.index] == ["2026-11-12", "2026-11-13"]  # today's bar is still forming and is left out
    assert frame["close"].tolist() == [1.5, 2.0] and calls[1]["page_token"] == "p2" and calls[0]["timeframe"] == "1Day"


def test_etfs_are_kept_in_the_universe_as_etfs_and_analysed(rt):
    end = rt.frames["AAA"].index[-1]
    rt.frames["SPY"] = us_series(7)[:end]
    rt.frames["QQQ"] = us_series(8)[:end]
    rt.autopilot.most_active = lambda key, secret, top: ["AAA", "SPY"]   # the screener may return an ETF: it must not become a second, "stock" SPY
    configure(rt, etfs=["spy", "QQQ"])
    out = rt.autopilot.tick()
    assert out["universe"]["etfs"] == 2 and out["universe"]["added"] == ["STOCK:AAA"] and out["universe"]["histories_requested"] == 3
    ids = {i.id for i in rt.registry.all()}
    assert {"ETF:SPY", "ETF:QQQ"} <= ids and "STOCK:SPY" not in ids and rt.registry.get("ETF:QQQ").asset_class is AssetClass.ETF
    with rt.db.session() as s:
        assert rt.store.load(s, "ETF:QQQ", "1d") is not None and rt.store.load(s, "ETF:SPY", "1d") is not None
    assert out["candidates"]["by_liquidity"] == 3 and out["validation"]["run"] == 6   # ETFs are validated like any other instrument
    assert rt.registry.is_mapped("paper-main", "ETF:QQQ") and rt.registry.get("ETF:QQQ").lot_size == 0.0001


def test_a_running_cycle_says_which_step_it_is_on_and_how_far_it_got(rt):
    import threading

    configure(rt)
    release, entered = threading.Event(), threading.Event()
    real = rt.autopilot._validate

    def slow_validate(result):
        rt.autopilot.state.update(step_progress="3 of 25 validations done")
        entered.set()
        release.wait(30)
        return real(result)

    rt.autopilot._validate = slow_validate
    worker = threading.Thread(target=rt.autopilot.run_cycle)
    worker.start()
    assert entered.wait(60)
    st = rt.autopilot.status()
    assert st["running"] and st["current"]["step"] == "validation" and st["current"]["detail"] == "3 of 25 validations done"
    assert st["current"]["minutes"] >= 0 and st["cycles"] == 0
    release.set()
    worker.join(120)
    st = rt.autopilot.status()
    assert not st["running"] and st["current"] is None and st["cycles"] == 1 and st["step"] is None


def test_validation_goes_breadth_first_with_daily_data_before_minute_data(rt):
    configure(rt, validations_per_cycle=2)   # strategies: ema_crossover, then rsi_reversion
    rt.autopilot.tick()
    with rt.db.session() as s:
        from app.quant_ai import opportunities as opp

        tested = {(v["strategy"], v["instrument_id"]) for v in opp.validations(s, rt.clock())}
    # Two validations: the first strategy on two different instruments, not both strategies on the first instrument.
    assert {t[0] for t in tested} == {"ema_crossover"} and len({t[1] for t in tested}) == 2


def test_validation_gets_most_of_a_cycle_never_an_hour_and_opportunities_come_first(rt):
    configure(rt, cycle_minutes=15)
    assert rt.autopilot.validation_budget() == 540
    configure(rt, cycle_minutes=5)
    assert rt.autopilot.validation_budget() == 180
    out = rt.autopilot.run_cycle()
    steps = list(out["seconds_per_step"])
    assert steps.index("scan") < steps.index("validation") < steps.index("rescan")


def test_after_a_change_of_criteria_former_candidates_are_retested_first_and_every_tradable_setup_is_listed(rt, monkeypatch):
    configure(rt, allow_short=False)
    out = rt.autopilot.tick()
    assert out["validation"]["candidates"] == 2
    listed = {(t["strategy"], t["instrument_id"].split(":")[1], t["level"], t["outcome"]) for t in out["scan"]["tradable_setups"]}
    assert any(level == "candidate" and outcome == "proposed" for *_, level, outcome in listed)
    with rt.db.session() as s:
        before = {(v["strategy"], v["instrument_id"]): v["recorded_status"] for v in opp.validations(s, rt.clock())}
    former = {k for k, st in before.items() if st == "candidate"}
    monkeypatch.setitem(opp.CRITERIA, "version", 99)   # new criteria: every earlier result is due for a retest
    opp._CACHE.clear()
    configure(rt, allow_short=False, validations_per_cycle=2)
    rt.now[0] += timedelta(minutes=16)
    again = rt.autopilot.tick()
    assert again["validation"]["run"] == 2
    # The candidates waiting for their retest are listed, with why they are not traded meanwhile.
    assert all(t["level"] for t in again["scan"]["tradable_setups"])
    with rt.db.session() as s:
        retested = {(v["strategy"], v["instrument_id"]) for v in opp.validations(s, rt.clock()) if v["criteria_version"] == 99}
    assert retested == former, "the setups that could trade were not retested first"
